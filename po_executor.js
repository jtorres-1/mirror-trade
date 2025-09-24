// po_executor.js — Executor with robust Closed tab scraping for /peek
// Hardened against stale results, formatting mismatches, and cross-chain contamination.

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
  closedRow: '.deals-list__item',
  directionUp: 'i.fa.fa-arrow-up',
  directionDown: 'i.fa.fa-arrow-down',
  profitCell: '.centered',
  closeTime: '.close-time'
};

let context, page;
let tradeInProgress = false;
let recentTrades = {};
let lastPairCache = null;

// remembers the most recent placed trade params per chain
const expectedByChain = Object.create(null);

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

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
      console.log("[Heal] Overlay stuck, reloading page");
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
    console.log("[Nav] Navigating to PocketOption trade page");
    await page.goto(PO_URL_TRADE, { waitUntil: "domcontentloaded", timeout: DEFAULT_TIMEOUT });
  }
  await withRetry(async () => { await waitForTradePanel(); }, 2, "wait trade panel");
}

async function ensurePageAlive() {
  if (!page || page.isClosed()) {
    console.log("[Heal] Page closed. Restarting browser");
    await initBrowser();
    return;
  }
  const panelVisible = await page.locator(SEL.tradePanel).first().isVisible().catch(() => false);
  if (!panelVisible) {
    console.log("[Heal] Trade panel not visible. Reloading");
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

function appendLog(ts, pair, dir, amount, result, profit, ml_tag = "", chain_id = "", closed_at = "") {
  const header = "Time,Pair,Dir,Amount,Result,Profit,ML_Tag,Chain_ID,Closed_At\n";
  if (!fs.existsSync(LOG_FILE)) {
    fs.writeFileSync(LOG_FILE, header);
  }
  fs.appendFileSync(LOG_FILE, `${ts},${pair},${dir},${amount},${result},${profit},${ml_tag},${chain_id},${closed_at}\n`);
}

function recordClosedTrade(meta) {
  const { chain_id } = meta;
  if (!chain_id) return;
  if (!recentTrades[chain_id]) recentTrades[chain_id] = [];
  recentTrades[chain_id].unshift(meta);
  recentTrades[chain_id] = recentTrades[chain_id].filter(
    t => Date.now() - new Date(t.closed_at).getTime() < 600000
  );
  if (recentTrades[chain_id].length > 5) recentTrades[chain_id].pop();
}

async function parseClosedTrade(amount, pair, direction, ml_tag, chain_id = "") {
  let profit = 0.0, result = "LOSS", closed_at = new Date().toISOString();
  try {
    await page.locator(SEL.closedTab).click({ timeout: 2000 });
    await page.waitForSelector(SEL.closedRow, { timeout: 3000 });
    await page.waitForTimeout(500);

    const rows = await page.locator(SEL.closedRow).all();
    for (const row of rows) {
      const text = await row.innerText().catch(() => '');

      // --- Pair check
      if (pair && !text.includes(pair)) continue;

      // --- Amount check (numeric tolerant)
      const numericMatches = text.match(/[\d.,]+/g) || [];
      const hasAmount = numericMatches.some(num => {
        const val = parseFloat(num.replace(/,/g, ""));
        return !isNaN(val) && Math.abs(val - amount) < 0.01;
      });
      if (amount && !hasAmount) continue;

      // --- Direction check
      let detectedDirection = null;
      if (await row.locator(SEL.directionUp).count() > 0) detectedDirection = "BUY";
      if (await row.locator(SEL.directionDown).count() > 0) detectedDirection = "SELL";
      if (detectedDirection && direction && detectedDirection !== direction.toUpperCase()) continue;

      // --- Profit check
      const profitNode = row.locator(SEL.profitCell).last();
      if (await profitNode.count() === 0) continue;
      let profitText = await profitNode.innerText();
      profitText = profitText.replace(/[^\d.-]/g, "");
      profit = parseFloat(profitText || "0");
      result = profit > 0 ? "WIN" : "LOSS";

      // --- Closed time freshness
      try {
        const timeNode = row.locator(SEL.closeTime).first();
        if (await timeNode.count()) {
          const timeStr = await timeNode.innerText();
          const now = new Date();
          closed_at = new Date(now.toDateString() + " " + timeStr + " UTC").toISOString();
        }
      } catch {}
      const closedAgo = Date.now() - new Date(closed_at).getTime();
      if (closedAgo > 180000) continue; // skip if older than 3 min

      break;
    }
  } catch (err) {
    console.error("[❌] Result parse failed:", err.message);
  }

  const meta = { amount, pair, direction, ml_tag, chain_id, profit, result, closed_at };
  recordClosedTrade(meta);

  const ts2 = new Date().toISOString();
  appendLog(ts2, pair, direction, amount, result, profit, ml_tag, chain_id, closed_at);
  console.log(`[Result] ${result} ${pair} ${direction} $${amount} profit=${profit} [${ml_tag}] [chain=${chain_id}] closed_at=${closed_at}`);
  return meta;
}

async function placeTrade(pair, amount, direction, ml_tag = "", chain_id = "", expiration = 300) {
  if (tradeInProgress) {
    console.warn("[Guard] Trade already in progress. Skipping duplicate request.");
    return { success: false, result: "SKIPPED", profit: 0, ml_tag, chain_id };
  }
  tradeInProgress = true;

  try {
    console.log(`[Step] Trade request: ${direction.toUpperCase()} ${pair} $${amount} [${ml_tag}] [chain=${chain_id}]`);
    await ensurePageAlive();
    await ensureOnPO();
    await forceCloseOverlays();

    await withRetry(async () => { await selectPair(pair); }, 2, "selectPair");
    await withRetry(async () => { await setTradeAmount(amount); }, 2, "setTradeAmount");

    const panel = page.locator(SEL.tradePanel).first();
    const btn = direction.toLowerCase() === 'buy'
      ? panel.locator(SEL.buyBtn).first()
      : panel.locator(SEL.sellBtn).first();

    await btn.scrollIntoViewIfNeeded().catch(() => {});
    await btn.waitFor({ state: 'visible', timeout: DEFAULT_TIMEOUT });
    console.log(`[CLICK] ${direction.toUpperCase()} button for ${pair} @ $${amount}`);

    await btn.click({ timeout: DEFAULT_TIMEOUT, force: true }).catch(err => {
      console.error("[❌] Button click failed:", err.message);
    });

    if (chain_id) {
      expectedByChain[chain_id] = { amount, pair, direction, ml_tag, ts: Date.now() };
    }

    const ts = new Date().toISOString();
    appendLog(ts, pair, direction, amount, "OPEN", 0.0, ml_tag, chain_id, "");

    return { success: true, result: "OPEN", profit: 0, ml_tag, chain_id };
  } finally {
    tradeInProgress = false;
  }
}

async function peekLatestProfit(chain_id = null) {
  await ensurePageAlive();
  try { await page.locator(SEL.closedTab).click({ timeout: 3000 }); } catch {}

  if (!chain_id) return null;
  const expected = expectedByChain[chain_id];
  if (!expected) return null;

  const { amount, pair, direction, ml_tag } = expected;
  const meta = await parseClosedTrade(amount, pair, direction, ml_tag, chain_id);
  return meta || null;
}

const app = express();
app.use(express.json());

app.post("/trade", (req, res) => {
  console.log("[REQ] Incoming trade request:", req.body);
  const { pair, amount, direction, ml_tag, chain_id, expiration } = req.body || {};
  if (!pair || !amount || !direction) {
    return res.status(400).json({ success: false, error: "pair, amount, direction required" });
  }

  res.json({ success: true, result: "QUEUED", pair, amount, direction, ml_tag, chain_id });

  (async () => {
    try { await placeTrade(pair, amount, direction, ml_tag, chain_id, expiration); }
    catch (err) { console.error("[❌] Background trade failed:", err); }
  })();
});

app.get("/peek", async (req, res) => {
  try {
    const { chain_id } = req.query;
    const result = await peekLatestProfit(chain_id);
    if (!result) return res.json({ ok: false, profit: 0, ml_tag: "", chain_id: "", closed_at: null });
    return res.json({ ok: true, profit: result.profit, ml_tag: result.ml_tag, chain_id: result.chain_id, closed_at: result.closed_at });
  } catch (err) {
    return res.json({ ok: false, error: err?.message || String(err), profit: 0, ml_tag: "", chain_id: "", closed_at: null });
  }
});

async function initBrowser() {
  console.log("[Init] Launching PocketOption");
  const browser = await chromium.launch({ headless: HEADLESS, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  context = await browser.newContext({ storageState: "po_storage.json" });
  page = await context.newPage();
  page.setDefaultTimeout(DEFAULT_TIMEOUT);

  await page.goto(PO_URL_TRADE, { waitUntil: "domcontentloaded", timeout: DEFAULT_TIMEOUT });
  await ensureOnPO();
  console.log("[Init] PocketOption ready");
}

app.listen(3000, async () => {
  await initBrowser();
  console.log("[Server] Executor API listening on http://localhost:3000");
});

process.on("SIGINT", async () => { try { await context?.close(); } catch {} process.exit(0); });
process.on("unhandledRejection", (err) => console.error("[UnhandledRejection]", err));
process.on("uncaughtException", (err) => console.error("[UncaughtException]", err));
