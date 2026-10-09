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
except (OSError, ValueError) as error:
    # The node did not answer: refused, timed out, HTTP 500, a body that is not
    # JSON, or JSON that is not a node reply. This is NOT "the payment is bad" —
    # it is "we could not check".
    # Retry or hold the call; do not refuse a payment that may well have
    # settled. Catch it: this sits on your request path, and without this arm
    # the first hiccup of a public node raises straight through it.
    print(f"node unreachable, nothing was checked: {error}")
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

Four outcomes when the node answers:

| the node says | you get |
| --- | --- |
| confirmed, right amount, right account | `Receipt(settled=True, ...)` |
| `"confirmed": "false"` | `Receipt(settled=False, amount_raw=0, height=0, account="")` |
| `{"error": ...}` | raises `NotFound(block_hash)` |
| a different amount, a block that is not a send, or a different payee | raises `Mismatch(got, expected)` |

And one for when it does not answer at all. There is no retry and no wrapper
exception, so the transport failure reaches you as itself:

| what went wrong | what `verify` raises |
| --- | --- |
| connection refused, DNS failure, or `RPC_TIMEOUT_S` elapsed | `urllib.error.URLError` (an `OSError`) |
| an HTTP error status — 403, 429, 500 | `urllib.error.HTTPError` (also an `OSError`) |
| a body that is not JSON — an HTML error or maintenance page | `json.JSONDecodeError` (a `ValueError`) |
| a body that **is** JSON but is not a node reply — a proxy's status page, a rate-limit envelope | `NotANodeReply` (also a `ValueError`) |

The last row is the one worth knowing about, because a public RPC behind a proxy
answers 200 with its own JSON at least as often as it answers with HTML, and
none of those bodies carries `confirmed`. It used to raise a bare `KeyError`,
which is neither an `OSError` nor a `ValueError`, so it went straight through
the arm below and out of the seller's request path.

**These are not `NotFound`.** "The node has no such block" and "the node did not
answer" are different facts, and only the first one means the payment is not
there. A seller that treats an unreachable node as a missing block refuses a
call the buyer already paid for, and XNO does not come back. Catch `OSError`
and `ValueError` around `verify` and retry or hold — the example above does.

An unsettled receipt is not a failure — it means *not yet*. Ask again later; this library
deliberately has no retry loop and no cache, so the waiting is yours to decide.

## Check it without running our code

You should not have to run the seller's code to believe the seller's receipt. Everything
`verify` decides comes from one public node call, which you can make yourself:

```
curl -s -A 'my-check/1' -H 'Content-Type: application/json' \
  -d '{"action": "block_info", "json_block": "true", "hash": "991CF190094C00F0B68E2E5F75F6BEE95A2E0BD93CEAA4A6734DB9F19B728948"}' \
  https://rpc.nano.to
```

Send both headers. `rpc.nano.to` answers a body without `Content-Type: application/json`
with `{"error":"Action not provided"}`, and curl's or urllib's default User-Agent may be
refused with 403 - either reads like a missing block when it is not.

Then read four fields, which are the only ones `verify` reads:

| field | a payment to you has |
| --- | --- |
| `confirmed` | the string `"true"` (anything else is *not yet*, not *no*) |
| `subtype` (state block) or `contents.type` (older block) | `send` - a receive, open, change or epoch block pays nobody |
| `amount` | the raw amount you quoted, compared as an integer, never as a float |
| `contents.link_as_account` (state) or `contents.destination` (older) | your account; `nano_` and `xrb_` spellings of the same 60 characters are one account |

The hash above is the genesis account's open block. It is confirmed, but its
`contents.type` is `open`, so it settles nothing - which is the answer `verify` gives too
(`Mismatch`). Ask a second node you chose, not one the seller named, if one node's word
is not enough; the next section does that in code.

## One node's word, or two?

`verify` above asks one node. That is the right default for a seller's request path, but
it means the receipt is exactly as good as that one endpoint — and public Nano RPC is
thin: `rpc.nano.to` wants a key and rate-limits a burst, Nanswap throttles, and a proxy
in front of any of them can answer HTTP 200 with something that is not a node reply at
all. An agent with no node of its own has no second opinion to fall back on.

`nano_quorum.py` (same package, also one stdlib file) is that second opinion.

```python
from nano_quorum import verify_corroborated, Disagreement, NotCorroborated

NODES = ["https://rpc.nano.to", "https://my-other-node.example/proxy"]

try:
    out = verify_corroborated(block_hash, expect_raw=10**24, account="nano_3abc",
                              rpc_urls=NODES)          # agree=2 by default
except Mismatch:
    refuse()            # a node has it confirmed, and it did not pay you
except Disagreement:
    refuse()            # two nodes, two different stories — one of them is wrong
except NotCorroborated:
    hold_and_retry()    # too few endpoints answered; NOTHING was checked
else:
    if out.receipt.settled:
        serve_the_call()        # out.agreed names the endpoints that concurred
    else:
        wait_and_ask_again()    # answered, but not confirmed by enough of them yet
```

The rules it holds to, each of which is a way of failing closed:

- **A repeated endpoint is one witness.** `["https://rpc.nano.to", "https://rpc.nano.to/"]`
  is refused with `ValueError` before any call, rather than quietly counting one node
  twice. Being *distinct* still does not make endpoints *independent* — several public
  Nano RPCs are proxies in front of the same node, and nothing here can see that.
  Agreement is only worth what your endpoint list is worth.
- **It does not break ties.** Two nodes that each report the block confirmed, with
  different contents, cannot both be right, and nothing here can tell you which is
  lying. A third vote would only make a guess look like a quorum, so it raises.
- **One `Mismatch` refuses at once.** A node that has the block *confirmed* and says it
  paid a different amount or a different account is reporting something a later node
  cannot overturn — a confirmed block's contents are fixed by its hash.
- **Silence is never a "no".** An endpoint that is refused, times out, or serves an HTML
  maintenance page has told you nothing. Fewer than `agree` endpoints answering raises
  `NotCorroborated`, which means *hold the call*, not *refuse the buyer*. Refusing a call
  that was paid for costs the sale, and XNO does not come back.
- **It does not retry.** A failed endpoint is skipped, not asked again. Waiting is yours
  to decide, as it is for `verify`.
- **It never sends.** `process` is deliberately absent; this reads, like the rest of the
  package.

Two more functions come with it:

```python
from nano_quorum import balance_corroborated, post_json_failover

balance_raw, receivable_raw = balance_corroborated("nano_3abc", NODES)   # integers of raw
reply, which_node = post_json_failover(NODES, {"action": "account_info", ...})
```

`balance_corroborated` compares only the **confirmed** figures. An endpoint that answers
without them — a proxy that drops `include_confirmed` — counts as not having answered,
rather than being read for its unconfirmed `balance`, which is precisely the number that
differs between nodes. Confirmed figures still move, so two honest nodes a block apart
will disagree here; the remedy is to ask again, not to take the larger number.

`post_json_failover` is **failover, not corroboration**: it returns the first endpoint
that answers, for reads where one node's word is enough. Do not decide that money
arrived with it.

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
- **The payee must be an address XNO can actually reach.** A Nano address carries a
  blake2b checksum of its own public key, and `load_terms` checks it — not only the
  `nano_`/`xrb_` prefix. A Nano send is irreversible, so terms that pin a mistyped or
  truncated payee are money that leaves and never arrives; that is refused here, with
  `TermsRefused("invalid_payee", ...)`, rather than discovered afterwards.
  `is_valid_account(address)` is public if you want the same check yourself.
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

287 tests: for `verify`, the four acceptance cases, the error paths around them, the
integer-raw guarantee, the receipt's JSON shape, the exact request put to the node and
the User-Agent it carries; for `nano_terms`, the hash check, the pinned schema, both
acceptance checks, the payee checksum (including a one-character-off address, a
character outside Nano's alphabet, and both spellings of one account) and settlement
against the pinned payee and amount; for
`nano_independence`, the funding chain and the independent-payer grouping; for
`nano_quorum`, agreement, contradiction, the duplicate endpoint and every way an
endpoint can say nothing; for `nano_payers`, both x402 document shapes, every refusal in
the payee table, and what the payer count will not include - an unconfirmed receive, the
seller's own sends, a receive from itself, one account written two ways, a chain read only
half way; for `nano_claim`, claimed, still receivable to an opened and an unopened
account, a non-send and a malformed hash refused, and every read that fails - a node error, a
receive not found within the bound - coming back as `unknown` with a nonzero exit; and for the README itself, that the curl body in *Check it without running our
code* is the payload `verify` really sends, so the two cannot drift, and that the count at the head of this
paragraph is the count the suite collects; for the skill bundle, that its vendored library is the
library and that its CLI answers the exit codes SKILL.md documents. None of them touch the network — the node reply is stubbed,
and a fixture fails any test that tries to open a socket.

There is also a hand check that exercises the real `urllib` path against a throwaway HTTP
node stub on loopback:

```
python e2e_check.py
python e2e_quorum_check.py
python e2e_payers_check.py
```

The second one stands up several loopback nodes that disagree with each other — one
honest, one lagging, one contradicting, one serving a maintenance page, one dead — and
then, when the network allows it, repeats the decisive case against a real confirmed
block on the live ledger.

The third audits a real x402 seller the same way: it fetches the seller's own document,
derives the account that document says to pay, and counts off the public ledger who has
paid it.

## Scope

It verifies. It does not sign, send, hold a key, retry or cache, and `verify` itself has no
command line. That is on purpose: a verifier that never touches a secret is one you can read
in a sitting and drop into a seller's request path. (`nano_payers` does have one, because an
audit is something a stranger runs once from a shell, not something on a request path. It
reads the same way: no key, no send. So does `nano_claim`.)

It also keeps no record of what it has seen, so **spending a block hash once is yours to
enforce**. A settled receipt says this block paid you that amount; it does not say the block
has not already been spent on an earlier call. Store the hash with the call it paid for and
refuse it the second time, or one payment buys every call the buyer cares to make.

## Licence

MIT — see [LICENSE](LICENSE).

## Is anybody actually paying this seller?

`verify` answers *did this payment settle?*. Before a buyer has paid anything at all it
asks something earlier: **is this seller real, and is the account it advertises the account
that gets paid?**

`nano_payers` answers that from two places the seller does not control the reading of — the
seller's **own** x402 document, and the **public ledger**. So a stranger holding nothing but
a URL can re-derive every number, and a seller claiming traffic it has not got is
contradicted by the ledger rather than argued with.

```
python nano_payers.py https://extract.paypercall.dev/.well-known/x402
```

```python
import json, urllib.request
from nano_payers import payees_from_manifest, payers

doc = json.load(urllib.request.urlopen("https://seller.example/.well-known/x402"))
for payee in payees_from_manifest(doc):     # offline: it parses, it does not fetch
    report = payers(payee.account, ["https://rpc.nano.to"])
    print(payee.account, payee.prices_raw)
    print(report.distinct_payers, report.payments, report.received_raw)
    print(report.repeat_payers, report.repeat_rate)
```

`distinct_payers` cannot tell an account that paid once from one that pays every week, so
each report also carries `repeat_payers` (accounts with two or more confirmed payments) and
`repeat_rate` (`repeat_payers / distinct_payers`, four places, `0` when nobody has paid);
per payer, `payments` and `first_timestamp`/`last_timestamp` (when the payee's receive was
seen, i.e. when the money was collected). Both are recomputed by `outside_payers`.
A repeat is any second payment, including one seconds after the first: read `repeat_rate`
together with each payer's first and last timestamps before calling it returning custom.

Both x402 shapes are read, because they are different documents: a **catalogue**
(`resources[]`, what a seller serves at `/.well-known/x402`) and a single **challenge**
(`accepts[]`, what one resource answers a 402 with). An entry on another rail is skipped —
a seller taking USDC beside XNO is doing nothing wrong — but a Nano entry that cannot be
paid is refused and named: `payee_checksum` (an address XNO would vanish into),
`network_unspecified` (a bare `nano`, which names the family and not the network, and
Nano's test networks share the `nano_` prefix), `not_mainnet`, `amount_not_raw` (a price
written `1e26` or `0.0001`, or as a JSON number, which cannot carry 30 digits exactly).

What the count will **not** include, each because including it overstates what a seller has
been paid:

- a **send** on the payee's chain — that is money leaving, not income;
- an **unconfirmed** receive — it can still be rolled back, so it is reported separately
  and never counted;
- a receive **from the payee itself** — moving your own money is not a customer;
- the same account in both spellings — `nano_` and `xrb_` are one payer, counted once;
- a chain the node did not read back to its open block — that raises `HistoryIncomplete`
  rather than reporting a smaller business, which is the one error a caller cannot see.

An endpoint that serves a reply this cannot read, an amount that is not an integer, or half
a chain is one `payers` walks away from and asks the next node about — failing over on a
*usable answer*, not on an HTTP 200. The report names the node it was read from, and it is
still one node's word; `payers_corroborated` requires several to name the **same set** of
paying accounts, and raises `Disagreement` with the accounts each one named when they do
not. Amounts and timestamps are deliberately not compared across nodes: `local_timestamp`
is when each node saw a block and legitimately differs.

**It cannot tell a customer from the seller funding itself.** The ledger records that an
account sent XNO, never why. `outside_payers(report, our_accounts)` takes that judgement
out of the count and puts it where it belongs — with whoever knows which accounts are
theirs — and nothing here infers ownership from the size of a number:

```python
from nano_payers import outside_payers

real = outside_payers(report, ["nano_<our own funding account>"])
real.distinct_payers       # accounts that are not us
real.repeat_rate           # the share of them that paid more than once
```

Measured against a live seller on 2026-10-08 — `extract.paypercall.dev`, whose document
advertises one payee across 24 resources priced from `100000000000000000000000000` raw:
**10 distinct accounts have paid it, 27 confirmed payments, 71618000000000000000000000000000
raw**, of which 5 accounts paid only whole multiples of the advertised per-call price.

### Witness: what a reading is true as of

A payer count, and any "this payer is our own money" attribution, is true as of a block,
not forever. Each payer report therefore carries `read_at` (UTC, when the read started) and
`witness`: `frontier`, the newest block of the payee's chain the history was read down from,
and `block_count`, its height. A partial read witnesses nothing (`frontier` and
`block_count` are `null`), and a corroborated report carries a frontier only when every node
read from the same one. `independence` does the same per payer: for each hop, the account's
`checked_at_frontier` and `block_count`, its `open_block`, and the `funding_send` the
attribution rests on. The re-read rule: compare frontiers before comparing counts. A later
read whose frontier is the same read the same chain; one whose frontier differs has seen
that account move, and may flip an attribution - a payer once funded only by its operator
can since have been paid from elsewhere, and the reverse.

## How many independent payers?

Counting paying keys is not counting buyers: one operator can pay from ten accounts.
`nano_independence` groups payer accounts by where their money came from - the send that
opened each account - and counts each group once. Read-only; same bounded node call.

`nano_payers.payers` produces exactly the list it wants, so the two compose into the whole
question: from a seller's URL to how many independent payers it has. On the live seller
above, the 10 paying accounts group into **7** independent payers - three of the small
per-call payers were funded from one account, and so are one operator however separately
they paid.

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

Nor can it see collusion. Two parties with money of their own who agree to pay each other
read as two independent payers, because they are: the ledger shows where money came from,
never why it moved. A count of independent payers says the payers do not share a funder; it
does not say the payments were for real work. That needs a delivery record the buyer signed,
which lives outside the ledger.

## Was the send claimed?

A Nano send does not land in the destination by itself: the account it paid has to publish
a receive block naming it, and an account that has never received anything does not exist
on the ledger yet. `nano_claim` reads one send hash off the public chain once and says which
of those it is:

```
python3 nano_claim.py <send-block-hash> [rpc-url]
```

```json
{"send_hash": "243AF6BFC56A199CB2C40C0ABFF4C583B905615E68F54D6CBCCD1A5F0D0DF57C",
 "from": "nano_1434j1n4sin4cefs5njibag4tsmo596fmg3s6bdogtod3ndmdfez5yuebrh9",
 "to": "nano_3rra4cwps4w6oh8sfctde3tt1g3tjgh39tgxbxdkbn8kmu5yykmgtqaehp6p",
 "amount_raw": "10000000000000000000000000", "send_confirmed": true,
 "outcome": "receivable", "receive_hash": null, "to_account_opened": false,
 "read_at": "2026-10-08T22:30:14Z", "source": "https://rpc.nano.to", "error": null}
```

`outcome` is one of four words. `claimed`: a confirmed receive on the destination's chain
links this send, and `receive_hash` is that block. `receivable`: the node still holds the
send for the destination; `to_account_opened` says whether that account exists yet. Both
exit 0. `unknown` (exit 2): the read did not settle it - a node call failed or answered
something that is not a node reply, the receive was not among the destination's newest 500
blocks, or the facts disagree; `error` says which. `refused` (exit 3): a malformed hash or a
block that is not a send. A read that could not run never comes back as `claimed` or
`receivable`.

It makes three kinds of read call - `blocks_info`, `account_info`, `account_history` - and
nothing else. The outcome field was asked for by an agent on Moltbook.
