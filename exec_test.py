# exec_test.py — Async test: schedule one trade and log result
import os, csv, asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from po_executor import PocketOptionExecutor

load_dotenv()

# ---- Config from env (with defaults) ----
pair         = os.getenv("TEST_PAIR",   "EUR/USD")
direction    = os.getenv("TEST_SIDE",   "BUY").upper()  # BUY or SELL
expiry_min   = int(os.getenv("TEST_EXPIRY", "5"))
amount       = float(os.getenv("TEST_AMOUNT", "1"))
force_otc    = os.getenv("FORCE_OTC", "1") == "1"
email        = os.getenv("PO_EMAIL")
password     = os.getenv("PO_PASSWORD")
tz_offset    = int(os.getenv("TZ_OFFSET_MIN", "0"))

assert email and password, "Add PO_EMAIL and PO_PASSWORD in .env"

# schedule 2 minutes from now by default
entry_time = (datetime.now() + timedelta(minutes=2)).strftime("%H:%M")
entry_time = os.getenv("TEST_ENTRY", entry_time)  # allow override via env

def to_next_timestamp(hhmm: str) -> datetime:
    now = datetime.now()
    hh, mm = map(int, hhmm.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if tz_offset:
        target += timedelta(minutes=tz_offset)
    if target <= now:
        target += timedelta(days=1)
    return target

def log_trade(row):
    path = "trade_history.csv"
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "pair", "direction", "expiry_min", "amount", "entry_time", "profit", "result"])
        w.writerow(row)

async def main():
    global pair
    if force_otc and "OTC" not in pair.upper():
        pair = f"{pair} OTC"

    signal = {
        "pair": pair,
        "direction": direction,
        "expiry_min": expiry_min,
        "amount": amount,
        "entry_time": entry_time,
    }
    print(f"[TEST] Injected signal: {signal}")

    bot = PocketOptionExecutor(email, password, headless=False, storage_state="po_storage.json")
    await bot.launch()
    await bot.login()
    await bot.goto_trade()

    try:
        when = to_next_timestamp(entry_time)
        delay = max(0, (when - datetime.now()).total_seconds())
        print(f"[WAITING] Until {when.strftime('%H:%M:%S')} local (in {int(delay)}s) …")
        await asyncio.sleep(delay)

        await bot.place_trade(pair, direction, expiry_min, amount)
        print(f"[EXECUTED] {direction} on {pair} for {expiry_min}m amount {amount}")

        # Wait for close and read result — buffer to ensure result is visible
        await asyncio.sleep(expiry_min * 60 + 15)
        try:
            profit = await bot.last_closed_profit()
            result = "WIN" if profit > 0.0 else "LOSS"
        except Exception as e:
            print(f"[WARN] Could not read profit: {e}")
            profit, result = "", "UNKNOWN"

        log_trade([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            pair, direction, expiry_min, amount, entry_time, profit, result
        ])
        print(f"[RESULT] profit={profit} -> {result}")

    finally:
        try:
            await bot.close()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(main())
