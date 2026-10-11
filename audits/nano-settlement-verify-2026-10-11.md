# nano-settlement-verify — audit 2026-10-11

Previous audit 2026-10-10. Audited at `e39e37e` (`main` after #25).
Lens unchanged: can an agent get PAID in XNO with this today without being hurt —
refused when it was paid, told it was paid when it was not, or dropped mid-request.

`python3 -m pytest -q`: **350 passed** on `e39e37e`, **354** on this branch.

## What I checked

- `nano_settlement_verify.py` end to end: the `expect_raw` and `not_after` type
  refusals, `confirmed`, the amount comparison, `_operation`, `_paid_account`,
  `_same_account`'s two spellings, the `Late`/`UnknownTime`/`NotANodeReply`
  families and which of them is a verdict about the money.
- `nano_quorum.py` end to end: `distinct_endpoints`, the first-`mismatch` refusal,
  `Disagreement`, `NotCorroborated`, and `balance_corroborated`'s confirmed-only
  figures.
- The shipped skill bundle (`skills/nano-settlement-verify/`): SKILL.md's contract
  against `verify_cli.py`'s behaviour, and the vendored library against the one
  the suite tests.
- Pattern sweep over every module: no `float()` on an amount anywhere (the only
  non-integer arithmetic is `nano_payers.py:214`, `repeat_payers / len(payers)`,
  a ratio), no `subprocess`, `os.system`, `eval`, `exec` or `shell=`, no `open()`
  on anything derived from input, and no secret of any kind in the tree.

## Found and fixed

**The skill declared `NANO_RPC_URL` and read it nowhere, so the node that decides
whether money arrived was never the one the operator chose.** Named by the
2026-10-10 audit (item 3) and left for a run of its own; this is that run.

`skills/nano-settlement-verify/SKILL.md:10-13` declares the variable under
`metadata.openclaw.requires.envVars` — *"Nano node RPC to ask. Defaults to
https://rpc.nano.to"* — and `verify_cli.py:61` read

```python
rpc_url = argv[4] if len(argv) == 5 else "https://rpc.nano.to"
```

with no environment lookup anywhere in the bundle.

Measured first-hand, with a loopback stub on `127.0.0.1:7177` that answers a
confirmed send of exactly the quoted amount to exactly the quoted account, and
`NANO_RPC_URL` exported to it, running the three-argument command SKILL.md
documents:

| | stub requests | exit | stdout |
| --- | --- | --- | --- |
| before | **0** | 3 | `{"verdict": "no_such_block"}` |
| after | **1** | 0 | `{"settled": true, "amount_raw": 1000000000000000000000000000000, ...}` |

Exit 3 is *do not serve*. So a seller whose buyer really had paid, the right
amount to the right account, refused the call — and did so because the only knob
the manifest offers was ignored, with the request going to `rpc.nano.to` instead,
the endpoint `nano_quorum.py:5-6` records as wanting a key and rate-limiting a
burst. It also made `README.md:166` — *"Ask a second node you chose, not one the
seller named"* — untrue for the bundle: the single endpoint deciding settlement
was not the operator's.

Fixed in `verify_cli.py` only: the default is now
`os.environ.get("NANO_RPC_URL") or "https://rpc.nano.to"`. Nothing else moved —
an explicit `RPC_URL` argument is the narrower instruction and still wins, and an
unset or empty variable leaves the default byte-identical to before. No verdict
about any payment changes for any caller that was already passing the endpoint.

Five tests, two of which fail on `e39e37e`:

- `test_nano_rpc_url_chooses_the_node_that_decides_the_payment` — fails before
  (asks `https://rpc.nano.to`), passes after.
- `test_the_bundle_reads_every_environment_variable_it_declares` — the law the
  2026-10-10 audit prescribed: every name SKILL.md declares under `envVars` must
  appear in a `.py` file of the bundle. Fails before.
- `test_an_explicit_rpc_url_argument_still_wins_over_the_variable` and
  `test_the_default_endpoint_is_unchanged_when_the_variable_is_unset` are
  controls: both pass before and after, which is what makes this a narrowing.
- The README test-count law (#19) required the page's count to move 350 → 354.

## Two gaps in that fix, found in review and fixed on the same branch

The review of the above (commit `2f6fca9`) found two things my fix did not cover.
Both are real; I reproduced each independently before accepting it, and the note
records them because the second one was a credential leak.

1. **A whitespace-only `NANO_RPC_URL` was truthy**, so `"   "` was asked as the
   endpoint and every verification exited 4 — the same holding-a-paid-call
   failure the fix exists to remove, reintroduced for a padded `export`. My
   control test used `""`, which the `or` already handled, and so proved less
   than it looked. Now `.strip() or DEFAULT_RPC_URL`: empty, blank and padded
   all behave. Measured by hand on the merged branch — blank and empty both ask
   `https://rpc.nano.to`, and `"  http://127.0.0.1:7177/x  "` asks
   `http://127.0.0.1:7177/x`.
2. **A URL with userinfo printed its password in a traceback.** urllib raises
   `http.client.InvalidURL` for `http://user:pw@host/`, which is an
   `HTTPException` — neither `OSError` nor `ValueError` — so it escaped the arm
   the README tells a seller to write *and* the CLI's own handler, exiting 1
   with the credential on stderr, outside SKILL.md's exit table entirely. This
   is the `not_after` defect of 2026-10-10 one argument over, with a secret in
   it. `HTTPException` now joins the unreachable-node arm and the detail is
   redacted. Measured: `NANO_RPC_URL=http://seller:sup3rsecret@127.0.0.1:1/`
   exits **4**, and `sup3rsecret` appears in neither stdout nor stderr.

The declared-variable law I wrote was also tightened, and it needed to be: it
searched the bundle's `.py` text for the name, which a comment or a docstring
mentioning `NANO_RPC_URL` would have satisfied without any code reading it. It
now requires a real `os.environ` / `os.getenv` read.

Suite on the branch head: **360 passed** (350 on `main`, 354 after my commit).

## Still open — the owner's one sentence, not a blocker

The repository spells the variable two ways: SKILL.md declares `NANO_RPC_URL`,
while `e2e_payers_check.py:171` and `e2e_quorum_check.py:178` read `NANO_RPC`.
This fix honours the declared name, because that is the one an installing agent
configures against and the one that was broken; the two e2e scripts are
developer tools and are untouched. If `NANO_RPC` is meant to be canonical, the
cheap follow-up is the CLI accepting it as a second fallback — one line, and the
new law already covers whichever name the manifest ends up declaring.

## Could not verify

- **No live payment of our own.** The measurement above uses a loopback stub
  whose reply shape is copied from a real `rpc.nano.to` answer; no XNO moved in
  this run.
- **Whether a seller has already refused a paid buyer on this.** Not knowable
  from here. The exposure is every installation of the skill bundle that set the
  variable, for as long as the manifest has declared it.
