# nano-settlement-verify — audit 2026-10-10

Previous audit 2026-10-08. Audited at `4b6e931` (`main` after #22).
Lens: can an agent get PAID in XNO with this today without being hurt — refused
when it was paid, told it was paid when it was not, or dropped mid-request.

`python3 -m pytest tests -q`: **330 passed** on `4b6e931`, **350** after #23 and
#24. `e2e_check.py` passes end to end.

The 2026-10-08, -10-05 and -10-04 audits' open items are not re-reported here.

## Found and fixed

**1. An unreadable node reply was a verdict about the money, not a hold
(#23, merged).** This package fails closed two ways and they are not
interchangeable:

| | means | exit | in the quorum |
|---|---|---|---|
| `Mismatch` | the ledger says something other than what was asked | 3, do not serve | raised on the **first** one heard |
| `NotANodeReply` (a `ValueError`) | nothing was read | 4, hold and ask another node | that endpoint is skipped |

`SKILL.md:54` already states exit 4's meaning: *"nothing was checked; retry or
try another RPC; do NOT treat this as a bad payment."* Three readings put that
case in the first family.

- `_operation(reply)` returns `""` when a reply carries no `subtype` and a
  `contents.type` of `"state"`, or when `contents` came back as an opaque string
  because the endpoint ignored `json_block` (`_contents` reads that as `{}`, by
  its own docstring). That became `Mismatch("", "send")`.
- `_paid_account(reply)` returns `""` when none of the four places a payee can
  appear is readable. That became `Mismatch("", account)` — telling a seller
  whose buyer really had paid, the right amount to the right account, that it
  had been paid by nobody.
- `nano_claim.py:252` raised the terminal `Refused("not_a_send")` on the same
  empty operation; its message even read *"is a unknown block"*. `Refused`
  carries no `reconcile`, no `absence_scope` and no retry, and `except Refused:
  raise` sent it past the `_Unknown` arm and out of `claim_status`'s node loop,
  so **node order decided the verdict** — `[proxy, honest]` read `refused`/exit
  3 where `[honest, proxy]` read `claimed`/exit 0, and the honest endpoint was
  never asked. The next line already treated an unreadable *destination* as
  `_Unknown`.

In the quorum this was worse than one wrong answer: one proxy anywhere in the
list manufactured a `mismatch`, which `verify_corroborated` raises on at once on
the reasoning that a confirmed block's contents are fixed — true of an endpoint
that read the block, false of one that read nothing. An endpoint that had already
reported the block settled was overridden and the rest were never called.

The trigger class is not hypothetical: `rpc.nano.to`, this package's own default
endpoint, silently ignores `raw: "true"` on `account_history`, which
`nano_claim.py:186-188` already records. A public endpoint dropping a request
flag is the same behaviour one field over.

Fixed by naming the empty case: `verify` raises `NotANodeReply`, `nano_claim`
raises `_Unknown`. A reply that names a real operation other than `"send"`, or a
real payee that is the wrong one, is unchanged — still `Mismatch`, still exit 3,
still immediate in the quorum. **Nothing newly settles from an unreadable
reply**: a settlement still requires `agree` endpoints each reading a confirmed
send of the right amount to the right account.

Ten tests fail with the three code files reverted; three of them are existing
tests re-pinned, because they asserted the family this changes — each keeps its
own law (nothing settles) and says in its docstring why the family moved. Four
of the new tests are controls that hold either way.

**2. A deadline that is not a number escaped the seller's own error handling
(#24, merged).** `expect_raw` is type-checked before the node is called;
`not_after` was never checked at all. It is used once, in the **last** thing
`verify` does — `if seen_at > not_after` — which runs only after the block is
confirmed and the amount and payee have matched. So a deadline read back as a
string from a JSON order record or a config file raised

```
TypeError: '>' not supported between instances of 'int' and 'str'
```

on exactly the payments that had actually arrived; a missing or 0
`local_timestamp` raises `UnknownTime` first and hides it, which is why no
happy-path test caught it. `TypeError` is neither `OSError` nor `ValueError`, so
it escaped the arm `README.md:107` tells a seller to write and the same arm in
`verify_cli.py:79` — an uncaught exception on the seller's request path,
dropping a call the buyer has already paid for on a feeless irreversible rail.
This is the class the 2026-10-04 audit called *"the costliest outcome in the
package"* when it was a bare `KeyError`, in the one argument that fix did not
reach. A **float** deadline works, so the failure is spelling-dependent and easy
to ship.

Checked beside `expect_raw`, before the node call, raised as a `ValueError` —
the family `parse_not_after` already uses, and the one the seller is already
holding on. `int` and `float` are both still accepted (`time.time() + 300`);
`bool` is not, because `True` is a deadline one second after the epoch and would
mark every payment late. No verdict about any payment changes. Eight tests fail
with the module reverted.

## Found, NOT fixed — named here for a run of its own

**3. The shipped skill declares `NANO_RPC_URL` and never reads it**, so every
verification goes to `rpc.nano.to` instead of the node the operator chose.
`skills/nano-settlement-verify/SKILL.md:10-13` declares the variable
("*Nano node RPC to ask. Defaults to https://rpc.nano.to*");
`verify_cli.py:61` is `rpc_url = argv[4] if len(argv) == 5 else
"https://rpc.nano.to"` and reads no environment variable at all.
`grep -rn "NANO_RPC_URL"` over the tree matches that manifest and nothing else.
Measured: with `NANO_RPC_URL` pointed at a loopback stub that would answer
`settled: true`, the documented three-argument command sent the stub **0**
requests and returned exit 3 (*do not serve*) from `rpc.nano.to`, which does not
hold that hash; the identical URL as `argv[4]` hit the stub once.

Two harms: a seller that set the variable *because* `rpc.nano.to` "wants a key
and rate-limits a burst" (`nano_quorum.py:5-6`) gets rate-limited into exit 4 or
403 and holds calls it was paid for; and `README.md:166` — *"Ask a second node
you chose, not one the seller named"* — is silently untrue, because the single
endpoint deciding whether money arrived is not the one the operator picked.
`test_skill_bundle.py` checks the vendored library's bytes and the exit-code
table, so nothing in the suite touches this.

Not fixed in this run because it is a third concern and belongs on its own
branch, and because the repository's own convention disagrees with itself — the
two e2e scripts read `NANO_RPC`, the manifest declares `NANO_RPC_URL`. The fix
is `verify_cli.py` honouring the declared name as its default, plus a law in
`test_skill_bundle.py` that every variable SKILL.md declares is read somewhere
in the bundle. Which of the two names is canonical is worth one sentence from
the owner first.

## Also checked, found clean

- **Live, against the real ledger.** `README.md:358`
  (`python nano_payers.py https://extract.paypercall.dev/.well-known/x402`) runs
  and reproduces the README's measured numbers exactly: 1 payee, **10 distinct
  payers, 27 confirmed payments, 71618000000000000000000000000000 raw**,
  `repeat_payers 5`, `complete: true`, witness frontier `B7DBFAE8…E441` at
  height 29. `nano_independence.independence` on that payer set gives 10 keys →
  **7 independent**, the `README.md:450` claim. `nano_claim.py` on the
  `README.md:485` example hash reproduces its printed JSON field for field.
- **`nano_payers` paging does not drift a single raw.** `HISTORY_PAGE` forced to
  5, 7 and 10 against the live 29-block chain (7 pages, `head` re-serving a
  duplicate each time) gives identical `blocks_read`, `payments`,
  `distinct_payers` and `received_raw`; `delta_raw = 0` in all three.
- **No float on an amount anywhere.** Only `repeat_rate` (a ratio) and
  `received_raw // 10**27` (integer division, display only).
- **Nothing trusts the payer.** The payer supplies a block hash and nothing
  else; confirmation, amount, operation and payee all come from the node.
  `nano_payers` takes the payee from the seller's own x402 document and the
  payers from the ledger.
- No secret in the tree; no `subprocess`, `os.system`, `eval`, `exec` or
  `shell=`, and no `open()` on anything derived from input outside `tests/`.
- `pip install .` and `python -m pytest -v` are sound: `pyproject.toml` lists
  all six modules under `py-modules` with `pythonpath = ["."]`.

## Could not verify

- **No live payment of our own.** The live reads above are of someone else's
  settled chain; no XNO moved in this run. Every fixed case is measured against
  stubbed node replies whose shapes are copied from `rpc.nano.to`.
- **Whether a real endpoint in the wild actually serves `contents` as a string.**
  The reply used in the proofs is a real `rpc.nano.to` answer for a real
  confirmed send with only `contents` re-serialised. That `rpc.nano.to` drops
  `raw` on `account_history` is first-hand; that some endpoint drops
  `json_block` on `block_info` is inferred from the same class of behaviour, not
  observed. The fix is safe either way — it only moves a refusal to a hold.
- **Whether any seller has already refused a buyer on this.** Not knowable from
  here.
