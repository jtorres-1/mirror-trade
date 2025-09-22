// po_executor.js — Executor with instant OPEN + background result parse
// Optimized: Caches lastPair/lastDirection across ML levels to save time
// Fixes:
//   1. Added /peek endpoint so Python can cancel ML legs on WIN
//   2. Result parser matches trade by amount + close time window
//   3. Keeps selectPair, setTradeAmount, overlays, retries intact
//   4. forceCloseOverlays handles both asset dropdowns + Magnific popups
//   5. Screenshot on failed clicks for instant debugging
//   6. Screenshot on selectPair failure for instant debugging
//   7. Hard overlay nuke (conditional) before selecting pair
//   8. /trade endpoint responds instantly, runs placeTrade in background (fixes ML delays)
//   9. Tightened sleeps in overlays to 400ms
//  10. Pair/direction cached across ML levels to skip redundant selectPair
//  11. Patched to carry chain_id through trades, /peek, and logs

const path = require("path");
const express = require("express");
const { chromium } = require("playwright");
const fs = require("fs");
require("dotenv").config();

const PO_URL_TRADE = "https://pocketoption.com/en/cabinet/";
const HEADLESS = process.env.HEADLESS === "1";
const DEFAULT_TIMEOUT = 60_000;
const LOG_FILE = path.resolve(__dirname, "trade_log.csv");

const SCREEN_DIR = path.resolve(__dirname, "screens");
if (!fs.existsSync(SCREEN_DIR)) fs.mkdirSync(SCREEN_DIR);

const SEL = {
  symbolToggle: 'span.current-symbol.current-symbol_cropped, .current-symbol',
  assetOverlay: '.drop-down-modal-wrap.active',
  tradePanel: '[id^="put-call-buttons-chart"]',
  searchInput: 'input[placeholder="Search"]',

  buyBtn: '#put-call-buttons-chart-1 a.buy, #put-call-buttons-chart-1 button:has-text("Buy"), a.btn.btn-call',
  sellBtn: '#put-call-buttons-chart-1 a.sell, #put-call-buttons-chart-1 button:has-text("Sell"), a.btn.btn-put',

  closedTab: 'li:has-text("Closed")',
  closedRow: '.deals-list__item'
};

let context, page;
let tradeInProgress = false;
let lastTradeMeta = null; // { amount, ts, pair, direction, ml_tag, chain_id }
let lastPairCache = null;
let lastDirectionCache = null;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ----------------------------- Utilities ------------------------------------
async function withRetry(fn, attempts = 2, label = "op") {
  let lastErr;
  for (let i = 0; i < attempts; i++) {
    try { return await fn(); }
    catch (err) {
      lastErr = err;
      console.warn(`[Retry] ${label} failed (${i + 1}/${attempts}) -> ${err?.message}`);
      await sleep(200);
    }
  }
  throw lastErr;
}

async function waitForTradePanel() {
  await page.waitForSelector(SEL.tradePanel, { timeout: DEFAULT_TIMEOUT });
  console.log("[✅] Trade panel detected");
}

async function forceCloseOverlays() {
  try {
    const overlayVisible = await page.locator("div.mfp-wrap, .drop-down-modal-wrap.active").first().isVisible().catch(() => false);
    if (!overlayVisible) return;

    await page.evaluate(() => {
      document.querySelectorAll("div.mfp-bg, div.mfp-wrap, .drop-down-modal-wrap.active").forEach(el => el.remove());
    }).catch(() => {});

    await sleep(400);
    const stillVisible = await page.locator("div.mfp-wrap, .drop-down-modal-wrap.active").first().isVisible().catch(() => false);
    if (stillVisible) {
      console.log("[Heal] Overlay stuck — reloading page");
      await page.reload({ waitUntil: "domcontentloaded", timeout: 15000 }).catch(() => {});
      await ensureOnPO();
    }
  } catch (err) {
    console.error("[Overlay] Force close failed:", err.message);
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
    console.log("[Heal] Page closed. Restarting browser…");
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

// ----------------------------- Patched selectPair ---------------------------
async function selectPair(pair) {
  if (lastPairCache && lastPairCache.toLowerCase() === pair.toLowerCase()) {
    console.log(`[Cache] Skipping selectPair, reusing: ${pair}`);
    return;
  }

  const toggle = page.locator(SEL.symbolToggle).first();
  try {
    await withRetry(async () => {
      await toggle.click({ timeout: DEFAULT_TIMEOUT });
      await page.waitForSelector(SEL.assetOverlay, { state: 'visible', timeout: DEFAULT_TIMEOUT });
    }, 2, "open asset overlay");

    const cleaned = pair.replace(" OTC", "").replace("/", "").toLowerCase();
    const search = page.locator(SEL.searchInput).first();
    await search.fill("");
    await search.type(cleaned, { delay: 30 }).catch(() => {});
    await sleep(200);

    const listItem = page.locator('.alist__label', { hasText: pair }).first();
    await withRetry(async () => { await listItem.click({ timeout: DEFAULT_TIMEOUT }); }, 2, "select list item");

    console.log(`[Step] Selected pair: ${pair}`);
    await page.keyboard.press('Escape').catch(() => {});
    await forceCloseOverlays();
    await sleep(100);

    lastPairCache = pair;
  } catch (err) {
    const ts = Date.now();
    const screenshotPath = path.join(SCREEN_DIR, `selectPair_fail_${pair}_${ts}.png`);
    try {
      await page.screenshot({ path: screenshotPath, fullPage: true });
      console.log(`[📸] Saved screenshot on selectPair fail: ${screenshotPath}`);
    } catch (ssErr) {
      console.error("[❌] Screenshot failed:", ssErr.message);
    }
    throw err;
  }
}

function appendLog(ts, pair, dir, amount, result, profit, ml_tag = "", chain_id = "") {
  const header = "Time,Pair,Dir,Amount,Result,Profit,ML_Tag,Chain_ID\n";
  if (!fs.existsSync(LOG_FILE)) {
    fs.writeFileSync(LOG_FILE, header);
  }
  fs.appendFileSync(LOG_FILE, `${ts},${pair},${dir},${amount},${result},${profit},${ml_tag},${chain_id}\n`);
}

// ----------------------------- Trade Placement ------------------------------
async function placeTrade(pair, amount, direction, ml_tag = "", chain_id = "") {
  if (tradeInProgress) {
    console.warn("[Guard] Trade already in progress. Skipping duplicate request.");
    return { success: false, result: "SKIPPED", profit: 0, ml_tag, chain_id };
  }
  tradeInProgress = true;

  console.log(`[Step] Trade request: ${direction.toUpperCase()} ${pair} $${amount} ${ml_tag ? `[${ml_tag}]` : ""} ${chain_id ? `[chain=${chain_id}]` : ""}`);
  await ensurePageAlive();
  await ensureOnPO();

  await forceCloseOverlays();

  try {
    await withRetry(async () => { await selectPair(pair); }, 2, "selectPair");
  } catch (err) {
    const ts = Date.now();
    const screenshotPath = path.join(SCREEN_DIR, `failed_selectPair_${pair}_${ts}.png`);
    try {
      await page.screenshot({ path: screenshotPath, fullPage: true });
      console.log(`[📸] Saved selectPair screenshot: ${screenshotPath}`);
    } catch (ssErr) {
      console.error("[❌] Failed to capture selectPair screenshot:", ssErr.message);
    }
    tradeInProgress = false;
    throw err;
  }

  console.log("[Step] Setting amount…");
  await withRetry(async () => { await setTradeAmount(amount); }, 2, "setTradeAmount");

  await forceCloseOverlays();

  const panel = page.locator(SEL.tradePanel).first();
  const btn = direction.toLowerCase() === 'buy'
    ? panel.locator(SEL.buyBtn).first()
    : panel.locator(SEL.sellBtn).first();

  await btn.scrollIntoViewIfNeeded().catch(() => {});
  await btn.waitFor({ state: 'visible', timeout: DEFAULT_TIMEOUT });
  console.log(`[CLICK] ${direction.toUpperCase()} button for ${pair} @ $${amount}`);

  try {
    await btn.click({ timeout: DEFAULT_TIMEOUT, force: true });
  } catch (err) {
    console.error("[❌] Button click failed:", err.message);
    const ts = Date.now();
    const screenshotPath = path.join(SCREEN_DIR, `failed_click_${pair}_${direction}_${ts}.png`);
    try {
      await page.screenshot({ path: screenshotPath, fullPage: true });
      console.log(`[📸] Saved screenshot: ${screenshotPath}`);
    } catch (ssErr) {
      console.error("[❌] Failed to capture screenshot:", ssErr.message);
    }
    tradeInProgress = false;
    throw err;
  }

  console.log(`[✅] Trade executed: ${direction.toUpperCase()} on ${pair} for $${amount} ${ml_tag ? `[${ml_tag}]` : ""} ${chain_id ? `[chain=${chain_id}]` : ""}`);
  tradeInProgress = false;

  lastTradeMeta = { amount: Number(amount), ts: Date.now(), pair, direction, ml_tag, chain_id };

  lastPairCache = pair;
  lastDirectionCache = direction;

  const ts = new Date().toISOString();
  appendLog(ts, pair, direction, amount, "OPEN", 0.0, ml_tag, chain_id);

  (async () => {
    await sleep(305000);
    await parseClosedTrade(amount, pair, direction, ml_tag, chain_id);
  })();

  return { success: true, result: "OPEN", profit: 0, ml_tag, chain_id };
}

// ----------------------------- Result Parser --------------------------------
async function parseClosedTrade(amount, pair, direction, ml_tag, chain_id = "") {
  let profit = 0.0, result = "LOSS";
  try {
    await page.locator(SEL.closedTab).click({ timeout: 1000 });
    const row = page.locator(SEL.closedRow).first();
    await row.waitFor({ state: "visible", timeout: 2000 });
    const rowText = (await row.innerText()).replace(/\n/g, " ").trim();
    console.log(`[Debug] Closed row text: ${rowText}`);

    const profitMatches = rowText.match(/\$-?[0-9.]+/g);
    if (profitMatches?.length) {
      const lastVal = profitMatches[profitMatches.length - 1];
      profit = parseFloat(lastVal.replace("$", ""));
      result = profit > 0 ? "WIN" : "LOSS";
    }
  } catch (err) {
    console.error("[❌] Result parse failed:", err.message);
  }
  const ts2 = new Date().toISOString();
  appendLog(ts2, pair, direction, amount, result, profit, ml_tag, chain_id);
  console.log(`[Result] ${result} ${pair} ${direction} $${amount} profit=${profit} ${ml_tag ? `[${ml_tag}]` : ""} ${chain_id ? `[chain=${chain_id}]` : ""}`);
}

// ----------------------------- Peek Endpoint --------------------------------
async function peekLatestProfit() {
  if (!lastTradeMeta) return null;
  const { amount, ts, ml_tag, chain_id } = lastTradeMeta;

  await ensurePageAlive();
  try { await page.locator(SEL.closedTab).click({ timeout: 3000 }); } catch {}
  const row = page.locator(SEL.closedRow).first();
  const visible = await row.isVisible().catch(() => false);
  if (!visible) return null;

  const rowText = (await row.innerText()).replace(/\n/g, " ").trim();
  const dollarVals = (rowText.match(/\$-?[0-9.]+/g) || []).map(s => parseFloat(s.replace("$", "")));

  const tol = Math.max(0.02, Number(amount) * 0.02);
  const withinWindow = (Date.now() - ts) < 6 * 60 * 1000;
  const hasApproxAmount = dollarVals.some(v => Math.abs(v - Number(amount)) <= tol);

  if (hasApproxAmount && withinWindow && dollarVals.length) {
    const lastVal = dollarVals[dollarVals.length - 1];
    return { profit: parseFloat(lastVal), ml_tag, chain_id };
  }
  return null;
}

// ----------------------------- Server ---------------------------------------
const app = express();
app.use(express.json());

app.post("/trade", (req, res) => {
  console.log("[REQ] Incoming trade request:", req.body);
  const { pair, amount, direction, ml_tag, chain_id } = req.body || {};
  if (!pair || !amount || !direction) {
    return res.status(400).json({ success: false, error: "pair, amount, direction required" });
  }

  res.json({ success: true, result: "QUEUED", pair, amount, direction, ml_tag, chain_id });

  (async () => {
    try {
      await placeTrade(pair, amount, direction, ml_tag, chain_id);
    } catch (err) {
      console.error("[❌] Background trade failed:", err);
    }
  })();
});

app.get("/peek", async (req, res) => {
  try {
    const result = await peekLatestProfit();
    if (!result) return res.json({ ok: false, profit: 0, ml_tag: "", chain_id: "", closed_at: null });

    return res.json({
      ok: true,
      profit: result.profit,
      ml_tag: result.ml_tag || "",
      chain_id: result.chain_id || "",
      closed_at: new Date().toISOString()   // 👈 add this line
    });
  } catch (err) {
    return res.json({
      ok: false,
      error: err?.message || String(err),
      profit: 0,
      ml_tag: "",
      chain_id: "",
      closed_at: null
    });
  }
});


// ----------------------------- Browser Init ---------------------------------
async function initBrowser() {
  console.log("[Init] Launching PocketOption…");
  const browser = await chromium.launch({ headless: HEADLESS, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  context = await browser.newContext({ storageState: "po_storage.json" });
  page = await context.newPage();
  page.setDefaultTimeout(DEFAULT_TIMEOUT);

  await page.goto(PO_URL_TRADE, { waitUntil: "domcontentloaded", timeout: DEFAULT_TIMEOUT });
  await ensureOnPO();
  console.log("[Init] PocketOption ready.");
}

app.listen(3000, async () => {
  await initBrowser();
  console.log("[Server] Executor API listening on http://localhost:3000");
});

process.on("SIGINT", async () => { try { await context?.close(); } catch {} process.exit(0); });
process.on("unhandledRejection", (err) => console.error("[UnhandledRejection]", err));
process.on("uncaughtException", (err) => console.error("[UncaughtException]", err));
