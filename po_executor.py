import re
from typing import Union
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

PO_URL_LOGIN   = "https://pocketoption.com/en/login/"
PO_URL_CABINET = "https://pocketoption.com/en/cabinet/"
PO_URL_TRADE   = "https://pocketoption.com/en/cabinet/demo-quick-high-low/"

SEL = {
    "amount_box": "#put-call-buttons-chart-1 >> role=textbox",
    "expiry_5m_chip": ':text(":05:00")',
    "buy_link": 'a:has-text("Buy")',
    "sell_link": 'a:has-text("Sell")',
    "asset_open": ".pair-number-wrap",
    "assets_modal": "#modal-root .drop-down-modal-wrap.active",
    "assets_search": 'input[placeholder*="Search"]',
}

DEFAULT_TIMEOUT = 60_000

class PocketOptionExecutor:
    def __init__(self, email: str, password: str, headless: bool = False, storage_state: str = "po_storage.json"):
        self.email = email
        self.password = password
        self.headless = headless
        self.storage_state = storage_state
        self.p = None
        self.browser = None
        self.ctx = None
        self.page = None

    async def launch(self):
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(headless=self.headless)
        try:
            self.ctx = await self.browser.new_context(
                storage_state=self.storage_state,
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/121.0.0.0 Safari/537.36")
            )
        except Exception:
            self.ctx = await self.browser.new_context()
        self.page = await self.ctx.new_page()
        self.page.set_default_timeout(DEFAULT_TIMEOUT)

    async def close(self):
        try:
            if self.ctx:
                await self.ctx.storage_state(path=self.storage_state)
        except Exception:
            pass
        if self.browser:
            await self.browser.close()
        if self.p:
            await self.p.stop()

    async def login(self):
        await self.page.goto(PO_URL_CABINET, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        if "cabinet" in self.page.url:
            return
        await self.page.goto(PO_URL_LOGIN, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        try:
            await self.page.get_by_title("Close").click(timeout=3000)
        except Exception:
            pass
        await self.page.get_by_role("textbox", name="Email *").fill(self.email)
        await self.page.get_by_role("textbox", name="Password *").fill(self.password)
        await self.page.get_by_role("button", name="Sign In").click()
        print("\nSolve the CAPTCHA in the opened browser and finish login")

    async def goto_trade(self):
        await self.page.goto(PO_URL_TRADE, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        await self._dismiss_popups()  # ← added

    # --- NEW: minimal popup killer ---
    async def _dismiss_popups(self):
        """Close reward/news/cookie modals or toasts that can block clicks."""
        # try ESC once (many modals close on Escape)
        try:
            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(120)
        except Exception:
            pass

        selectors = [
            '#modal-root [data-test="close"]',
            '#modal-root [title="Close"]',
            '#modal-root button:has-text("Close")',
            '#modal-root button:has-text("OK")',
            '#modal-root button:has-text("Got it")',
            '.modal .icon-close, .modal .close, .popup .close',
            '.toast-close, .s-alert-close, [aria-label="Close"]',
        ]
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                if await loc.count():
                    await loc.first.click(timeout=600)
                    await self.page.wait_for_timeout(120)
            except Exception:
                continue

    async def _ensure_assets_modal_closed(self):
        try:
            await self.page.wait_for_selector(SEL["assets_modal"], state="hidden", timeout=1000)
        except Exception:
            try:
                await self.page.keyboard.press("Escape")
                await self.page.wait_for_selector(SEL["assets_modal"], state="hidden", timeout=2000)
            except Exception:
                pass

    async def _open_closed_trades(self):
        try:
            await self.page.click('text=Trades', timeout=4000)
        except Exception:
            pass
        try:
            await self.page.get_by_text("Closed", exact=True).click(timeout=3000)
        except Exception:
            try:
                await self.page.get_by_role("tab", name="Closed").click(timeout=3000)
            except Exception:
                pass
        await self.page.wait_for_timeout(700)

    async def select_pair(self, pair: str, max_retries: int = 3):
        """
        Type the base pair only (e.g., 'EUR/JPY' or 'EURJPY'), then click the '… OTC' row.
        Verifies header shows '<BASE> OTC'. Retries up to max_retries, then raises.
        """
        base = pair.upper().replace(" OTC", "").strip()
        expected = f"{base} OTC"
        keyword = base.replace("/", "")

        for attempt in range(1, max_retries + 1):
            await self.page.click(SEL["asset_open"], timeout=DEFAULT_TIMEOUT)
            modal = self.page.locator(SEL["assets_modal"])
            await modal.wait_for(state="visible", timeout=5000)

            try:
                search = modal.get_by_placeholder("Search")
            except Exception:
                search = modal.locator(SEL["assets_search"])

            await search.click()
            try:
                await self.page.keyboard.press("Meta+A")
                await self.page.keyboard.press("Backspace")
            except Exception:
                pass
            await search.fill(keyword)
            await self.page.wait_for_timeout(350)

            clicked = False
            try:
                await modal.get_by_text(expected, exact=False).first.click(timeout=1200)
                clicked = True
            except Exception:
                try:
                    await modal.get_by_text(base, exact=False).first.click(timeout=1200)
                    clicked = True
                except Exception:
                    clicked = False

            try:
                await self.page.wait_for_selector(SEL["assets_modal"], state="hidden", timeout=1500)
            except Exception:
                try:
                    await self.page.keyboard.press("Enter")
                except Exception:
                    pass
                await self.page.keyboard.press("Escape")
                await self.page.wait_for_selector(SEL["assets_modal"], state="hidden", timeout=2500)

            await self.page.wait_for_timeout(400)

            active = (await self.page.locator(SEL["asset_open"]).inner_text()).strip().upper()
            if clicked and base in active and "OTC" in active:
                return
            print(f"[WARN] Active pair '{active}' != '{expected}' (try {attempt}/{max_retries})")

        raise PWTimeout(f"Could not select pair '{expected}' after {max_retries} attempts")

    # -------- FIXED: robust amount setter (Mac-friendly & verified) --------
    async def set_amount(self, amount: Union[float, int]):
        await self._ensure_assets_modal_closed()
        amt = self.page.locator(SEL["amount_box"])
        await amt.wait_for(state="visible", timeout=5000)

        want = f"{float(amount):.2f}".rstrip("0").rstrip(".")

        for _ in range(3):
            try:
                await amt.fill("")          # hard clear
                await amt.fill(want)        # set
                await self.page.wait_for_timeout(120)

                cur = await amt.input_value()
                if cur == want or cur == f"{float(amount):.2f}":
                    return
            except Exception:
                pass
            # fallback: try select-all on both Mac/Win
            try:
                await amt.click()
                for combo in ("Meta+A", "Control+A"):
                    try:
                        await self.page.keyboard.press(combo)
                        await self.page.keyboard.press("Backspace")
                    except Exception:
                        pass
                await amt.fill(want)
            except Exception:
                pass

        cur = await amt.input_value()
        raise RuntimeError(f"Amount set failed: wanted {want}, got {cur}")

    async def set_expiry(self, minutes: int):
        if minutes == 5:
            try:
                await self.page.locator(SEL["expiry_5m_chip"]).click(timeout=3000)
                return
            except Exception:
                pass
        try:
            await self.page.click('[data-test="expiry-dropdown"]', timeout=3000)
            if minutes == 5:
                try:
                    await self.page.click('text="5 min"', timeout=2000)
                    return
                except Exception:
                    return
            else:
                await self.page.click(f'text="{minutes} min"', timeout=3000)
        except Exception:
            if minutes != 5:
                raise

    async def click_direction(self, direction: str):
        d = direction.upper()
        try:
            if d == "BUY":
                await self.page.locator(SEL["buy_link"]).first.click(timeout=3000)
                return
            elif d == "SELL":
                await self.page.locator(SEL["sell_link"]).first.click(timeout=3000)
                return
            else:
                raise ValueError(f"Unknown direction {direction}")
        except Exception:
            pass
        try:
            await self.page.get_by_text(d, exact=False).first.click(timeout=3000)
            return
        except Exception as e:
            raise PWTimeout(f"Could not click {d} button") from e

    async def place_trade(self, pair: str, direction: str, expiry_min: int, amount: Union[float, int]):
        await self.goto_trade()
        await self._dismiss_popups()  # ← added
        await self.select_pair(pair)
        await self.set_amount(amount)
        await self.set_expiry(expiry_min)
        await self._dismiss_popups()  # ← added
        await self.click_direction(direction)

    async def last_closed_profit(self) -> float:
        await self._open_closed_trades()
        row = self.page.locator('.deals-list__item-first, [data-test="trades-closed"] [class*="row"], .table-row').first
        txt = await row.inner_text(timeout=3000)
        amounts = re.findall(r'([+\-]?\s*\$?\s*\d+(?:[\.,]\d{1,2})?)', txt)
        if not amounts:
            raise RuntimeError(f"No profit figure found in closed row text: {txt!r}")
        raw = amounts[-1]
        raw = (raw.replace(" ", "")
               .replace("$", "")
               .replace(",", "")
               .replace("\u202f", "")
               .replace("\xa0", ""))
        try:
            return float(raw)
        except ValueError:
            norm = raw.replace("+", "").replace("-", "")
            return float(norm) if norm else 0.0

    async def check_last_closed_won(self) -> bool:
        profit = await self.last_closed_profit()
        return profit > 0.0
