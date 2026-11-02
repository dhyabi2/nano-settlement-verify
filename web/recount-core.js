// Pure counting logic shared by recount.html and its test. No network here: the caller passes rpc(body)->Promise<json>.
// Rule (same as nano_recount.py): count confirmed RECEIVE blocks of the address, grouped by sending account; a receive
// from the address itself is a self-send, not a payer. A history that does not reach height 1 is refused, never shrunk.
async function recount(address, rpc, maxPages = 40) {
  if (!/^(nano|xrb)_[13][13-9a-km-uw-z]{59}$/.test(address)) throw new Error("not a nano_ address");
  let head = null, pages = 0, reachedOpen = false;
  const payers = new Map();
  let payments = 0, selfSends = 0, unconfirmed = 0, total = 0n;
  while (pages < maxPages) {
    const body = { action: "account_history", account: address, count: "500", raw: "true" };
    if (head) body.head = head;
    const r = await rpc(body);
    if (r.error) throw new Error("node: " + r.error);
    const h = r.history;
    if (!Array.isArray(h)) { if (pages === 0 && !h) { reachedOpen = true; } break; }
    pages++;
    for (const b of h) {
      if (b.height === "1" || b.height === 1) reachedOpen = true;
      if (b.type !== "receive") continue;
      if (String(b.confirmed) !== "true") { unconfirmed++; continue; }
      if (b.account === address) { selfSends++; continue; }
      payments++; total += BigInt(b.amount);
      const p = payers.get(b.account) || { account: b.account, payments: 0, raw: 0n };
      p.payments++; p.raw += BigInt(b.amount); payers.set(b.account, p);
    }
    if (reachedOpen || !r.previous) break;
    head = r.previous;
  }
  if (!reachedOpen) return { complete: false, reason: "range_incomplete: did not reach the open block; no number printed" };
  return { complete: true, address, distinct_payers: payers.size, payments, self_sends: selfSends, unconfirmed,
           total_raw: total.toString(), payers: [...payers.values()].map(p => ({ account: p.account, payments: p.payments, raw: p.raw.toString() })) };
}
if (typeof module !== "undefined") module.exports = { recount };
