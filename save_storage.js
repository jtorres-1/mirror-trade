// save_storage.js
const { chromium } = require('playwright');
const fs = require('fs');

(async () => {
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext();
  const page = await context.newPage();

  await page.goto('https://pocketoption.com/en/login/');

  console.log('🟡 Please log in manually. I will save storage after 30 seconds...');
  await page.waitForTimeout(30000); // give time for manual login

  const storage = await context.storageState();
  fs.writeFileSync('./po_storage.json', JSON.stringify(storage));

  console.log('✅ Storage state saved to po_storage.json');
  await browser.close();
})();
