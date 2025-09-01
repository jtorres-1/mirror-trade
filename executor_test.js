const { initPOExecutor } = require('./po_executor');

async function main() {
  // CLI args: node executor_test.js "EUR/JPY OTC" 1 buy
  const args = process.argv.slice(2);
  const pair = args[0] || "EUR/JPY OTC";
  const amount = parseFloat(args[1]) || 1;
  const direction = args[2] || "buy";

  console.log(`[Test] Running trade: ${direction.toUpperCase()} ${pair} for $${amount}`);

  try {
    const executor = await initPOExecutor();
    await executor.placeTrade(pair, amount, direction);
    await executor.close();
  } catch (err) {
    console.error("Executor test failed:", err);
  }
}

main();
