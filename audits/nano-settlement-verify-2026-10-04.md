# nano-settlement-verify — audit 2026-10-04

Lens: can an agent prove it got paid in XNO with this, today, without being hurt?
Previous audits: 2026-09-25, 2026-10-01, 2026-10-02. This run concentrated on what has
landed since the last one — `nano_quorum.py` and the OpenClaw skill bundle (PRs #7, #8) —
plus a re-read of `verify` from the caller's side rather than the node's.

## Checked

- `python3 -m pytest -q`, `python3 e2e_check.py`, `python3 e2e_quorum_check.py` — the three
  commands `.github/workflows/test.yml` runs. Green before the change (153 passed) and after
  (159 passed).
- `verify` end to end: the confirmed check, the amount comparison, the send-only check and the
  payee comparison, and the order they run in.
- Amount arithmetic across all five modules. Raw is read with `int()` and never with `float()`;
  `expect_raw` is refused outright when it is not an `int`, including `bool`. No float reaches
  an amount anywhere.
- `nano_quorum.py` (new since the last audit) in full: `distinct_endpoints`' deduplication,
  the fail-closed `Mismatch` path, `Disagreement`, `NotCorroborated`, and the
  `_UNREADABLE` classification.
- The exception hierarchy, for shadowing: `NotFound`, `Mismatch`, `NotCorroborated` and
  `Disagreement` all subclass `Exception` directly, so none of them is swallowed by
  `_UNREADABLE`'s `(OSError, ValueError, KeyError, TypeError)`.
- Whether anything trusts what the payer submitted. The payer supplies only a block hash;
  every fact — confirmation, amount, operation, payee — comes from the node and is compared
  against the caller's own expectation. Nothing is taken from the payer.
- A live call to `https://rpc.nano.to` for the reply shapes a real public endpoint returns.
- Secret scan of the tree.

## Found and fixed

**`verify` raised a bare `KeyError` on a JSON body that is not a node reply, escaping the
error handling the README tells a seller to write.**

`README.md:100-110` gives the seller two tables of what `verify` can raise and then says, of
the transport arm, *"Catch `OSError` and `ValueError` around `verify` and retry or hold — the
example above does"*, warning that *"without this arm the first hiccup of a public node raises
straight through it"*. `verify` read three fields with bare subscripts —
`reply["confirmed"]` (`nano_settlement_verify.py:188`), `reply["amount"]` (`:190`) and
`reply["height"]` (`:207`) — so a 200 response carrying JSON that is not a `block_info` reply
raised `KeyError`, which is neither an `OSError` nor a `ValueError` and so went through that
arm and out of the seller's request path.

The case is not hypothetical: `nano_quorum.py:165` already describes it in as many words —
*"a body that IS JSON but is not a node reply - a proxy's status page, an error envelope with
no `confirmed` - which `verify` raises on by design"* — and lists `KeyError` in `_UNREADABLE`
so that `verify_corroborated` survives it. A seller following the README's single-node
quickstart had no such protection.

Measured, driving the README's own seller against each shape:

```
BEFORE (main):
  a proxy's status page      -> *** ESCAPED the seller's handlers: KeyError ***
  a rate-limit envelope      -> *** ESCAPED the seller's handlers: KeyError ***
  confirmed but no amount    -> *** ESCAPED the seller's handlers: KeyError ***

AFTER:
  a proxy's status page      -> HELD by the README's arm (NotANodeReply)
  a rate-limit envelope      -> HELD by the README's arm (NotANodeReply)
  confirmed but no amount    -> HELD by the README's arm (NotANodeReply)
```

By the README's own reasoning this is the costliest outcome in the package: an uncaught
exception on the request path drops a call the buyer may already have paid for, and on a
feeless, irreversible rail the XNO does not come back.

**The fix adds a refusal and widens nothing.** `NotANodeReply(ValueError)` is raised through a
single `_required()` helper, which replaces each bare read in place so the order of the existing
checks is untouched. Every outcome that was reachable before is byte-identical:
`{"error": ...}` is still `NotFound`, `"confirmed": "false"` is still an unsettled receipt, a
wrong amount or payee is still `Mismatch`, and a non-integer amount is still a `ValueError`.
The only paths that change are the three that previously raised `KeyError`, and they now raise
a `ValueError` subclass instead. Nothing that verified before stops verifying, and nothing new
verifies.

`nano_quorum` behaviour is unchanged, because `NotANodeReply` is a `ValueError` and
`_UNREADABLE` already catches `ValueError`; the endpoint still counts as silence.
`tests/test_quorum.py`'s assertion on the recorded failure string was updated for the new type
name, its stated intent ("counts as silence") unchanged. `balance_corroborated` reads a balance
reply on its own path, which this change deliberately does not touch, so its `KeyError`
classification and the test pinning it stand.

The test that pinned the old behaviour,
`test_a_reply_with_no_confirmed_field_raises_key_error`, asserted the exception *type* while its
docstring states the intent as *"A malformed reply is not quietly treated as settled or as
unsettled"*. That intent is preserved; the test is renamed and now asserts `NotANodeReply`.

Failing-then-passing: six new tests, four of them parametrised over the reply shapes above,
asserting that what `verify` raises is catchable as `(OSError, ValueError)` and is neither
`NotFound` nor `Mismatch`. Reverting `nano_settlement_verify.py` alone and running the README's
seller reproduces the three escapes exactly as printed above.

README: the missing row is added to the outcomes table, the example's comment names the case,
and the stated suite size moves 153 → 159.

## Checked and clean

- **No send path exists here at all.** No `process`, no signing, no key handling, no seed, in
  any of the five modules — confirmed by reading, not only by the README's claim. This package
  only reads.
- **The replay limitation is disclosed, not hidden.** `verify` will settle for any confirmed
  send of the right amount to the right account, including one the payer already spent on
  another order; Nano carries no memo, so binding a payment to an order is out of scope here.
  `README.md:220` and `:258` and `nano_terms.py:208` each say "spending one block hash once is
  yours to enforce" in those words. Not a defect: a documented boundary, and the repository
  that closes it is `nano-invoice`.
- **A receive or open block cannot pass as a payment.** `verify` requires `operation == "send"`,
  so the hash of any confirmed inbound block on the seller's own chain is refused. A state
  block's `contents.type` of `"state"` is deliberately not in the operations table, so a reply
  carrying no subtype fails closed.
- **Both account spellings are one account.** `nano_` and `xrb_` compare equal, and the receipt
  records the node's canonical `nano_` form.
- **`distinct_endpoints` is honest about what it can and cannot fold.** Case and a trailing
  slash only; it says in its own docstring that it resolves no DNS and follows no redirects, so
  two names for one node still count as two, and it refuses a list too short to support `agree`
  *before* any call is made rather than silently folding a duplicate into a false quorum.
- **A single `Mismatch` fails closed immediately** rather than polling for a node that has not
  caught up and reading its silence as agreement.
- **`NotCorroborated` is distinguished from an unsettled receipt** — "nobody could tell us"
  versus "they told us it is not confirmed yet" — which is the distinction that decides whether
  a seller holds a call or refuses one.
- No secret in the tree.

## Could not verify

- **The live public-RPC non-reply shapes were not captured first-hand.** `https://rpc.nano.to`
  answered `{"error": ...}` for every probe made here, which `verify` has always handled as
  `NotFound`. The proxy status page and rate-limit envelope are the shapes `nano_quorum.py:165`
  names from the package's own experience, reproduced here as fixtures rather than observed
  live. The fix does not depend on the exact body: it covers any JSON reply missing a field a
  `block_info` answer must carry.
- **Node independence is still unmeasurable from here**, as the previous audits recorded.
  Several public Nano RPCs are proxies in front of the same node; `nano_independence.py` groups
  by funding chain, not by operator, and `distinct_endpoints` says so.
- The OpenClaw skill bundle under `skills/` was read but not exercised through ClawHub.
