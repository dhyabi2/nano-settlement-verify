# nano-settlement-verify — audit 2026-09-25

First audit of this repository. Reviewed at commit `5adb9a6`.

## What was checked

- `nano_settlement_verify.py` line by line against the Nano `block_info` RPC reply, and
  `verify`'s four documented outcomes against what the code actually does.
- Every claim the README makes about behaviour: integer raw, the bounded node call, the
  four outcomes table, the `Receipt` field table, the test count.
- `tests/test_verify.py` — whether each test asserts the thing its name says, and whether
  the node fixture really is "the shape the real node sends it".
- `e2e_check.py` over the real `urllib` path against the loopback stub.
- Build and install: `pip install .` into a clean target, then import.
- Dependencies: none declared, none used beyond the standard library — nothing to advise on.
- Secrets: the working tree (8 files) and every distinct blob version across the
  repository's full history (2 revisions, 11 blob versions). Clean.
- Input reaching a shell or a file path: none. The module runs no subprocess and opens no
  file.

## What was found

**The account check read the wrong field, and nothing constrained the block to a send.**
`verify` compared `reply["block_account"]` against the `account` argument, which the README
documents as "the account you expected to be paid". A node reports `block_account` as the
account whose chain the block sits on — for a send, the **payer**. The account paid is the
block's link: `contents.link_as_account` on a state block, `contents.destination` on a
pre-state one. Two consequences, both demonstrated by tests added in this change:

1. **An honest payment was rejected.** Hand `verify` a real confirmed mainnet send and the
   seller's own address and it raised `Mismatch(payer, seller)`. The library could not
   confirm the one thing it exists to confirm.

2. **A receive block on the seller's own chain verified as a payment.** There was no
   subtype check, and on an inbound block `block_account` *is* the seller — so the hash of
   any already-confirmed receive of the right size passed both checks and returned
   `settled=True`. A buyer could serve a paid call by quoting a block they never sent.

The existing suite could not see either, because `tests/test_verify.py:45`'s `block_info()`
fixture — docstring: "in the shape the real node sends it" — emitted only
`{block_account, amount, confirmed, height}`: no `subtype`, no `contents`, no link. The
fixture's sender and payee were the same string, which is exactly the case that hides this.

Ground truth for the field semantics is this account's own `nano-mcp-public`, whose
`normalize_block_info` was written after the same mistake ("Block 13") and verified against
two independent public RPC endpoints: `block_account` is the *emitter*, the receiver is
`contents.destination` / `link_as_account`.

## What was fixed

Branch `fix/verify-the-account-that-was-paid`.

- `verify` now requires the block's operation to be `send`, and compares the **link** against
  the expected account. `Receipt.account` carries the account paid.
- The operation is read from the top-level `subtype` (state blocks) or from `contents.type`
  when that names a real operation (pre-state blocks). `"state"` names no operation, so a
  reply carrying no subtype fails closed rather than being read as a send.
- A `contents` that is not a mapping — what a node sends when it ignores `json_block` —
  reads as no link and fails closed.
- The test fixture now emits the real reply shape, with the payer's chain and the payee's
  link as different accounts. 8 new tests: the real send settles, the pre-state send
  settles, a send to someone else mismatches, a receive never settles, an open block never
  settles, an unknown operation fails closed, a stringified `contents` fails closed, and the
  amount is still reported before the payee.
- `e2e_check.py` uses the real shape and adds case 6, the receive block.
- README: the account checked is the payee and why, the outcomes table, the `Receipt.account`
  row, and the test count (it said 21 against an actual 25 before this change).

Proved: 15 failed / 18 passed with the new tests against the module as it was, then
33 passed with the fix. `e2e_check.py` passes all six cases.

## What could not be verified

- **No live node.** This sandbox's network policy denies `rpc.nano.to` and
  `proxy.nanos.cc` (403 on CONNECT), so no request was made to a real Nano node. The reply
  shape used here is taken from `nano-mcp-public`'s live-verified normalizer and its real
  on-chain fixture (block `ECCB8CB6…`, payer `nano_3t6k35gi…`, payee
  `nano_1111111111…hifc8npp`, 205676479000000000000000000000000000000 raw), not from a call
  made during this audit. Worth one hand check against a real node from a box that can
  reach one.
- Whether any caller already depends on `Receipt.account` being the payer. Nothing in this
  account's repositories imports this module; the change is a fix to a published
  `1.0.0`, so it is a behaviour change for anyone who worked around the old comparison.

## Noted, not changed

`rpc_url` is passed straight to `urllib.request.Request`, which will open a `file://` or
`ftp://` URL as readily as `https://`. The seller configures that value, so it is not a
finding — but a caller that ever took it from a request would have a file read. Out of
scope for a fix that only corrects the account check.
