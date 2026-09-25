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
- The node call is bounded by `RPC_TIMEOUT_S` (30s). Verification sits on a seller's request
  path, so a node that accepts the connection and then goes quiet fails instead of hanging it.
- The account checked is the account **paid** — the block's link, which a node reports as
  `contents.link_as_account` on a state block and `contents.destination` on a pre-state one.
  It is not `block_account`: that field is the chain the block sits on, which for a send is
  the *payer*. And only a `send` settles anything, so a receive, open, change or epoch block
  is refused however well its amount matches.
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

## Tests

```
pip install pytest
python -m pytest -v
```

33 tests: the four acceptance cases, the error paths around them, the integer-raw
guarantee, the receipt's JSON shape, and the exact request put to the node. None of them
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

## Licence

MIT — see [LICENSE](LICENSE).
