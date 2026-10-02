# nano-settlement-verify — code audit, 2026-10-02

Clone of `main` at `f6ecb79`. Baseline, before any change:

```
$ python -m pytest -q          # 113 passed
$ python -m py_compile *.py    # clean
```

Earlier runs read `verify`, `nano_terms` and `nano_independence` as source. This run
instead drove `verify` against the shapes a **real public node actually answers with**,
including the ones that are not JSON, which is where the finding is.

## Found and fixed

**The README enumerated the outcomes of `verify` and left out the one that crashes a
seller's request path.** `README.md` listed four outcomes and called them
"Four outcomes, and nothing else", and its quickstart catches `NotFound` and `Mismatch`.
`post_json` (`nano_settlement_verify.py:78-89`) has no error handling — deliberately: there
is no retry and no wrapper exception — so a transport failure reaches the caller as itself.
Driven against real sockets:

| what was done to the node | what `verify` raised |
|---|---|
| nothing listening on the port | `urllib.error.URLError: [Errno 111] Connection refused` |
| answers HTTP 500 | `urllib.error.HTTPError: HTTP Error 500` |
| answers `<html>down for maintenance</html>` | `json.JSONDecodeError: Expecting value: line 1 column 1` |

None of the three is `NotFound` or `Mismatch`, and the README named none of them. The third
is not exotic: `README.md:16-18` already records that Cloudflare-fronted RPCs answer a bad
User-Agent with a 403 error page, and this library's whole pitch (`README.md`, Scope) is
that it is small enough "to drop into a seller's request path". A seller that copied the
quickstart gets an unhandled exception out of that path the first time a public node
hiccups — and worse if it reads an unreachable node as a missing block, because refusing a
call the buyer already paid for does not give the XNO back.

**Fixed in the README**, which is the half this audit may change — the code is deliberate
and is unchanged. The outcome table is now "Four outcomes when the node answers", followed
by a second table naming `URLError`, `HTTPError` and `JSONDecodeError`, a paragraph saying
plainly that these are *not* `NotFound` because "the node has no such block" and "the node
did not answer" are different facts, and a quickstart that catches
`except (OSError, ValueError)` and holds or retries rather than refusing.

### Failing then passing

```
# baseline on main
python -m pytest -q   ->  113 passed

# with the 4 new tests, README reverted
FAILED tests/test_verify.py::test_the_readme_names_the_node_unreachable_outcome
                      ->  1 failed, 116 passed

# with the fix
python -m pytest -q   ->  117 passed
```

Three of the four new tests pin the behaviour itself — a refused connection, an HTTP error
status and a non-JSON body each raise, and each assertion includes
`not isinstance(caught.value, (NotFound, Mismatch))`. They stub `urlopen` rather than open a
socket, so the repository's promise that no test touches the network still holds (and
`test_the_no_sockets_guard_really_bites` still passes). The fourth asserts the README names
all three and that its quickstart catches them.

## Found, NOT fixed — for a person to decide

**`nano_terms.load_terms` accepts a payee that cannot be paid.** `nano_terms.py:160-162`
validates the payee with `nano_settlement_verify._account_body(payee) in (None, "")`, which
only checks that the string starts with `nano_`/`xrb_` and has *something* after it. There
is no length, alphabet or checksum check, so `"nano_1"` is accepted as the pinned payee of
a set of terms both sides hash and rely on, and the buyer then pays `terms.amount_raw` to
it with its own wallet. This is the exact failure `nano-receipt-ledger/ledger/nanoaddr.py`
was written for and records in its module docstring: an outside agent handed over a payout
address that fails checksum and nothing in the pipeline caught it.

Not fixed here because the fix is not small: every terms fixture in the suite uses a
placeholder (`nano_1a` 39 times, `nano_3abc` 16, `nano_1b` 18, …), so enforcing a checksum
means rewriting them all and adding a ~20-line base32+blake2b decoder to a module whose
refusal set the README pins. That is a maintainer's call, not an auditor's. The shape, if
wanted: port `public_key_from_address` from `agent-wallet-multirail/mandate.py:266-285`,
which is stdlib-only and already does exactly this, and refuse with
`TermsRefused("invalid_payee", ...)`.

**`verify` reads a JSON boolean `confirmed` as unconfirmed.**
`nano_settlement_verify.py:196` is `if reply["confirmed"] != "true":`. A reply whose
`confirmed` is the JSON bool `true` — not the string — returns
`Receipt(settled=False, amount_raw=0, height=0, account="")` for a confirmed send, and the
README then tells the seller to "wait and ask again", which will never change. Driven with
an otherwise-valid state send and `"confirmed": True`: `settled=False`. Two sibling
repositories accept both forms —
`agent-wallet-multirail/src/agent_wallet_multirail/rails.py:177-181` ("A Nano node sends
`confirmed` as the string "true", so both that and a bool count") and
`nano-receipt-ledger/ledger/node.py:67` (`str(...).lower() == "true"`) — so this one is the
outlier. Not changed because **I could not show a node or proxy that answers the bool**: a
real `nano_node` answers the string, and changing this on the strength of a sibling's
defensiveness alone would be hardening a hypothesis. If the bool is ever seen in the wild
it is a one-line fix. Note that a reply with *no* `confirmed` field raises `KeyError` on
purpose, pinned by `test_a_reply_with_no_confirmed_field_raises_key_error` — that is
deliberate and was left alone.

## Checked, nothing to fix

- **No float touches an amount.** `verify` refuses a non-`int` `expect_raw` (and `bool`)
  before the node is called, and parses the node's `amount` with `int()`.
  `nano_terms._RAW` is `[1-9][0-9]*` with `fullmatch` — explicitly ASCII, so no leading
  zero, no sign, no exponent and no non-ASCII digit parses.
- **The account checked is the account paid, not the chain the block sits on.**
  `_paid_account` reads `contents.link_as_account` for a state block and
  `contents.destination` for a pre-state one, falling back to the top level; `block_account`
  is never used for the comparison. Both spellings of one account compare equal
  (`_same_account`), and a non-string `account` returns `None` and refuses rather than
  raising.
- **Only a send settles.** `_operation` takes `subtype` for a state block and
  `contents.type` for a pre-state one, and deliberately excludes `"state"`, so a reply
  naming no operation fails closed. A confirmed receive on the seller's own chain is
  refused.
- **`nano_terms` refuses an unknown field, a missing one, a duplicated key** (via
  `object_pairs_hook`, nested objects included), a wrong version, and an acceptance it
  cannot run; `settle` refuses an acceptance made against other terms and verifies against
  the pinned payee and amount, never against what the seller says afterwards.
- Every relative link in `README.md` resolves. `pip install .` succeeds; the module is
  importable as one file with no dependency, as the README says.

## Not verified here

No live payment and no live node: every test stubs the reply, and `e2e_check.py` was run
against its own loopback stub (it passes). The read-only-mount and DNS-failure variants of
the transport finding are not separately tested — `URLError` covers both and the committed
tests raise it directly rather than depending on the sandbox's network.

## Secrets

Clean. No credential, key or seed in the tree or in `git log -p` over the module files.
The 64-hex strings are Nano block hashes; the addresses are public accounts. This library
holds no key by design: it only reads.
