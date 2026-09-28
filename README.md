# nano-settlement-verify

Prove a Nano (XNO) payment really settled — without running a Nano node client, and
without taking anyone's word for it.

If you sell per call (x402, pay-per-call, metered API), this answers the question every
maintainer asks first: *how do I check the payment actually arrived?* You hand it a block
hash, the amount you quoted, and the account you expected to be paid. It asks a public
Nano node and hands back a receipt.

- Python 3.11, **standard library only** — no third-party dependency.
- Raw amounts are integers throughout. 1 XNO is `10**30` raw; a float loses the low digits,
  so raw is parsed with `int()` and never with `float()`. A float `expect_raw` is refused
  with `TypeError` rather than compared — written as `1e30`, 1 XNO would otherwise "match"
  an amount 19884624838656 raw short of it.
- Every node call sends its own User-Agent (`USER_AGENT`). Cloudflare-fronted RPCs,
  `rpc.nano.to` among them, answer urllib's default one with 403 "error code: 1010",
  which reads exactly like a bad node.
- The node call is bounded by `RPC_TIMEOUT_S` (30s). Verification sits on a seller's request
  path, so a node that accepts the connection and then goes quiet fails instead of hanging it.
- The account checked is the account **paid** — the block's link, which a node reports as
  `contents.link_as_account` on a state block and `contents.destination` on a pre-state one.
  It is not `block_account`: that field is the chain the block sits on, which for a send is
  the *payer*. And only a `send` settles anything, so a receive, open, change or epoch block
  is refused however well its amount matches.
- The account is compared as an account, not as the string that spells it. One Nano
  account has two spellings — the modern `nano_` form and the legacy `xrb_` form, whose
  60 characters after the prefix are identical — and a node always answers the `nano_`
  one. Pass `account` in either; a stranger is still refused in either. The receipt
  records the account as the node spelled it, so it always carries the `nano_` form.
- No signing, no sending, no wallet or seed handling of any kind. It only reads.

## Install

```
pip install .
```

Or just copy `nano_settlement_verify.py` into your project — it is one file with no
dependencies.

## Use it

```python
from nano_settlement_verify import verify, NotFound, Mismatch

RPC = "https://rpc.nano.to"          # any Nano node RPC endpoint

try:
    receipt = verify(
        block_hash="B2EC1E2A1F2C3D4E5F60718293A4B5C6D7E8F9012345678901234567890ABCDE",
        expect_raw=10**24,            # the amount you quoted, in raw
        account="nano_3abc",          # the account you expected to be paid
        rpc_url=RPC,
    )
except NotFound as error:
    print(f"no such block: {error.block_hash}")       # nothing settled — do not serve
except Mismatch as error:
    print(f"expected {error.expected}, got {error.got}")  # underpaid, or paid elsewhere
else:
    if receipt.settled:
        print(receipt.to_json())
        # {"settled": true, "amount_raw": 1000000000000000000000000, "height": 42, "account": "nano_3abc"}
        serve_the_call()
    else:
        print("node has the block but has not confirmed it yet — wait and ask again")
```

## What comes back

`verify(block_hash, expect_raw, account, rpc_url) -> Receipt`

| field | meaning |
| --- | --- |
| `settled` | `True` only when the node reports the block confirmed |
| `amount_raw` | the block's amount, as an integer number of raw |
| `height` | the block's height in its account chain |
| `account` | the account the node reports the block paid |

`Receipt.to_json()` gives you that as a JSON string, with `amount_raw` as an unquoted
integer — so it survives a round trip that a float would have truncated.

Four outcomes, and nothing else:

| the node says | you get |
| --- | --- |
| confirmed, right amount, right account | `Receipt(settled=True, ...)` |
| `"confirmed": "false"` | `Receipt(settled=False, amount_raw=0, height=0, account="")` |
| `{"error": ...}` | raises `NotFound(block_hash)` |
| a different amount, a block that is not a send, or a different payee | raises `Mismatch(got, expected)` |

An unsettled receipt is not a failure — it means *not yet*. Ask again later; this library
deliberately has no retry loop and no cache, so the waiting is yours to decide.

## Pay against terms, not against a claim

`nano_terms.py` (same package, also one stdlib file) is for two agents that trade once
and need to agree, before any XNO moves, on what is paid, to whom, and what counts as
delivered.

- **The terms are addressed by the sha256 of their own bytes.** Both sides hold the bytes
  and re-derive the hash, so checking a cited hash needs no registry and nobody else
  online.
- **The schema is pinned:** `version` (`"1"`), `payee`, `amount_raw` (a decimal string of
  raw), `task` (what is bought, in words) and `acceptance`. An unknown field, a missing
  one or a duplicated key is refused, not ignored.
- **Acceptance is a check a program runs**, written in before payment: `{"sha256": hex}`
  of the deliverable, or `{"json_keys": [...]}` that a JSON object must carry non-empty.
- **Settlement is checked against the payee and amount the terms pinned**, through
  `verify` above, never against what the seller says afterwards.

```python
from nano_terms import load_terms, accept, settle, terms_sha256

terms = load_terms(terms_bytes, cited_sha256)   # TermsRefused on any mismatch
done = accept(terms, deliverable_bytes)         # offline; NotAccepted if it fails
# the buyer's own wallet pays terms.amount_raw to terms.payee here
deal = settle(terms, done, block_hash, RPC)
print(deal.to_json())
# {"terms_sha256": "...", "deliverable_sha256": "...", "block_hash": "...",
#  "settled": true, "amount_raw": 1000000000000000000000000, "payee": "nano_3..."}
```

The `Deal` record cites the terms, the deliverable and the block by hash, so anyone
holding the bytes can check it offline, and anyone can check the block on a public node.
Like `verify`, it never signs or sends, and spending one block hash once is yours to
enforce.

## Tests

```
pip install pytest
python -m pytest -v
```

95 tests: for `verify`, the four acceptance cases, the error paths around them, the
integer-raw guarantee, the receipt's JSON shape, the exact request put to the node and
the User-Agent it carries; for `nano_terms`, the hash check, the pinned schema, both
acceptance checks and settlement against the pinned payee and amount. None of them
touch the network — the node reply is stubbed, and a fixture fails any test that tries to
open a socket.

There is also a hand check that exercises the real `urllib` path against a throwaway HTTP
node stub on loopback:

```
python e2e_check.py
```

## Scope

It verifies. It does not sign, send, hold a key, retry, cache, or offer a command line.
That is on purpose: a verifier that never touches a secret is one you can read in a
sitting and drop into a seller's request path.

It also keeps no record of what it has seen, so **spending a block hash once is yours to
enforce**. A settled receipt says this block paid you that amount; it does not say the block
has not already been spent on an earlier call. Store the hash with the call it paid for and
refuse it the second time, or one payment buys every call the buyer cares to make.

## Licence

MIT — see [LICENSE](LICENSE).

## How many independent payers?

Counting paying keys is not counting buyers: one operator can pay from ten accounts.
`nano_independence` groups payer accounts by where their money came from - the send that
opened each account - and counts each group once. Read-only; same bounded node call.

```python
from nano_independence import independence

report = independence(payers, "https://rpc.nano.to", seller="nano_<your account>",
                      ignore=["nano_<an exchange hot wallet>"])
report.independent      # groups sharing no funder, and not funded by you
report.funded_by_seller # your own money coming back in a circle
```

It is a lower bound on common control, not proof of independence: keys funded from
different exchange withdrawals read as separate payers. `hops` (default 1) follows the
funding chain further back; pass hub accounts in `ignore` so strangers who withdrew from
one exchange are not merged.
