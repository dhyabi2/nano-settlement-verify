const { recount } = require("./recount-core.js");
const assert = require("assert");
const A = "nano_1434j1n4sin4cefs5njibag4tsmo596fmg3s6bdogtod3ndmdfez5yuebrh9";
const X = "nano_3saqoz5qfgmohfz3dg5ywwmxj7dwdp3g6xfbspt11g7gyrxgbupi1w9u4g4r";
const Y = "nano_1111111111111111111111111111111111111111111111111111hifc8npp";
const hist = [
  { type: "send", account: Y, amount: "5", confirmed: "true", height: "5" },
  { type: "receive", account: Y, amount: "7", confirmed: "true", height: "4" },
  { type: "receive", account: X, amount: "3", confirmed: "true", height: "3" },
  { type: "receive", account: X, amount: "2", confirmed: "false", height: "2" },
  { type: "receive", account: A, amount: "9", confirmed: "true", height: "1" }];
(async () => {
  let r = await recount(A, async () => ({ history: hist }));
  assert.deepStrictEqual([r.complete, r.distinct_payers, r.payments, r.self_sends, r.unconfirmed, r.total_raw], [true, 2, 2, 1, 1, "10"]);
  // truncated: never reaches height 1 -> refuses
  r = await recount(A, async () => ({ history: hist.slice(0, 3) }));
  assert.strictEqual(r.complete, false);
  // paging
  let calls = 0;
  r = await recount(A, async (b) => { calls++; return b.head ? { history: hist.slice(3) } : { history: hist.slice(0, 3), previous: "H" }; });
  assert.deepStrictEqual([r.complete, r.payments, calls], [true, 2, 2]);
  // account never opened: node answers with no history -> zero is an answer
  r = await recount(A, async () => ({ error: "Account not found" }).error ? { error: "Account not found" } : 0).catch(e => e.message);
  assert.ok(String(r).includes("Account not found"));
  await assert.rejects(() => recount("nano_bad", async () => ({})));
  console.log("ok 5 laws");
})();
