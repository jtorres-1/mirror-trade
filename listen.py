# listen.py — Telegram -> PocketOption with martingale (anchored ML, tight gaps, no ghosts, chain fingerprint)

import os, re, csv, asyncio, sys, requests, emoji, uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

load_dotenv()

# ── Env ────────────────────────────────────────────────────────────────────────
api_id       = int(os.getenv("API_ID", "0"))
api_hash     = os.getenv("API_HASH", "")
phone        = os.getenv("PHONE_NUMBER")
session_name = os.getenv("SESSION_NAME", "mirrortrade")
channel      = os.getenv("CHANNEL")

tz_offset_minutes = -int(os.getenv("TZ_OFFSET_MIN", "240"))
FORCE_OTC       = os.getenv("FORCE_OTC", "1") == "1"
base_amount     = float(os.getenv("TRADE_AMOUNT", "1"))
mg_mult         = float(os.getenv("MARTINGALE_MULT", "2.2"))
MAX_STAKE       = float(os.getenv("MAX_STAKE", "10.65"))
DAILY_STOP_LOSS = float(os.getenv("DAILY_STOP_LOSS", "0"))

SKEW_MS       = int(os.getenv("SKEW_MS", "4200"))

ML1_GAP_S     = float(os.getenv("ML1_GAP_S", "2"))
ML2_GAP_S     = float(os.getenv("ML2_GAP_S", "2"))

if not api_id or not api_hash:
    print("[FATAL] API_ID/API_HASH missing in .env"); sys.exit(1)
if not channel:
    print("[FATAL] CHANNEL missing in .env"); sys.exit(1)
if channel and not channel.startswith("@"):
    channel = "@" + channel

client = TelegramClient(session_name, api_id, api_hash)

LOG_FILE = "trade_log.csv"
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, "w", newline="") as f:
        csv.writer(f).writerow(
            ["ts_utc","pair","direction","expiry_min","amount","result","profit","ml_tag"]
        )

def log_trade(pair, direction, expiry_min, amount, result, profit, ml_tag=""):
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.utcnow().isoformat(), pair, direction, expiry_min, amount, result, profit, ml_tag
        ])

PAIR_RE = re.compile(r'([A-Z]{3}/[A-Z]{3})', re.I)
TIME_RE = re.compile(r'(\d{1,2}:\d{2})')
MIN_RE  = re.compile(r'(\d+)\s*m', re.I)
SUMMARY_MARKERS = ("REPORT","SESSION","FINISHED","ACCURACY","TESTIMONIAL","CONTACT SUPPORT","FOLLOW ME")

def looks_like_summary(text: str) -> bool:
    return any(m in text.upper() for m in SUMMARY_MARKERS)

def normalize_signal_text(text: str) -> str:
    text = text.replace('\u200b',' ')
    text = text.replace("|", "\n")
    text = emoji.replace_emoji(text, replace="")
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def parse_signal(text: str) -> Optional[Dict]:
    norm = normalize_signal_text(text)
    if looks_like_summary(norm):
        return None
    d = {"pair": None, "direction": None, "expiry_min": None, "entry_time": None}

    m_pair = PAIR_RE.search(norm.upper())
    if m_pair: d["pair"] = m_pair.group(1)

    lines = [ln.strip() for ln in norm.splitlines() if ln.strip()]
    for ln in lines:
        up = ln.upper()
        if "BUY" in up:  d["direction"] = "BUY"
        if "SELL" in up: d["direction"] = "SELL"
        if "EXPIRATION" in up:
            m = MIN_RE.search(up)
            if m: d["expiry_min"] = int(m.group(1))
        if "ENTRY" in up:
            m = TIME_RE.search(ln)
            if m: d["entry_time"] = m.group(1)

    if d["expiry_min"] is None:
        d["expiry_min"] = 5

    if d["pair"] and d["direction"] and d["entry_time"]:
        return d
    return None

def resolve_entry_datetime(hhmm: str, msg_date_utc: datetime) -> datetime:
    hh, mm = map(int, hhmm.split(":"))
    et = msg_date_utc + timedelta(minutes=tz_offset_minutes)
    candidate = et.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return candidate - timedelta(minutes=tz_offset_minutes)

def et_day_key() -> str:
    now_utc = datetime.utcnow()
    now_et = now_utc + timedelta(minutes=tz_offset_minutes)
    return now_et.strftime("%Y-%m-%d")

current = {
    "active": False, "pair": None, "direction": None, "expiry_min": 5,
    "amount": base_amount,
    "base_exec_at": None,
    "ml1_exec_at": None,
    "chain_id": None  # new fingerprint per signal
}
last_signal_utc: Optional[datetime] = None
seen_ids = set()

daily_pnl = 0.0
halted_for_day = False
executor_busy = False

scheduled_tasks = []
ttl_task = None
ml1_task = None
ml2_task = None

def cancel_task(t):
    try:
        if t and not t.done():
            t.cancel()
    except Exception:
        pass

def cancel_all_tasks():
    global scheduled_tasks, ttl_task, ml1_task, ml2_task
    for t in scheduled_tasks:
        cancel_task(t)
    cancel_task(ttl_task); ttl_task = None
    cancel_task(ml1_task); ml1_task = None
    cancel_task(ml2_task); ml2_task = None
    scheduled_tasks = []

def arm_ttl(until_dt: datetime, why: str):
    global ttl_task
    cancel_task(ttl_task)
    async def _ttl(deadline_dt: datetime, cid: str):
        try:
            await asyncio.sleep(max(0, (deadline_dt - datetime.utcnow()).total_seconds() + 60))
            if current["active"] and current["chain_id"] == cid:
                reset_chain("TTL watchdog expired")
        except asyncio.CancelledError:
            return
    cid = current["chain_id"]
    ttl_task = asyncio.create_task(_ttl(until_dt, cid))
    scheduled_tasks.append(ttl_task)
    print(f"[TTL] armed until {until_dt} ({why}) [chain={cid}]")

def reset_chain(reason=""):
    cancel_all_tasks()
    current.update({
        "active": False, "pair": None, "direction": None,
        "expiry_min": 5, "amount": base_amount,
        "base_exec_at": None, "ml1_exec_at": None,
        "chain_id": None
    })
    if reason: print(f"[RESET] {reason}")
    else:      print("[RESET] Chain cleared.")

last_win_ping_utc: Optional[datetime] = None
last_win_chain: Optional[str] = None
WIN_HINT_RE = re.compile(r'\bWIN\b|\bVICTORY\b', re.I)

PO_URL = "http://localhost:3000"

def force_otc(pair: str) -> str:
    return pair if (not FORCE_OTC or "OTC" in pair.upper()) else f"{pair} OTC"

def quick_peek() -> (bool, float):
    try:
        r = requests.get(f"{PO_URL}/peek", timeout=1.5)
        if r.status_code == 200:
            j = r.json()
            return bool(j.get("ok", False)), float(j.get("profit", 0.0))
    except Exception:
        pass
    return False, 0.0

def executor_trade(pair, amount, direction, ml_tag) -> Dict:
    payload = {"pair": pair, "amount": amount, "direction": direction.lower(), "ml_tag": ml_tag}
    try:
        r = requests.post(f"{PO_URL}/trade", json=payload, timeout=400)
        if r.status_code == 200:
            return r.json()
        else:
            print(f"[API ERROR] {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[API EXCEPTION] {e}")
    return {"success": False, "result": "OPEN", "profit": 0}

async def run_one_trade(pair, direction, expiry_min, amount, ml_label=None) -> None:
    global executor_busy, daily_pnl, halted_for_day, ml1_task, ml2_task, last_win_chain
    if DAILY_STOP_LOSS > 0 and daily_pnl <= -DAILY_STOP_LOSS:
        print("[HALT] Daily stop loss reached. Skip trade.")
        return
    if executor_busy:
        print("[BLOCK] Executor busy; skipping overlapping call.")
        return
    executor_busy = True

    clean_pair = force_otc(pair)
    ml_tag = "BASE" if ml_label is None else f"ML{ml_label}"

    try:
        data = await asyncio.get_event_loop().run_in_executor(
            None, lambda: executor_trade(clean_pair, amount, direction, ml_tag)
        )
        result = data.get("result", "OPEN")
        profit = float(data.get("profit", 0))
        log_trade(clean_pair, direction, expiry_min, amount, result, profit, ml_tag)
        print(f"[API] Fired: {direction} {clean_pair} ${amount} [{ml_tag}] → {result}")

        exec_time = datetime.utcnow()

        if profit > 0:
            last_win_chain = current["chain_id"]

        if ml_label is None:
            current["base_exec_at"] = exec_time
            base_close = exec_time + timedelta(minutes=expiry_min)
            gap1 = ML1_GAP_S if ML1_GAP_S is not None else 3
            ml1_fire = base_close + timedelta(seconds=gap1)
            cancel_task(ml1_task)
            ml1_task = asyncio.create_task(schedule_leg(ml1_fire, 1, base_close, current["chain_id"]))
            print(f"[CHAIN] ML1 anchored → {ml1_fire} (gap {gap1:.2f}s after base close)")
            arm_ttl(base_close + timedelta(minutes=expiry_min, seconds=30), "base->ml1 span")

        elif ml_label == 1:
            current["ml1_exec_at"] = exec_time
            ml1_close = exec_time + timedelta(minutes=expiry_min)
            gap2 = ML2_GAP_S if ML2_GAP_S is not None else 2
            ml2_fire = ml1_close + timedelta(seconds=gap2)
            cancel_task(ml2_task)
            ml2_task = asyncio.create_task(schedule_leg(ml2_fire, 2, ml1_close, current["chain_id"]))
            print(f"[CHAIN] ML2 anchored → {ml2_fire} (gap {gap2:.2f}s after ML1 close)")
            arm_ttl(ml1_close + timedelta(minutes=expiry_min, seconds=30), "ml1->ml2 span")

        elif ml_label == 2:
            ml2_close = exec_time + timedelta(minutes=expiry_min)
            arm_ttl(ml2_close + timedelta(seconds=30), "ml2 close")
    finally:
        executor_busy = False

    if ml_label == 2:
        print("[DONE] ML2 placed. Freeing chain for next signal.")
        reset_chain("ML2 placed; chain released.")

# ── Scheduling ────────────────────────────────────────────────────────────────
async def schedule_leg(fire_dt: datetime, ml_label: Optional[int], prev_close: Optional[datetime] = None, cid: Optional[str] = None):
    label = "BASE" if ml_label is None else f"ML{ml_label}"
    delay = max(0.0, (fire_dt - datetime.utcnow()).total_seconds())
    print(f"[SCHEDULE] {label} fire={fire_dt} delay={delay:.3f}s [chain={cid}]")

    try:
        await asyncio.sleep(delay)

        if ml_label in (1, 2):
            if prev_close:
                while datetime.utcnow() < prev_close:
                    await asyncio.sleep(0.1)  # finer wait

            # dynamic guard loop — only cancel if same chain
            start = datetime.utcnow()
            while (datetime.utcnow() - start).total_seconds() < 2.0:
                ok, p = quick_peek()
                if ok and p > 0 and cid == current["chain_id"] and last_win_chain == cid:
                    reset_chain(f"{label} cancelled: WIN via /peek (profit {p})")
                    return
                if (not ok) and last_win_ping_utc and (datetime.utcnow() - last_win_ping_utc).total_seconds() <= 6 and cid == current["chain_id"] and last_win_chain == cid:
                    reset_chain(f"{label} cancelled: WIN ping")
                    return
                await asyncio.sleep(0.2)
            # final last-sec peek
            ok, p = quick_peek()
            if ok and p > 0 and cid == current["chain_id"] and last_win_chain == cid:
                reset_chain(f"{label} cancelled last-sec: WIN via /peek (profit {p})")
                return

        amt = base_amount if ml_label is None else min(round(base_amount * (mg_mult ** ml_label), 2), MAX_STAKE)
        await run_one_trade(current["pair"], current["direction"], current["expiry_min"], amt, ml_label=ml_label)
    except asyncio.CancelledError:
        print(f"[CANCEL] {label} cancelled.")
        return

async def handle_signal_from_text(text: str, msg_date=None):
    global last_signal_utc, daily_pnl, halted_for_day, current, scheduled_tasks, last_win_ping_utc, ml1_task, ml2_task
    if WIN_HINT_RE.search(text):
        last_win_ping_utc = datetime.utcnow()
        return False
    sig = parse_signal(text)
    if not sig:
        return False
    if not msg_date:
        msg_date = datetime.utcnow().replace(tzinfo=timezone.utc)

    base_dt = resolve_entry_datetime(sig["entry_time"], msg_date.replace(tzinfo=None))
    if (datetime.utcnow() - base_dt).total_seconds() > 300:
        print(f"[INFO] Signal {sig['entry_time']} too old; ignoring.")
        return True

    if not hasattr(handle_signal_from_text, "_day"):
        handle_signal_from_text._day = et_day_key()
    if et_day_key() != handle_signal_from_text._day:
        daily_pnl = 0.0; halted_for_day = False
        handle_signal_from_text._day = et_day_key()
        reset_chain("ET day rollover")

    if DAILY_STOP_LOSS > 0 and halted_for_day:
        print("[HALT] Stop loss hit; ignoring signals.")
        return True

    now_utc = datetime.utcnow()
    if last_signal_utc and (now_utc - last_signal_utc).total_seconds() < 60:
        print("[INFO] Rapid signal ignored.")
        return True

    should_block = True
    for _ in range(6):
        ok, p = quick_peek()
        if not ok:
            should_block = False
            break
        if p != 0:
            if p > 0:
                last_win_ping_utc = datetime.utcnow()
            should_block = False
            break
        await asyncio.sleep(0.2)
    if should_block:
        print("[GUARD] Skipping new BASE: unresolved trade still open.")
        return True

    if current["active"]:
        print("[INFO] Chain active; ignoring new signal.")
        return True

    # new fingerprint
    cid = str(uuid.uuid4())[:8]
    current.update({
        "active": True, "pair": force_otc(sig["pair"]), "direction": sig["direction"],
        "expiry_min": sig["expiry_min"], "amount": base_amount,
        "base_exec_at": None, "ml1_exec_at": None, "chain_id": cid
    })
    last_signal_utc = now_utc
    print(f"[SIGNAL] {current['pair']} {sig['direction']} {sig['expiry_min']}m entry {sig['entry_time']} [chain={cid}]")

    cancel_task(ml1_task); ml1_task = None
    cancel_task(ml2_task); ml2_task = None

    base_fire = base_dt - timedelta(milliseconds=SKEW_MS)
    t0 = asyncio.create_task(schedule_leg(base_fire, None, cid=cid)); scheduled_tasks.append(t0)

    arm_ttl(base_fire + timedelta(minutes=current["expiry_min"], seconds=45), "base window")
    return True

async def on_signal(e):
    if e.message.id in seen_ids:
        return
    seen_ids.add(e.message.id)
    src = getattr(getattr(e, "chat", None), "title", None) or ""
    username = getattr(getattr(e, "chat", None), "username", None)
    print(f"[TG DEBUG] Incoming from: '{src}' (@{username})")
    text = (e.message.message or "").strip()
    print("[TG RAW]", text.replace("\n"," | ")[:500])
    ok = await handle_signal_from_text(text, msg_date=e.message.date)
    if not ok:
        print("[TG DEBUG] Ignored: no valid trading signal")

async def main():
    print("[DEBUG] Starting Telegram client...")
    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(phone)
        code = input("Enter the Telegram code: ").strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pw = input("Enter your 2FA password: ").strip()
            await client.sign_in(password=pw)
    me = await client.get_me()
    print(f"[DEBUG] Logged in as: {me.username or me.first_name} (ID {me.id})")
    entity = await client.get_entity(channel)
    print(f"[DEBUG] Listening to: {getattr(entity,'title',None)} (ID {entity.id})")
    client.add_event_handler(on_signal, events.NewMessage(chats=entity))
    print("[DEBUG] Pocket Option trade screen ready (via Node API).")
    await client.run_until_disconnected()

if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
