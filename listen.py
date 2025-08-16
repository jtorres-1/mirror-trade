# listen.py — Telegram -> PocketOption with martingale
import os, re, csv, asyncio, sys
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

from po_executor import PocketOptionExecutor

load_dotenv()

# --- Env ---
api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")
phone = os.getenv("PHONE_NUMBER")
session_name = os.getenv("SESSION_NAME", "mirrortrade")
channel = os.getenv("CHANNEL")  # e.g. "@yousefftraderusa"

# Minutes to ADD to LOCAL to get ET (NY). Using abs so -180 or 180 both work.
tz_offset_minutes = abs(int(os.getenv("TZ_OFFSET_MIN", "180")))

FORCE_OTC = os.getenv("FORCE_OTC", "1") == "1"
base_amount = float(os.getenv("TRADE_AMOUNT", "1"))
mg_mult = float(os.getenv("MARTINGALE_MULT", "2.2"))
PO_EMAIL = os.getenv("PO_EMAIL")
PO_PASSWORD = os.getenv("PO_PASSWORD")
NO_BACKFILL = "--no-backfill" in sys.argv

# --- NEW: cap any single stake (max-stake guard) ---
MAX_STAKE = float(os.getenv("MAX_STAKE", "10.65"))

# --- NEW: daily stop-loss (0 disables) ---
DAILY_STOP_LOSS = float(os.getenv("DAILY_STOP_LOSS", "0"))

if channel and not channel.startswith("@"):
    channel = "@" + channel

# --- Clients ---
client = TelegramClient(session_name, api_id, api_hash)
executor = PocketOptionExecutor(PO_EMAIL, PO_PASSWORD, headless=False)

# --- Logging ---
LOG_FILE = "trade_log.csv"
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, "w", newline="") as f:
        csv.writer(f).writerow(["ts_utc","pair","direction","expiry_min","amount","result","profit"])

def log_trade(pair, direction, expiry_min, amount, result, profit):
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.utcnow().isoformat(), pair, direction, expiry_min, amount, result, profit
        ])

# --- Parsing ---
PAIR_RE = re.compile(r'([A-Z]{3}/[A-Z]{3})', re.I)
TIME_RE = re.compile(r'(\d{1,2}:\d{2})')
MIN_RE  = re.compile(r'(\d+)\s*m', re.I)
SUMMARY_MARKERS = ("REPORT","SESSION","FINISHED","ACCURACY","TESTIMONIAL","CONTACT SUPPORT","FOLLOW ME")

def looks_like_summary(text: str) -> bool:
    up = text.upper()
    return any(m in up for m in SUMMARY_MARKERS)

def parse_signal(text: str) -> Optional[Dict]:
    norm = text.replace('\u200b',' ')
    if looks_like_summary(norm):
        return None
    d = {"pair": None, "direction": None, "expiry_min": None, "entry_time": None, "ml_levels": []}
    m_pair = PAIR_RE.search(norm.upper())
    if m_pair:
        d["pair"] = m_pair.group(1)
    lines = [ln.strip() for ln in norm.splitlines() if ln.strip()]
    for ln in lines:
        up = ln.upper()
        if " BUY" in f" {up} " or up.endswith("BUY") or up.startswith("BUY"):
            d["direction"] = "BUY"
        if " SELL" in f" {up} " or up.endswith("SELL") or up.startswith("SELL"):
            d["direction"] = "SELL"
        if "EXPIRATION" in up:
            m = MIN_RE.search(up)
            if m: d["expiry_min"] = int(m.group(1))
        if "ENTRY" in up:
            m = TIME_RE.search(ln)
            if m: d["entry_time"] = m.group(1)
        if "LEVEL" in up:
            m = TIME_RE.search(ln)
            if m: d["ml_levels"].append(m.group(1))
    if d["entry_time"] is None:
        times = TIME_RE.findall(norm)
        if times:
            d["entry_time"] = times[0]
    if d["expiry_min"] is None:
        d["expiry_min"] = 5
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
    age_sec = (now - tgt).total_seconds()
    if 0 <= age_sec <= 300:
        return now
    if age_sec > 300:
        tgt += timedelta(days=1)
    return tgt

def entry_still_relevant(hhmm: str) -> bool:
    now = datetime.now()
    tgt_today = entry_local_from_et(hhmm)
    if (tgt_today - now).total_seconds() >= -300:
        return True
    tgt_tomorrow = tgt_today + timedelta(days=1)
    return (tgt_tomorrow - now).total_seconds() <= 12 * 3600

def et_day_key() -> str:
    now_local = datetime.now()
    now_et = now_local + timedelta(minutes=tz_offset_minutes)
    return now_et.strftime("%Y-%m-%d")

# --- Trade state ---
current = {"active": False,"pair": None,"direction": None,"expiry_min": 5,
           "ml_levels": [],"ml_i": 0,"amount": base_amount}
last_signal_utc: Optional[datetime] = None
seen_ids = set()  # NEW

# --- Daily PnL + halt flag ---
daily_pnl = 0.0
halted_for_day = False

async def sleep_until(when: datetime):
    delay = max(0, (when - datetime.now()).total_seconds())
    await asyncio.sleep(delay)

async def run_one_trade(pair: str, direction: str, expiry_min: int, amount: float):
    clean_pair = pair
    if FORCE_OTC and "OTC" not in clean_pair.upper():
        clean_pair = f"{clean_pair} OTC"
    await executor.select_pair(clean_pair)
    await executor.place_trade(clean_pair, direction, expiry_min, amount)
    await asyncio.sleep(expiry_min * 60 + 8)
    profit = await executor.last_closed_profit()
    won = profit > 0.0
    log_trade(clean_pair, direction, expiry_min, amount, "WIN" if won else "LOSS", profit)
    print(f"[RESULT] {clean_pair} {direction} amount={amount} profit={profit} {'WIN' if won else 'LOSS'}")
    global daily_pnl, halted_for_day
    daily_pnl += profit
    if DAILY_STOP_LOSS > 0 and daily_pnl <= -DAILY_STOP_LOSS and not halted_for_day:
        halted_for_day = True
        print(f"[HALT] Daily stop-loss hit: PnL {daily_pnl:.2f} ≤ -{DAILY_STOP_LOSS:.2f}. Halting for today (ET).")
    return won

async def schedule_entry(entry_time: str):
    if DAILY_STOP_LOSS > 0 and halted_for_day:
        print("[HALT] Daily stop-loss reached; skip scheduled entry.")
        return
    tgt_local = to_next_or_now(entry_time)
    print(f"[TIME] ET {entry_time} -> local {tgt_local.strftime('%Y-%m-%d %H:%M:%S')}  now {datetime.now()}")
    await sleep_until(tgt_local)
    pair = current["pair"]; direction = current["direction"]; expiry = current["expiry_min"]
    amt = min(current["amount"], MAX_STAKE)
    if amt != current["amount"]:
        print(f"[CAP] Amount capped to {amt} (MAX_STAKE={MAX_STAKE})")
    print(f"[EXECUTE] {pair} {direction} {expiry}m amount {amt} @ {datetime.now().strftime('%H:%M:%S')}")
    won = await run_one_trade(pair, direction, expiry, amt)
    if won:
        current.update({"active":False,"pair":None,"direction":None,"ml_levels":[],"ml_i":0,"amount":base_amount})
        return
    if current["ml_i"] < len(current["ml_levels"]):
        if DAILY_STOP_LOSS > 0 and halted_for_day:
            print("[HALT] Stop-loss reached; aborting remaining ML steps.")
            current.update({"active":False,"pair":None,"direction":None,"ml_levels":[],"ml_i":0,"amount":base_amount})
            return
        next_t = current["ml_levels"][current["ml_i"]]; current["ml_i"] += 1
        next_amt = round(current["amount"] * mg_mult, 2)
        current["amount"] = min(next_amt, MAX_STAKE)
        if current["amount"] < next_amt:
            print(f"[CAP] ML amount capped to {current['amount']} (MAX_STAKE={MAX_STAKE})")
        print(f"[ML] Scheduling ML{current['ml_i']} at {next_t} amount={current['amount']}")
        await schedule_entry(next_t)
    else:
        print("[ML] No ML levels left. Chain ends.")
        current.update({"active":False,"pair":None,"direction":None,"ml_levels":[],"ml_i":0,"amount":base_amount})

# --- Telegram handlers ---
async def handle_signal_from_text(text: str, msg_date=None):
    global last_signal_utc, daily_pnl, halted_for_day
    sig = parse_signal(text)
    if not sig:
        return False
    # NEW: stale message guard
    if msg_date:
        msg_age = (datetime.utcnow() - msg_date.replace(tzinfo=None)).total_seconds()
        if msg_age > 120:
            print(f"[INFO] Stale message (age {msg_age:.1f}s) ignored.")
            return True
    if not hasattr(handle_signal_from_text, "_day"):
        handle_signal_from_text._day = et_day_key()
    cur_day = et_day_key()
    if cur_day != handle_signal_from_text._day:
        daily_pnl = 0.0
        halted_for_day = False
        handle_signal_from_text._day = cur_day
        print(f"[INFO] New ET day {cur_day}: daily PnL reset.")
    if DAILY_STOP_LOSS > 0 and halted_for_day:
        print("[HALT] Daily stop-loss reached; ignoring signals until ET midnight.")
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
    print(f"[SIGNAL] {pair} {sig['direction']} {sig['expiry_min']}m entry {sig['entry_time']} | ML {current['ml_levels']}")
    asyncio.create_task(schedule_entry(sig["entry_time"]))
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
    if not ok:
        print("[TG DEBUG] Ignored: no valid signal found")

async def backfill_latest(entity):
    async for msg in client.iter_messages(entity, limit=20):
        if not msg.message:
            continue
        text = msg.message.strip()
        if await handle_signal_from_text(text, msg_date=msg.date):
            break

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
    client.add_event_handler(on_signal, events.MessageEdited(chats=entity))
    print("[DEBUG] Launching PocketOptionExecutor...")
    await executor.launch(); await executor.login(); await executor.goto_trade()
    print("[DEBUG] Pocket Option trade screen ready.")
    if not NO_BACKFILL:
        print("[DEBUG] Backfilling recent messages…")
        await backfill_latest(entity)
    await client.run_until_disconnected()

if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
