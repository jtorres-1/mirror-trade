# listen.py — Telegram -> PocketOption with martingale (hybrid, no delay, no ghosts)
# Strategy:
#   • Pre-schedule Base, ML1, ML2 at signal time (SKEW_MS early fire).
#   • Right before ML1/ML2 fires, do a just-in-time guard:
#       - Try quick PO "Closed" peek via Node (/peek) to see if last closed trade was WIN.
#       - Fallback: cancel if a channel "WIN" message was seen moments ago.
#   • On any WIN -> cancel all pending ML tasks and reset chain.
#   • ML2 is the cap. After ML2 is placed, chain is freed for next signal.
#   • TTL watchdog ensures chain is never stuck active across sessions or day rollovers.

import os, re, csv, asyncio, sys, requests, emoji
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

load_dotenv()

# Env
api_id       = int(os.getenv("API_ID", "0"))
api_hash     = os.getenv("API_HASH", "")
phone        = os.getenv("PHONE_NUMBER")
session_name = os.getenv("SESSION_NAME", "mirrortrade")
channel      = os.getenv("CHANNEL")

tz_offset_minutes = -int(os.getenv("TZ_OFFSET_MIN", "240"))
FORCE_OTC   = os.getenv("FORCE_OTC", "1") == "1"
base_amount = float(os.getenv("TRADE_AMOUNT", "1"))
mg_mult     = float(os.getenv("MARTINGALE_MULT", "2.2"))
MAX_STAKE   = float(os.getenv("MAX_STAKE", "10.65"))
DAILY_STOP_LOSS = float(os.getenv("DAILY_STOP_LOSS", "0"))
SKEW_MS     = int(os.getenv("SKEW_MS", "2200"))  # fire slightly early

if not api_id or not api_hash:
    print("[FATAL] API_ID/API_HASH missing in .env"); sys.exit(1)
if not channel:
    print("[FATAL] CHANNEL missing in .env"); sys.exit(1)
if channel and not channel.startswith("@"):
    channel = "@" + channel

# Telegram client
client = TelegramClient(session_name, api_id, api_hash)

# Logging
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

# Parsing
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
    d = {"pair": None, "direction": None, "expiry_min": None, "entry_time": None, "ml_levels": []}

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
        if "LEVEL" in up:
            times = TIME_RE.findall(ln)
            for t in times:
                if t != d["entry_time"]:
                    d["ml_levels"].append(t)

    if d["expiry_min"] is None:
        d["expiry_min"] = 5

    if d["pair"] and d["direction"] and d["entry_time"]:
        return d
    return None

# Time handling
def resolve_entry_datetime(hhmm: str, msg_date_utc: datetime) -> datetime:
    hh, mm = map(int, hhmm.split(":"))
    msg_et = msg_date_utc + timedelta(minutes=tz_offset_minutes)
    candidate = msg_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if (candidate - msg_et).total_seconds() < -6*3600: candidate += timedelta(days=1)
    elif (candidate - msg_et).total_seconds() > 18*3600: candidate -= timedelta(days=1)
    return candidate - timedelta(minutes=tz_offset_minutes)

def et_day_key() -> str:
    now_utc = datetime.utcnow()
    now_et = now_utc + timedelta(minutes=tz_offset_minutes)
    return now_et.strftime("%Y-%m-%d")

# Trade state
current = {
    "active": False, "pair": None, "direction": None, "expiry_min": 5,
    "ml_levels": [], "amount": base_amount, "anchor": None
}
last_signal_utc: Optional[datetime] = None
seen_ids = set()

daily_pnl = 0.0
halted_for_day = False
executor_busy = False

# keep scheduled tasks so we can cancel them on WIN
scheduled_tasks = []
def cancel_all_tasks():
    global scheduled_tasks
    for t in scheduled_tasks:
        if not t.done():
            t.cancel()
    scheduled_tasks = []

def reset_chain(reason=""):
    cancel_all_tasks()
    current.update({
        "active": False, "pair": None, "direction": None,
        "expiry_min": 5, "ml_levels": [],
        "amount": base_amount, "anchor": None
    })
    if reason:
        print(f"[RESET] {reason}")
    else:
        print("[RESET] Chain cleared.")

# track channel "WIN" pings as a fallback guard
last_win_ping_utc: Optional[datetime] = None
WIN_HINT_RE = re.compile(r'\bWIN\b|\bVICTORY\b', re.I)

# Executor helpers
PO_URL = "http://localhost:3000"

def force_otc(pair: str) -> str:
    return pair if (not FORCE_OTC or "OTC" in pair.upper()) else f"{pair} OTC"

async def executor_trade(pair, amount, direction, ml_tag) -> Dict:
    """Fire trade immediately; executor returns OPEN/PENDING. We log OPEN here."""
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

def quick_peek_profit() -> Optional[float]:
    """
    Try to read the latest closed profit quickly via Node /peek.
    If the endpoint isn't present, return None and rely on WIN ping fallback.
    """
    try:
        r = requests.get(f"{PO_URL}/peek", timeout=2.0)
        if r.status_code == 200:
            j = r.json()
            # expect shape: {"ok": true, "profit": 8.10}
            return float(j.get("profit", 0.0))
    except Exception:
        pass
    return None

def recent_win_ping(threshold_sec=8) -> bool:
    if not last_win_ping_utc:
        return False
    return (datetime.utcnow() - last_win_ping_utc).total_seconds() <= threshold_sec

# Core trade execution
async def run_one_trade(pair, direction, expiry_min, amount, ml_label=None) -> None:
    """Fire the click; we do not wait for result here (executor logs later)."""
    global executor_busy, daily_pnl, halted_for_day

    if DAILY_STOP_LOSS > 0 and daily_pnl <= -DAILY_STOP_LOSS:
        print("[HALT] Daily stop loss reached. Skip trade.")
        return

    if executor_busy:
        print("[BLOCK] Executor busy; skipping overlapping call.")
        return
    executor_busy = True

    clean_pair = force_otc(pair)
    ml_tag = "BASE" if not ml_label else f"ML{ml_label}"

    data = await asyncio.get_event_loop().run_in_executor(
        None, lambda: executor_trade(clean_pair, amount, direction, ml_tag)
    )
    # We expect result to be "PENDING"/"OPEN" here; log as OPEN (no chain decision now)
    result = data.get("result", "OPEN")
    profit = float(data.get("profit", 0))
    log_trade(clean_pair, direction, expiry_min, amount, result, profit, ml_tag)
    print(f"[API] Fired: {direction} {clean_pair} ${amount} [{ml_tag}] → {result}")

    executor_busy = False

    # If this was ML2 placement, free the chain
    if ml_label == 2:
        print("[DONE] ML2 placed. Freeing chain for next signal.")
        reset_chain("ML2 placed; chain released.")

    # If this was Base and no MLs are scheduled, free the chain to avoid permanent lock
    if ml_label is None and not current.get("ml_levels"):
        reset_chain("Base only signal completed; chain released.")

# Scheduling with just in time cancellation
async def schedule_leg(entry_dt: datetime, ml_label: Optional[int]):
    """Schedule Base, ML1, ML2 with SKEW and JIT cancel for MLs."""
    label = "BASE" if ml_label is None else f"ML{ml_label}"
    fire_dt = entry_dt - timedelta(milliseconds=SKEW_MS)
    now = datetime.utcnow()
    delay = max(0.0, (fire_dt - now).total_seconds())
    print(f"[SCHEDULE] {label} fire={fire_dt} (target={entry_dt}) delay={delay:.3f}s")
    try:
        # Wake slightly before fire to run JIT guard for ML1/ML2
        wake_early = max(0.0, delay - 0.9)
        await asyncio.sleep(wake_early)

        if ml_label in (1, 2):
            # JIT cancel: prefer PO peek, fallback to channel WIN ping
            p = quick_peek_profit()
            if p is not None:
                if p > 0:
                    reset_chain(f"{label} cancelled: previous leg WIN via /peek (profit {p})")
                    return
            else:
                if recent_win_ping(8):
                    reset_chain(f"{label} cancelled: WIN ping from channel")
                    return

        # Sleep remaining tiny slice to align to skewed time
        now2 = datetime.utcnow()
        rem = max(0.0, (fire_dt - now2).total_seconds())
        if rem:
            await asyncio.sleep(rem)

        # Place the leg
        amt = base_amount if ml_label is None else min(round(base_amount * (mg_mult ** ml_label), 2), MAX_STAKE)
        await run_one_trade(current["pair"], current["direction"], current["expiry_min"], amt, ml_label=ml_label)

    except asyncio.CancelledError:
        print(f"[CANCEL] {label} task cancelled.")
        return

# TTL watchdog to prevent stuck chains
async def _ttl_release(deadline_dt: datetime):
    try:
        # wait until the latest leg would finish, plus small buffer
        await asyncio.sleep(max(0, (deadline_dt - datetime.utcnow()).total_seconds() + 70))
        if current["active"]:
            reset_chain("TTL watchdog")
    except asyncio.CancelledError:
        pass

# Signal handling
async def handle_signal_from_text(text: str, msg_date=None):
    global last_signal_utc, daily_pnl, halted_for_day, current, scheduled_tasks, last_win_ping_utc

    # 1) WIN pings used for JIT cancel fallback
    if WIN_HINT_RE.search(text):
        last_win_ping_utc = datetime.utcnow()
        return False

    # 2) Trade signal
    sig = parse_signal(text)
    if not sig:
        return False
    if not msg_date:
        msg_date = datetime.utcnow().replace(tzinfo=timezone.utc)

    base_dt = resolve_entry_datetime(sig["entry_time"], msg_date.replace(tzinfo=None))
    if (datetime.utcnow() - base_dt).total_seconds() > 300:
        print(f"[INFO] Signal {sig['entry_time']} too old; ignoring.")
        return True

    # ET day rollover
    if not hasattr(handle_signal_from_text, "_day"):
        handle_signal_from_text._day = et_day_key()
    cur_day = et_day_key()
    if cur_day != handle_signal_from_text._day:
        daily_pnl = 0.0
        halted_for_day = False
        handle_signal_from_text._day = cur_day
        reset_chain("ET day rollover")

    if DAILY_STOP_LOSS > 0 and halted_for_day:
        print("[HALT] Stop loss hit; ignoring signals.")
        return True

    now_utc = datetime.utcnow()
    if last_signal_utc and (now_utc - last_signal_utc).total_seconds() < 60:
        print("[INFO] Rapid signal ignored.")
        return True
    if current["active"]:
        print("[INFO] Chain active; ignoring new signal.")
        return True

    pair = force_otc(sig["pair"])
    current.update({
        "active": True, "pair": pair, "direction": sig["direction"],
        "expiry_min": sig["expiry_min"], "ml_levels": sig.get("ml_levels", []),
        "amount": base_amount, "anchor": msg_date.replace(tzinfo=None)
    })
    last_signal_utc = now_utc
    print(f"[SIGNAL] {pair} {sig['direction']} {sig['expiry_min']}m entry {sig['entry_time']} | ML {sig.get('ml_levels', [])}")

    # Pre schedule Base, ML1, ML2
    scheduled_tasks = []
    t0 = asyncio.create_task(schedule_leg(base_dt, None)); scheduled_tasks.append(t0)

    latest_dt = base_dt
    if current["ml_levels"]:
        ml1_dt = resolve_entry_datetime(current["ml_levels"][0], msg_date.replace(tzinfo=None))
        t1 = asyncio.create_task(schedule_leg(ml1_dt, 1)); scheduled_tasks.append(t1)
        latest_dt = ml1_dt
    if len(current["ml_levels"]) > 1:
        ml2_dt = resolve_entry_datetime(current["ml_levels"][1], msg_date.replace(tzinfo=None))
        t2 = asyncio.create_task(schedule_leg(ml2_dt, 2)); scheduled_tasks.append(t2)
        latest_dt = ml2_dt

    # TTL watchdog to guarantee release even if no WIN ping or ML legs fail to fire
    ttl_task = asyncio.create_task(_ttl_release(latest_dt + timedelta(minutes=current["expiry_min"])))
    scheduled_tasks.append(ttl_task)

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

# Main
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
