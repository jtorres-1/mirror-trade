# listen.py — Telegram -> PocketOption with martingale
# Outcome-anchored, hybrid wait (sleep-then-poll), no time-based races, no stale wins

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

SKEW_MS   = int(os.getenv("SKEW_MS", "2380"))      # base fires ~2.3s early vs entry
ML1_GAP_S = float(os.getenv("ML1_GAP_S", "1.0"))   # optional tiny gap after base loss
ML2_GAP_S = float(os.getenv("ML2_GAP_S", "0.25"))  # optional tiny gap after ML1 loss

# hybrid poll tuning
POLL_WINDOW_S  = float(os.getenv("POLL_WINDOW_S", "30"))   # increased from 12s → 30s
POLL_INTERVAL_S = float(os.getenv("POLL_INTERVAL_S", "0.2"))

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
            ["ts_utc","pair","direction","expiry_min","amount","result","profit","ml_tag","chain_id"]
        )

def log_trade(pair, direction, expiry_min, amount, result, profit, ml_tag="", chain_id=""):
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.utcnow().isoformat(), pair, direction, expiry_min, amount, result, profit, ml_tag, chain_id
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

# ── Trade State ──────────────────────────────────────────────────────────────
current = {
    "active": False, "pair": None, "direction": None, "expiry_min": 5,
    "amount": base_amount,
    "chain_id": None,
}
last_signal_utc: Optional[datetime] = None
seen_ids = set()

executor_busy = False
scheduled_tasks = []
ml1_task = None
ml2_task = None

def cancel_task(t):
    try:
        if t and not t.done():
            t.cancel()
    except Exception:
        pass

def cancel_all_tasks():
    global scheduled_tasks, ml1_task, ml2_task
    for t in scheduled_tasks: cancel_task(t)
    cancel_task(ml1_task); ml1_task = None
    cancel_task(ml2_task); ml2_task = None
    scheduled_tasks = []

def reset_chain(reason=""):
    cancel_all_tasks()
    current.update({
        "active": False, "pair": None, "direction": None,
        "expiry_min": 5, "amount": base_amount,
        "chain_id": None,
    })
    print(f"[RESET] {reason}" if reason else "[RESET] Chain cleared.")

# ── Executor / Peek ──────────────────────────────────────────────────────────
PO_URL = "http://localhost:3000"

def force_otc(pair: str) -> str:
    return pair if (not FORCE_OTC or "OTC" in pair.upper()) else f"{pair} OTC"

def quick_peek(chain_id: str):
    try:
        r = requests.get(f"{PO_URL}/peek", params={"chain_id": chain_id}, timeout=1.5)
        if r.status_code == 200:
            j = r.json()
            return (
                bool(j.get("ok", False)),
                float(j.get("profit", 0.0)),
                j.get("ml_tag", ""),
                j.get("chain_id", ""),
            )
    except Exception:
        pass
    return False, 0.0, "", ""

def executor_trade(pair, amount, direction, ml_tag, chain_id) -> Dict:
    payload = {
        "pair": pair, "amount": amount, "direction": direction.lower(),
        "ml_tag": ml_tag, "chain_id": chain_id, "expiration": current["expiry_min"] * 60
    }
    try:
        r = requests.post(f"{PO_URL}/trade", json=payload, timeout=400)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[API EXCEPTION] {e}")
    return {"success": False, "result": "OPEN", "profit": 0}

# ── Trade Execution ──────────────────────────────────────────────────────────
async def run_one_trade(pair, direction, expiry_min, amount, ml_label=None) -> None:
    global executor_busy, ml1_task, ml2_task
    if executor_busy: return
    executor_busy = True

    clean_pair = force_otc(pair)
    ml_tag = "BASE" if ml_label is None else f"ML{ml_label}"
    chain_id = current["chain_id"]

    try:
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: executor_trade(clean_pair, amount, direction, ml_tag, chain_id)
        )
        log_trade(clean_pair, direction, expiry_min, amount, "OPEN", 0.0, ml_tag, chain_id)
        print(f"[API] Fired: {direction} {clean_pair} ${amount} [{ml_tag}] [chain={chain_id}]")

        # Anchor next decision via hybrid wait (sleep then short poll)
        if ml_label is None:
            cancel_task(ml1_task)
            ml1_task = asyncio.create_task(wait_outcome_then_decide(expiry_min, 1, chain_id))
        elif ml_label == 1:
            cancel_task(ml2_task)
            ml2_task = asyncio.create_task(wait_outcome_then_decide(expiry_min, 2, chain_id))
    finally:
        executor_busy = False

# ── Outcome-anchored hybrid wait ─────────────────────────────────────────────
async def wait_outcome_then_decide(expiry_min: int, ml_label: int, cid: str):
    label = f"ML{ml_label}"
    full_sleep = max(0.0, expiry_min * 60.0)
    print(f"[WAIT] {label}: sleeping {full_sleep:.2f}s for expiry [chain={cid}]")
    await asyncio.sleep(full_sleep)

    # short, tight poll window
    start = datetime.utcnow()
    while (datetime.utcnow() - start).total_seconds() < POLL_WINDOW_S:
        ok, profit, _tag, peek_chain = quick_peek(cid)
        if ok and peek_chain == cid:
            if profit > 0:
                reset_chain(f"{label} cancelled: WIN via /peek [chain={cid}]")
                return
            gap = ML1_GAP_S if ml_label == 1 else ML2_GAP_S
            if gap > 0 and ml_label < 2:
                await asyncio.sleep(gap)
            if ml_label < 2:  # Only fire next if not ML2
                amt = min(round(base_amount * (mg_mult ** ml_label), 2), MAX_STAKE)
                await run_one_trade(current["pair"], current["direction"], current["expiry_min"], amt, ml_label=ml_label)
                return
            else:
                reset_chain(f"{label} LOSS confirmed [chain={cid}]")
                return
        await asyncio.sleep(POLL_INTERVAL_S)

    # Failsafe reset — no result found within poll window
    if ml_label == 2:
        reset_chain(f"{label} assumed LOSS (no result) [chain={cid}]")
    else:
        reset_chain(f"{label} aborted: no posted result within {POLL_WINDOW_S}s [chain={cid}]")

# ── Signal Handling ──────────────────────────────────────────────────────────
async def handle_signal_from_text(text: str, msg_date=None):
    sig = parse_signal(text)
    if not sig: return False
    if not msg_date: msg_date = datetime.utcnow().replace(tzinfo=timezone.utc)
    base_dt = resolve_entry_datetime(sig["entry_time"], msg_date.replace(tzinfo=None))
    if (datetime.utcnow() - base_dt).total_seconds() > 300: return True
    if current["active"]: return True

    cid = str(uuid.uuid4())[:8]
    current.update({
        "active": True, "pair": force_otc(sig["pair"]), "direction": sig["direction"],
        "expiry_min": sig["expiry_min"], "amount": base_amount,
        "chain_id": cid,
    })
    print(f"[SIGNAL] {current['pair']} {sig['direction']} {sig['expiry_min']}m entry {sig['entry_time']} [chain={cid}]")

    base_fire = base_dt - timedelta(milliseconds=SKEW_MS)
    delay = max(0.0, (base_fire - datetime.utcnow()).total_seconds())
    t0 = asyncio.create_task(asyncio.sleep(delay))
    async def _fire_base():
        await t0
        await run_one_trade(current["pair"], current["direction"], current["expiry_min"], base_amount, ml_label=None)
    scheduled_tasks.append(asyncio.create_task(_fire_base()))
    return True

async def on_signal(e):
    if e.message.id in seen_ids: return
    seen_ids.add(e.message.id)
    text = (e.message.message or "").strip()
    print("[TG RAW]", text.replace("\n"," | ")[:500])
    ok = await handle_signal_from_text(text, msg_date=e.message.date)
    if not ok: print("[TG DEBUG] Ignored")

async def main():
    print("[DEBUG] Starting Telegram client…")
    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(phone)
        code = input("Enter Telegram code: ").strip()
        try: await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pw = input("Enter 2FA password: ").strip()
            await client.sign_in(password=pw)
    entity = await client.get_entity(channel)
    client.add_event_handler(on_signal, events.NewMessage(chats=entity))
    print("[DEBUG] Pocket Option trade screen ready.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    with client: client.loop.run_until_complete(main())
