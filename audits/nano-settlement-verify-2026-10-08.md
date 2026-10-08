# nano-settlement-verify — audit 2026-10-08

Previous audit 2026-10-05. Audited at `e15bf0f` (`main` after #10). Lens: can a
seller agent prove it was paid in XNO with this, today, without being hurt?

Python 3.11, standard library only. Baseline on `main`: `pytest -q` **162
passed**, `e2e_check.py` and `e2e_quorum_check.py` both green, `py_compile`
clean over every module, the tests and the skill bundle. After this change:
**171 passed**, both e2e checks green, compile clean.

## Found and fixed (this PR)

**`nano_terms.load_terms` accepted a payee that cannot be paid.** Found by the
2026-10-01 audit, re-measured and left open by 2026-10-02, still live on `main`
a week later. It is fixed here.

`nano_terms.py:160-162` validated the pinned payee with

```python
if nano_settlement_verify._account_body(payee) in (None, ""):
```

which only asks whether the string starts with `nano_`/`xrb_` and has *something*
after it. No length, no alphabet, no checksum. So `"nano_1a"` was accepted as
the payee of a set of terms **both sides hash and rely on**, and the buyer then
pays `terms.amount_raw` to it with its own wallet. A Nano send is irreversible:
that is money that leaves and never arrives, and the loss falls on the buyer who
did everything the module asked.

A Nano address exists in a form that makes this checkable by anyone, offline: 60
characters after the prefix, the first 52 carrying four zero padding bits and the
256-bit public key, the last 8 carrying a 5-byte blake2b digest of that key,
reversed. The checksum is there precisely so a mistyped address is caught before
the money moves.

Fixed by `is_valid_account` / `public_key_from_address` in
`nano_settlement_verify.py` — stdlib `hashlib.blake2b`, no dependency added, the
same decoder shape as `nano-invoice/nano_invoice/account.py` and
`agent-wallet-multirail/mandate.py` — called from `load_terms`. It refuses a
payee; it accepts none it refused before. `_same_account` and `verify` are
deliberately **untouched**: a seller who passes a malformed `account` to `verify`
already gets `Mismatch`, so validating there would change no outcome and would
widen a diff on the one function a seller has on its request path.

**Why the two earlier audits left it, and why that no longer holds.** The stated
reason was fixture churn — "every terms fixture in the suite uses a placeholder
(`nano_1a` 39 times, `nano_3abc` 16, `nano_1b` 18, …)". Measured, that count is
of the whole repository. Only `tests/test_terms.py` reaches `load_terms`, and it
pins its addresses in **one module constant**: `PAYEE`, plus two literals used as
a node's `block_account` and as a different paid account, neither of which goes
through the payee check. Three constants, now real checksum-valid addresses, and
`test_the_fixture_addresses_are_payable` holds them to it so a later edit cannot
reintroduce a placeholder and make this file's refusals pass for the wrong
reason.

Nine tests added. **Eight fail with `nano_terms.py` and
`nano_settlement_verify.py` reverted to `main`** (the seven unpayable payees, and
the fixture-payability test, since `is_valid_account` does not exist there). The
ninth is a control: both spellings of one payee, `nano_` and `xrb_`, are still
accepted — the checksum check must not cost the legacy spelling a node never
answers but older tooling stores.

The skill bundle's vendored copy was re-synced, and
`test_the_vendored_library_is_the_library` — the guard added after #10 shipped a
stale copy — failed until it was. That guard works.

## Also checked, found clean

- `verify` end to end again: the `block_info` request shape, the `confirmed`
  gate, `int()` on the amount, the send-only `_operation` gate (a `state`
  `contents.type` names no operation and fails closed), and the payee read from
  `contents.link_as_account` / `destination` rather than `block_account`, which
  for a send is the *payer*.
- `expect_raw` refuses a non-`int`, `bool` included, **before** the node is
  called. No float touches an amount anywhere in the package.
- `_required` routes every mandatory field through `NotANodeReply` (a
  `ValueError`), so a proxy's status page lands in the arm the README tells a
  seller to catch rather than as a bare `KeyError` on its request path.
- `nano_terms`: the sha256 self-addressing, the duplicate-key hook (two parsers
  disagreeing on which duplicate wins would let the sides read different terms
  out of the very bytes they both hashed), `_RAW` as `[1-9][0-9]*` with
  `fullmatch` (no sign, no exponent, no leading zero, ASCII only), and
  `settle` checking the acceptance belongs to these terms.
- `nano_quorum`: a `Mismatch` from any endpoint that has the block confirmed
  fails closed rather than being out-voted; `balance_corroborated` reads only
  `confirmed_*` figures.
- The package holds no send path at all — no signing, no `process`, no key, and
  `post_json` is the only network call. Nothing here can move XNO.
- Secret scan of the tree and of `git log -p`: none found.

## Could not verify

- **No live node.** Every test stubs `urlopen` (and a fixture asserts a socket
  was never opened), so the request shape is checked against recorded replies,
  not against `rpc.nano.to`; this session's egress proxy does not reach one. No
  XNO moved.
- **The addresses used as fixtures are valid, not funded.** They are derived
  from a hash, so no key exists for them and nothing should ever be sent to
  them.
- **`verify` still reads a JSON boolean `confirmed` as unconfirmed**
  (`nano_settlement_verify.py:196`, `!= "true"`). Left alone again, for the
  2026-10-02 reason, which this run could not improve on: a real `nano_node`
  answers the string, and no node or proxy answering the bool has been shown.
  The failure direction is safe (a confirmed payment reads as not yet settled, a
  refusal) but it is a sale refused, so if the bool is ever seen in the wild it
  is a one-line fix. Recorded here rather than fixed on a sibling's
  defensiveness alone.
