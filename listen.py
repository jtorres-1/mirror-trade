# listen.py — Telegram -> PocketOption with martingale
# Final Build: Sequential ML controller with fast recovery (~2s delay)
# Cancel shield + chain_id for isolation

import os, re, csv, asyncio, sys, requests, uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
import emoji

load_dotenv()

# --- Env ---
api_id = int(os.getenv("API_ID", "0"))
api_hash = os.getenv("API_HASH", "")
phone = os.getenv("PHONE_NUMBER")
session_name = os.getenv("SESSION_NAME", "mirrortrade")
channel = os.getenv("CHANNEL")

tz_offset_minutes = -int(os.getenv("TZ_OFFSET_MIN", "240"))
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

# --- Telegram client ---
client = TelegramClient(session_name, api_id, api_hash)

# --- Logging ---
LOG_FILE = "trade_log.csv"
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, "w", newline="") as f:
        csv.writer(f).writerow(
            ["ts_utc","pair","direction","expiry_min","amount","result","profit","ml_tag","chain_id"]
        )

def log_trade(pair, direction, expiry_min, amount, result, profit, ml_tag="", chain_id=""):
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.utcnow().isoformat(), pair, direction, expiry_min,
            amount, result, profit, ml_tag, chain_id
        ])

# --- Parsing ---
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
    if looks_like_summary(norm): return None
    d = {"pair": None, "direction": None, "expiry_min": None,
         "entry_time": None, "ml_levels": []}
    m_pair = PAIR_RE.search(norm.upper())
    if m_pair: d["pair"] = m_pair.group(1)
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
    if d["expiry_min"] is None: d["expiry_min"] = 5
    if d["pair"] and d["direction"] and d["entry_time"]: return d
    return None

# --- Time handling ---
def resolve_entry_datetime(hhmm: str, msg_date_utc: datetime) -> datetime:
    hh, mm = map(int, hhmm.split(":"))
    msg_et = msg_date_utc + timedelta(minutes=tz_offset_minutes)
    candidate = msg_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if (candidate - msg_et).total_seconds() < -6*3600:
        candidate += timedelta(days=1)
    elif (candidate - msg_et).total_seconds() > 18*3600:
        candidate -= timedelta(days=1)
    return candidate - timedelta(minutes=tz_offset_minutes)

def et_day_key() -> str:
    now_et = datetime.utcnow() + timedelta(minutes=tz_offset_minutes)
    return now_et.strftime("%Y-%m-%d")

# --- Trade state ---
current = {"active": False,"pair": None,"direction": None,"expiry_min": 5,
           "ml_levels": [],"ml_i": 0,"amount": base_amount,"chain_id": None}
last_signal_utc: Optional[datetime] = None
seen_ids = set()

daily_pnl = 0.0
halted_for_day = False
executor_busy = False
chain_active = True

async def sleep_until(when: datetime, pad_sec: float = 0.0):
    delay = max(0, (when - datetime.utcnow()).total_seconds() + pad_sec)
    await asyncio.sleep(delay)

# --- Run one trade ---
async def run_one_trade(pair, direction, expiry_min, amount, ml_label=None, chain_id="") -> bool:
    global executor_busy, daily_pnl, halted_for_day, chain_active, current
    if not chain_active:
        print(f"[CANCEL] Chain inactive, aborting {ml_label or 'BASE'} trade.")
        return False
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
            json={"pair": clean_pair, "amount": amount,
                  "direction": direction.lower(), "ml_tag": ml_tag,
                  "chain_id": chain_id},
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
    log_trade(clean_pair, direction, expiry_min, amount, result, profit, ml_tag, chain_id)
    daily_pnl += profit
    if DAILY_STOP_LOSS > 0 and daily_pnl <= -DAILY_STOP_LOSS:
        halted_for_day = True
        print(f"[HALT] Daily stop-loss reached. PnL={daily_pnl:.2f}, halting new trades.")
    print(f"[API] Trade done: {direction} {clean_pair} ${amount} → {result} ({profit}) [{ml_tag}] [chain={chain_id}]")
    if success:
        print("[CANCEL] WIN detected — chain ends here.")
        chain_active = False
        current.update({"active": False,"pair": None,"direction": None,
                        "ml_levels": [],"ml_i": 0,"amount": base_amount,"chain_id": None})
    return success

# --- Sequential ML controller ---
async def run_chain(entry_dt: datetime):
    global current, chain_active
    await sleep_until(entry_dt)
    amt = min(current["amount"], MAX_STAKE)
    print(f"[EXECUTE] BASE {current['pair']} {current['direction']} {amt} [chain={current['chain_id']}]")
    win = await run_one_trade(current["pair"], current["direction"], current["expiry_min"], amt, ml_label=None, chain_id=current["chain_id"])
    if win: return
    while chain_active and current["ml_i"] < len(current["ml_levels"]):
        current["ml_i"] += 1
        if current["ml_i"] >= 3:
            print("[ML] ML3 disabled; stopping at ML2.")
            break
        amt = round(min(current["amount"] * mg_mult, MAX_STAKE), 2)
        current["amount"] = amt
        # Fire ~2s after prior close, not at Lorenzo's clock
        next_dt = datetime.utcnow() + timedelta(seconds=2)
        print(f"[EXECUTE] ML{current['ml_i']} {current['pair']} {current['direction']} {amt} at {next_dt} [chain={current['chain_id']}]")
        await sleep_until(next_dt)
        win = await run_one_trade(current["pair"], current["direction"], current["expiry_min"], amt, ml_label=current["ml_i"], chain_id=current["chain_id"])
        if win: break
    chain_active = False
    current.update({"active": False,"pair": None,"direction": None,
                    "ml_levels": [],"ml_i": 0,"amount": base_amount,"chain_id": None})

# --- Telegram handlers ---
async def handle_signal_from_text(text: str, msg_date=None):
    global last_signal_utc, daily_pnl, halted_for_day, chain_active, current
    sig = parse_signal(text)
    if not sig: return False
    if not msg_date: msg_date = datetime.utcnow().replace(tzinfo=timezone.utc)
    entry_dt = resolve_entry_datetime(sig["entry_time"], msg_date.replace(tzinfo=None))
    if (datetime.utcnow() - entry_dt).total_seconds() > 300:
        print(f"[INFO] Signal entry {sig['entry_time']} too old; ignoring.")
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
    chain_active = True
    chain_id = uuid.uuid4().hex
    current.update({"active": True,"pair": sig["pair"],"direction": sig["direction"],
                    "expiry_min": sig["expiry_min"],"ml_levels": sig.get("ml_levels", []),
                    "ml_i": 0,"amount": base_amount,"chain_id": chain_id})
    last_signal_utc = now_utc
    print(f"[SIGNAL] {sig['pair']} {sig['direction']} {sig['expiry_min']}m entry {sig['entry_time']} | ML {sig.get('ml_levels', [])} [chain={chain_id}]")
    asyncio.create_task(run_chain(entry_dt))
    return True

async def on_signal(e):
    if e.message.id in seen_ids: return
    seen_ids.add(e.message.id)
    text = (e.message.message or "").strip()
    await handle_signal_from_text(text, msg_date=e.message.date)

# --- Main ---
async def main():
    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(phone)
        code = input("Enter the Telegram code: ").strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pw = input("Enter your Telegram 2FA password: ").strip()
            await client.sign_in(password=pw)
    entity = await client.get_entity(channel)
    client.add_event_handler(on_signal, events.NewMessage(chats=entity))
    await client.run_until_disconnected()

if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
