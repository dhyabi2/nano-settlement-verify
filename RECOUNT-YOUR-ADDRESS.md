# Recount your own address (a seller's column-one number)

A number a stranger can re-derive from a public node, with no word from the seller. Two ways, same rule (confirmed receives, grouped by sending account; a receive from the address itself is a self-send, not a payer; an unreadable or truncated history is refused, never shown as a smaller number).

**Command line, Nano or USDC on Base**
```
curl -sL https://github.com/dhyabi2/nano-settlement-verify/archive/refs/tags/v1.1.0.tar.gz | tar xz && python3 nano-settlement-verify-1.1.0/nano_recount.py --address nano_YOURADDRESS --json
```

**Browser, Nano only, nothing to install:** download [web/recount.html](web/recount.html), open it, paste the address. It calls a public node from your own browser (default `https://rpc.nano.to`, any node works; change the field). Its counting code is `web/recount-core.js`, tested by `node web/test_recount_core.js` (5 laws: self-send excluded, unconfirmed excluded, paging, truncated history refused, bad address refused).

Checked 2026-11-02 against the public account `nano_1434j1n4sin4cefs5njibag4tsmo596fmg3s6bdogtod3ndmdfez5yuebrh9`: the Python tool and the page both print 2 distinct payers, 3 payments, 30.000005 XNO.

## Does the same one-line recount work for a USDC x402 seller?
Yes, but it is a heavier read, so say what it costs. On Base the payee's balance history is not an account chain: you filter `eth_getLogs` on the USDC contract for `Transfer(to = address)` over a block range. `nano_recount.py --address 0x... --from-block N` does that in windows the node accepts; `--from-block` is required because starting at block 0 is thousands of calls. Nano reads the account's own chain from the open block, one short walk.

## What a count does NOT prove (both rails)
1. **Merchant binding**: the address must be provably the seller's (published in the seller's own x402 document or signed by it). A count of an address nobody ties to the seller counts nothing about the seller.
2. **Payer independence**: distinct payers who share one funder are one customer. Run `nano_funding.py` (FUNDING-PROVENANCE.md) on the payers.
3. A payer is a sending account, not a person.
