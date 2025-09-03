# listen.py — Telegram -> PocketOption with martingale (airtight, safe executor, no duplicate trades)
import os, re, csv, asyncio, sys, requests
from datetime import datetime, timedelta
from typing import Optional, Dict
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
import emoji  # <-- added

load_dotenv()

# --- Env ---
api_id = int(os.getenv("API_ID", "0"))
api_hash = os.getenv("API_HASH", "")
phone = os.getenv("PHONE_NUMBER")
session_name = os.getenv("SESSION_NAME", "mirrortrade")
channel = os.getenv("CHANNEL")

tz_offset_minutes = abs(int(os.getenv("TZ_OFFSET_MIN", "180")))
FORCE_OTC = os.getenv("FORCE_OTC", "1") == "1"
base_amount = float(os.getenv("TRADE_AMOUNT", "1"))
mg_mult = float(os.getenv("MARTINGALE_MULT", "2.2"))
MAX_STAKE = float(os.getenv("MAX_STAKE", "10.65"))
DAILY_STOP_LOSS = float(os.getenv("DAILY_STOP_LOSS", "0"))

if not api_id or not api_hash:
    print("[FATAL] API_ID/API_HASH missing in .env")
    sys.exit(1)

if not channel:
    print("[FATAL] CHANNEL missing in .env")
    sys.exit(1)

if channel and not channel.startswith("@"):
    channel = "@" + channel

# --- Initialize Telegram client (user session; no bot token) ---
client = TelegramClient(session_name, api_id, api_hash)

# --- Logging ---
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

# --- Parsing ---
PAIR_RE = re.compile(r'([A-Z]{3}/[A-Z]{3})', re.I)
TIME_RE = re.compile(r'(\d{1,2}:\d{2})')
MIN_RE  = re.compile(r'(\d+)\s*m', re.I)
SUMMARY_MARKERS = ("REPORT","SESSION","FINISHED","ACCURACY","TESTIMONIAL","CONTACT SUPPORT","FOLLOW ME")

def looks_like_summary(text: str) -> bool:
    up = text.upper()
    return any(m in up for m in SUMMARY_MARKERS)

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
    if m_pair:
        d["pair"] = m_pair.group(1)

    lines = [ln.strip() for ln in norm.splitlines() if ln.strip()]
    for ln in lines:
        up = ln.upper()
        if "BUY" in up: d["direction"] = "BUY"
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

    if d["entry_time"] is None:
        times = TIME_RE.findall(norm)
        if times: d["entry_time"] = times[0]
    if d["expiry_min"] is None: d["expiry_min"] = 5

    if d["pair"] and d["direction"] and d["entry_time"]:
        return d
    return None

# --- Time handling ---
def entry_local_from_et(hhmm: str) -> datetime:
    hh, mm = map(int, hhmm.split(":"))
    now_local = datetime.now()
    now_et = now_local + timedelta(minutes=tz_offset_minutes)
    entry_et_today = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return entry_et_today - timedelta(minutes=tz_offset_minutes)

def to_next_or_now(hhmm: str) -> datetime:
    now = datetime.now()
    tgt = entry_local_from_et(hhmm)
    diff = (tgt - now).total_seconds()
    if -300 <= diff <= 300:  # allow ±5 min window
        return now
    if diff < -300:
        tgt += timedelta(days=1)
    return tgt

def entry_still_relevant(hhmm: str) -> bool:
    now = datetime.now()
    tgt = entry_local_from_et(hhmm)
    diff = (tgt - now).total_seconds()
    # Accept if signal is within 5m past or up to 10m future
    if -300 <= diff <= 600:
        return True
    return False

def et_day_key() -> str:
    now_local = datetime.now()
    now_et = now_local + timedelta(minutes=tz_offset_minutes)
    return now_et.strftime("%Y-%m-%d")

# --- Trade state ---
current = {"active": False,"pair": None,"direction": None,"expiry_min": 5,
           "ml_levels": [],"ml_i": 0,"amount": base_amount}
last_signal_utc: Optional[datetime] = None
seen_ids = set()
scheduled_tasks = []

daily_pnl = 0.0
halted_for_day = False
executor_busy = False

async def sleep_until(when: datetime):
    delay = max(0, (when - datetime.now()).total_seconds())
    await asyncio.sleep(delay)

# --- Run one trade via Node executor ---
async def run_one_trade(pair, direction, expiry_min, amount, ml_label=None) -> bool:
    global executor_busy, daily_pnl, halted_for_day
    if executor_busy:
        print("[BLOCK] Executor busy, skipping duplicate call.")
        return False
    executor_busy = True
    clean_pair = pair
    if FORCE_OTC and "OTC" not in clean_pair.upper():
        clean_pair = f"{clean_pair} OTC"
    ml_tag = f"ML{ml_label}" if ml_label else "BASE"
    success, result, profit = False, "ERROR", 0.0
    try:
        res = requests.post(
            "http://localhost:3000/trade",
            json={"pair": clean_pair, "amount": amount, "direction": direction.lower(), "ml_tag": ml_tag},
            timeout=400
        )
        if res.status_code == 200:
            data = res.json()
            result = data.get("result", "LOSS")
            profit = float(data.get("profit", 0))
            if profit <= 0: result = "LOSS"
            success = (result == "WIN")
        else:
            print(f"[API ERROR] {res.status_code}: {res.text}")
    except Exception as e:
        print(f"[API EXCEPTION] {e}")
    finally:
        executor_busy = False
    log_trade(clean_pair, direction, expiry_min, amount, result, profit, ml_tag)
    daily_pnl += profit
    if DAILY_STOP_LOSS > 0 and daily_pnl <= -DAILY_STOP_LOSS:
        halted_for_day = True
        print(f"[HALT] Daily stop-loss reached. PnL={daily_pnl:.2f}, halting new trades.")
    print(f"[API] Trade done: {direction} {clean_pair} ${amount} → {result} ({profit}) [{ml_tag}]")
    return success

# --- ML scheduling ---
async def schedule_entry(entry_time: str, ml_label=None):
    global current, scheduled_tasks
    if DAILY_STOP_LOSS > 0 and halted_for_day:
        print("[HALT] Daily stop-loss reached; skip scheduled entry.")
        return
    tgt_local = to_next_or_now(entry_time)
    print(f"[TIME] ET {entry_time} -> local {tgt_local.strftime('%Y-%m-%d %H:%M:%S')} now {datetime.now()}")
    await sleep_until(tgt_local)
    pair, direction, expiry = current["pair"], current["direction"], current["expiry_min"]
    amt = min(current["amount"], MAX_STAKE)
    label_str = f"ML{ml_label}" if ml_label else "BASE"
    print(f"[EXECUTE] {pair} {direction} {expiry}m amount {amt} ({label_str}) @ {datetime.now().strftime('%H:%M:%S')}")
    won = await run_one_trade(pair, direction, expiry, amt, ml_label=ml_label)
    if won:
        print("[ML] WIN → cancelling pending ML tasks and resetting to base")
        for t in scheduled_tasks:
            if not t.done(): t.cancel()
        scheduled_tasks.clear()
        current.update({"active": False,"pair": None,"direction": None,
                        "ml_levels": [],"ml_i": 0,"amount": base_amount})
        return
    if current["ml_i"] < len(current["ml_levels"]):
        next_t = current["ml_levels"][current["ml_i"]]
        current["ml_i"] += 1
        if current["ml_i"] >= 3:
            print("[ML] ML3 disabled; chain ends at ML2.")
            current.update({"active": False,"pair": None,"direction": None,
                            "ml_levels": [],"ml_i": 0,"amount": base_amount})
            return
        next_amt = round(current["amount"] * mg_mult, 2)
        current["amount"] = min(next_amt, MAX_STAKE)
        if current["amount"] < next_amt:
            print(f"[CAP] ML amount capped to {current['amount']} (MAX_STAKE={MAX_STAKE})")
        print(f"[ML] LOSS → scheduling ML{current['ml_i']} at {next_t} amount={current['amount']}")
        task = asyncio.create_task(schedule_entry(next_t, ml_label=current["ml_i"]))
        scheduled_tasks.append(task)
    else:
        print("[ML] LOSS but no ML levels left. Resetting.")
        current.update({"active": False,"pair": None,"direction": None,
                        "ml_levels": [],"ml_i": 0,"amount": base_amount})

# --- Telegram handlers ---
async def handle_signal_from_text(text: str, msg_date=None):
    global last_signal_utc, daily_pnl, halted_for_day
    sig = parse_signal(text)
    if not sig: return False
    if msg_date:
        msg_age = (datetime.utcnow() - msg_date.replace(tzinfo=None)).total_seconds()
        if msg_age > 120:
            print(f"[INFO] Stale message (age {msg_age:.1f}s) ignored.")
            return True
    if not hasattr(handle_signal_from_text, "_day"):
        handle_signal_from_text._day = et_day_key()
    cur_day = et_day_key()
    if cur_day != handle_signal_from_text._day:
        daily_pnl = 0.0; halted_for_day = False
        handle_signal_from_text._day = cur_day
        print(f"[INFO] New ET day {cur_day}: daily PnL reset.")
    if DAILY_STOP_LOSS > 0 and halted_for_day:
        print("[HALT] Daily stop-loss reached; ignoring signals.")
        return True
    now_utc = datetime.utcnow()
    if last_signal_utc and (now_utc - last_signal_utc).total_seconds() < 60:
        print("[INFO] Duplicate/rapid signal ignored.")
        return True
    if current["active"]:
        print("[INFO] Chain active; ignoring new signal.")
        return True
    if not entry_still_relevant(sig["entry_time"]):
        print(f"[INFO] Signal entry {sig['entry_time']} too old; ignoring.")
        return True
    pair = sig["pair"]
    if FORCE_OTC and "OTC" not in pair.upper():
        pair = f"{pair} OTC"
    current.update({"active": True,"pair": pair,"direction": sig["direction"],
                    "expiry_min": sig["expiry_min"],"ml_levels": sig.get("ml_levels", []),
                    "ml_i": 0,"amount": base_amount})
    last_signal_utc = now_utc
    print(f"[SIGNAL] {pair} {sig['direction']} {sig['expiry_min']}m entry {sig['entry_time']} | ML {sig.get('ml_levels', [])}")
    task = asyncio.create_task(schedule_entry(sig["entry_time"]))
    scheduled_tasks.append(task)
    return True

async def on_signal(e):
    if e.message.id in seen_ids:
        print(f"[INFO] Duplicate message ID {e.message.id} ignored.")
        return
    seen_ids.add(e.message.id)
    src = getattr(getattr(e, "chat", None), "title", None) or ""
    username = getattr(getattr(e, "chat", None), "username", None)
    print(f"[TG DEBUG] Incoming from: '{src}' (@{username})")
    text = (e.message.message or "").strip()
    print("[TG RAW]", text.replace("\n", " | ")[:500])
    ok = await handle_signal_from_text(text, msg_date=e.message.date)
    if not ok: print("[TG DEBUG] Ignored: no valid signal found")

# --- Main ---
async def main():
    print("[DEBUG] Starting Telegram client...")
    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(phone)
        code = input("Enter the Telegram code: ").strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pw = input("Enter your Telegram 2FA password: ").strip()
            await client.sign_in(password=pw)
    me = await client.get_me()
    print(f"[DEBUG] Logged in as: {me.username or me.first_name} (ID {me.id})")
    entity = await client.get_entity(channel)
    print(f"[DEBUG] Listening to: {getattr(entity, 'title', None)} (ID {entity.id})")
    client.add_event_handler(on_signal, events.NewMessage(chats=entity))
    print("[DEBUG] Pocket Option trade screen ready (via Node API).")
    await client.run_until_disconnected()

if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
