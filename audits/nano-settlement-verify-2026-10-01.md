# nano-settlement-verify — audit 2026-10-01

Second audit. Reviewed at commit `7e7d294`, which is three commits past the
2026-09-25 audit: `nano_terms.py` (#2), the User-Agent fix (#3) and
`nano_independence.py` (#4) are all new since then and are the substance of this pass.

## What was checked

- `pip install . && python -m pytest -q` from a clean virtualenv: **113 passed**, which is
  exactly the count `README.md:136` advertises.
- `e2e_check.py` over the real `urllib` path against the loopback stub: all eight cases
  pass, including both spellings of one account and the receive-block refusal.
- `nano_settlement_verify.verify` re-read end to end against the 2026-09-25 finding — the
  send-only check, the link-not-`block_account` comparison and the `nano_`/`xrb_`
  equivalence are all still in place and still tested.
- `nano_terms.py` line by line: `load_terms`'s schema refusal, the duplicate-key hook, the
  `_RAW` pattern, `accept`'s two check kinds, and `settle`'s delegation to `verify`.
- `nano_independence.py` line by line: `_funder`'s state/pre-state open-block handling, the
  loop and unknown-origin stops in `funding_chain`, the union-find, the hub `break`, and
  the `seller` circular-funding carve-out.
- Every amount on the path, for a float: there is none. `verify` refuses a non-`int`
  `expect_raw` outright; `nano_terms._RAW` refuses anything but an unsigned decimal
  integer string with no leading zero, so `"1e24"` and `" 12"` are both refused.
- Secrets: the working tree (14 files) and every blob version across the full history
  (32 blob versions). Clean. The only 64-hex strings are public mainnet block hashes
  (`B2EC1E2A…`, `ECCB8CB6…`) used as fixtures.
- Input reaching a shell or a path: none. No subprocess, no `open()` outside the tests.

## What was found

Nothing worth changing. The three things that looked like candidates all turned out to be
deliberate and tested:

- **`verify` raises `KeyError` on a reply carrying neither `error` nor `confirmed`.** That
  is not an oversight: `tests/test_verify.py:146`,
  `test_a_reply_with_no_confirmed_field_raises_key_error`, asserts it on purpose, with the
  reason in its docstring — "a malformed reply is not quietly treated as settled or as
  unsettled". A proxy that answers something other than a node reply fails loudly instead
  of being read as either outcome. Correct for a verifier.
- **`nano_terms.load_terms` accepts a payee that only has the right prefix**, because
  `_account_body` checks the prefix and nothing else — no length, no checksum. It costs
  nothing: `settle` compares that payee against the block's link, so a malformed payee
  can only ever make the settlement mismatch. It fails closed, and a checksum check here
  would be a second address implementation to keep in step with `nano-mcp-public`'s.
- **`independence`'s hub `break` leaves a payer that is itself a hub in a group of one.**
  That is the conservative direction — it under-counts rather than over-counts shared
  control, which is what the module's own "LOWER BOUND on common control" contract asks
  for.

## What could not be verified

- **No live node**, again. This sandbox's network policy denies every outbound host,
  including `rpc.nano.to` and `proxy.nanos.cc`, so no request was made to a real Nano node
  during this pass either. The reply shapes used are still the ones taken from
  `nano-mcp-public`'s live-verified normalizer. The 2026-09-25 audit's standing request is
  unchanged and still worth one hand check from a connected box: `verify` against a real
  confirmed mainnet send, and `funding_chain` against a real account's open block, where
  the `link` / `source` distinction in `_funder` is only exercised against fixtures here.
- **`nano_independence` against a real chain.** Every test drives it through a stubbed
  `post_json`. Whether `account_info` on a mainnet account answers `open_block` the way
  `_funder` reads it is untested against anything but those stubs.

## Also noted

The module docstrings state the contract as `NotFound` / `Mismatch`; the deliberate
`KeyError` above is a third thing a caller on a request path can see and is not named in
`verify`'s docstring. Not a defect — a caller that catches `Exception` around a node call
is right either way — and naming it would be a docstring change on a published `1.0.0`
for no behaviour difference, so it is left alone and recorded here instead.
