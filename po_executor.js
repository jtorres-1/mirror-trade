// po_executor.js — Executor with corrected result logging
const path = require("path");
const express = require("express");
const { chromium } = require("playwright");
const fs = require("fs");

const PO_URL_TRADE = "https://pocketoption.com/en/cabinet/";
const USER_DATA_DIR = path.resolve(__dirname, "po_profile");
const HEADLESS = process.env.HEADLESS === "1";
const DEFAULT_TIMEOUT = 60_000;
const LOG_FILE = path.resolve(__dirname, "trade_log.csv");

const SEL = {
  symbolToggle: 'span.current-symbol.current-symbol_cropped, .current-symbol',
  assetOverlay: '.drop-down-modal-wrap.active',
  tradePanel: '#put-call-buttons-chart-1, .put-call-buttons',
  searchInput: 'input[placeholder="Search"]',
  buyBtn: 'a:has-text("Buy"), a.buy, button:has-text("Buy")',
  sellBtn: 'a:has-text("Sell"), a.sell, button:has-text("Sell")',
  closedTab: 'li:has-text("Closed")',
  closedRow: '.deals-list__item'
};

let context, page;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ----------------------------- Utilities ------------------------------------
async function withRetry(fn, attempts = 2, label = "op") {
  let lastErr;
  for (let i = 0; i < attempts; i++) {
    try { return await fn(); }
    catch (err) {
      lastErr = err;
      console.warn(`[Retry] ${label} failed (${i + 1}/${attempts}) -> ${err?.message}`);
      await sleep(300);
    }
  }
  throw lastErr;
}

async function waitForTradePanel() {
  await page.waitForSelector(SEL.tradePanel, { timeout: DEFAULT_TIMEOUT });
}

async function focusTradePanel() {
  const panel = page.locator(SEL.tradePanel).first();
  await panel.waitFor({ state: "visible", timeout: DEFAULT_TIMEOUT });
  await panel.click({ timeout: DEFAULT_TIMEOUT }).catch(() => {});
}

async function forceCloseOverlays() {
  // extra guarantee that dropdown closes
  for (let i = 0; i < 3; i++) {
    try { await page.keyboard.press('Escape'); } catch {}
    const overlay = page.locator(SEL.assetOverlay).first();
    const visible = await overlay.isVisible().catch(() => false);
    if (!visible) break;
    try { await page.mouse.click(10, 10); } catch {}
    await sleep(120);
  }
}

async function ensureOnPO() {
  if (!page || page.isClosed()) throw new Error("No page");
  const url = page.url() || "";
  if (!url.includes("pocketoption.com")) {
    console.log("[Nav] Navigating to PocketOption trade page…");
    await page.goto(PO_URL_TRADE, { waitUntil: "domcontentloaded", timeout: DEFAULT_TIMEOUT });
  }
  await withRetry(async () => { await waitForTradePanel(); }, 2, "wait trade panel");
}

async function ensurePageAlive() {
  if (!page || page.isClosed()) {
    console.log("[Heal] Page was closed. Re-initializing browser…");
    await initBrowser();
    return;
  }
  const panelVisible = await page.locator(SEL.tradePanel).first().isVisible().catch(() => false);
  if (!panelVisible) {
    console.log("[Heal] Trade panel not visible. Reloading…");
    await page.reload({ waitUntil: "domcontentloaded", timeout: DEFAULT_TIMEOUT }).catch(() => {});
    await ensureOnPO();
  }
}

async function setTradeAmount(amount) {
  const panel = page.locator(SEL.tradePanel).first();
  const amountBox = panel.getByRole('textbox').first();

  await amountBox.waitFor({ state: 'attached', timeout: DEFAULT_TIMEOUT }).catch(() => {});
  try {
    await amountBox.fill(String(amount), { force: true, timeout: 1500 });
    return;
  } catch {}
  await panel.click({ force: true }).catch(() => {});
  await page.keyboard.press('Control+A').catch(() => {});
  await page.keyboard.press('Backspace').catch(() => {});
  await page.keyboard.type(String(amount)).catch(() => {});
}

async function selectPair(pair) {
  const toggle = page.locator(SEL.symbolToggle).first();
  let current = "";
  try { current = (await toggle.textContent({ timeout: 800 })) || ""; } catch {}
  if (current && current.toLowerCase().includes(pair.toLowerCase().replace(" otc", ""))) {
    console.log(`[Step] Pair already selected: ${pair}`);
    await forceCloseOverlays();
    return;
  }

  await withRetry(async () => {
    await toggle.click({ timeout: DEFAULT_TIMEOUT });
    await page.waitForSelector(SEL.assetOverlay, { state: 'visible', timeout: DEFAULT_TIMEOUT });
  }, 2, "open asset overlay");

  const cleaned = pair.replace(" OTC", "").replace("/", "").toLowerCase();
  const search = page.locator(SEL.searchInput).first();
  await search.fill(""); 
  await search.type(cleaned, { delay: 30 }).catch(() => {});
  await sleep(250);

  const listItem = page.locator('.alist__label', { hasText: pair }).first();
  await withRetry(async () => { await listItem.click({ timeout: DEFAULT_TIMEOUT }); }, 2, "select list item");

  console.log(`[Step] Selected pair: ${pair}`);

  // force-close dropdown
  await page.keyboard.press('Escape').catch(() => {});
  await forceCloseOverlays();
}

// ----------------------------- Logging helper --------------------------------
function appendLog(ts, pair, dir, amount, result, profit) {
  if (!fs.existsSync(LOG_FILE)) {
    fs.writeFileSync(LOG_FILE, "Time,Pair,Dir,Amount,Result,Profit\n");
  }
  fs.appendFileSync(LOG_FILE, `${ts},${pair},${dir},${amount},${result},${profit}\n`);
}

// ----------------------------- Trade Exec -----------------------------------
async function placeTrade(pair, amount, direction) {
  console.log(`[Step] Trade request: ${direction.toUpperCase()} ${pair} $${amount}`);

  await ensurePageAlive();
  await ensureOnPO();
  await focusTradePanel();

  await withRetry(async () => { await selectPair(pair); }, 2, "selectPair");

  console.log("[Step] Setting amount…");
  await withRetry(async () => { await setTradeAmount(amount); }, 2, "setTradeAmount");

  console.log("[Step] Expiry locked at 5m (no action needed)");

  const panel = page.locator(SEL.tradePanel).first();
  const btn = direction.toLowerCase() === 'buy'
    ? panel.locator(SEL.buyBtn).first()
    : panel.locator(SEL.sellBtn).first();

  await btn.waitFor({ state: 'visible', timeout: DEFAULT_TIMEOUT });
  await withRetry(async () => { await btn.click({ timeout: DEFAULT_TIMEOUT }); }, 2, "click buy/sell");

  console.log(`[✅] Trade executed: ${direction.toUpperCase()} on ${pair} for $${amount}`);

  // ✅ Wait for expiry: 5m + buffer
  await sleep(305000);

  // Switch to Closed tab and scrape result
  await page.locator(SEL.closedTab).click({ timeout: 5000 }).catch(() => {
    throw new Error("Failed to click Closed tab");
  });

  const row = page.locator(SEL.closedRow).first();
  await row.waitFor({ state: "visible", timeout: 10000 });

  const rowText = (await row.innerText()).replace(/\n/g, " ").trim();
  console.log(`[Debug] Closed row text: ${rowText}`);

  // ✅ Extract final profit (last $ value)
  let profit = 0.0, result = "LOSS";
  const profitMatches = rowText.match(/\$[0-9.]+/g);
  if (profitMatches && profitMatches.length > 0) {
    const lastVal = profitMatches[profitMatches.length - 1];
    profit = parseFloat(lastVal.replace("$", ""));
    result = profit > 0 ? "WIN" : "LOSS";
  }

  const ts = new Date().toISOString();
  appendLog(ts, pair, direction, amount, result, profit);

  console.log(`[Result] ${result} ${pair} ${direction} $${amount} profit=${profit}`);
  return { success: true, result, profit };
}

// ----------------------------- Browser Init ---------------------------------
async function initBrowser() {
  console.log("[Init] Launching PocketOption persistent session…");
  context = await chromium.launchPersistentContext(USER_DATA_DIR, {
    headless: HEADLESS,
    viewport: null,
    args: [
      "--start-maximized",
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--disable-blink-features=AutomationControlled"
    ]
  });

  page = context.pages()[0] || await context.newPage();
  page.setDefaultTimeout(DEFAULT_TIMEOUT);

  page.on("close", () => console.warn("[Warn] Page closed event detected."));
  context.on("close", () => console.warn("[Warn] Context closed event detected."));

  await page.goto(PO_URL_TRADE, { waitUntil: "domcontentloaded", timeout: DEFAULT_TIMEOUT });
  await ensureOnPO();
  console.log("[Init] PocketOption ready.");
}

// ----------------------------- HTTP API -------------------------------------
const app = express();
app.use(express.json());

app.post("/trade", async (req, res) => {
  console.log("[REQ] Incoming trade request:", req.body);
  const { pair, amount, direction } = req.body || {};
  if (!pair || !amount || !direction) {
    console.error("[❌] Missing required fields:", req.body);
    return res.status(400).json({ success: false, error: "pair, amount, direction required" });
  }
  try {
    const result = await placeTrade(pair, amount, direction);
    res.json({ success: true, pair, amount, direction, ...result });
  } catch (err) {
    console.error("[❌] Trade failed:", err);
    res.status(500).json({ success: false, error: err?.message || String(err) });
  }
});

app.listen(3000, async () => {
  await initBrowser();
  console.log("[Server] Executor API listening on http://localhost:3000");
});

// Graceful shutdown
process.on("SIGINT", async () => {
  console.log("[Shutdown] Closing context…");
  try { await context?.close(); } catch {}
  process.exit(0);
});

process.on("unhandledRejection", (err) => {
  console.error("[UnhandledRejection]", err);
});
process.on("uncaughtException", (err) => {
  console.error("[UncaughtException]", err);
});
