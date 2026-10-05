# nano-settlement-verify — audit 2026-10-05

Read through the one question that matters here: can a seller agent prove it was paid in
XNO with this, today, without being hurt?

## Checked

- `python3 -m pytest -q` — 159 tests green on `main` before any change.
- `e2e_check.py` (acceptance, wrong payee, receive path, both account spellings) and
  `e2e_quorum_check.py` (agreement, contradiction, silence, duplicate endpoints,
  balances) — both green before and after.
- `python3 -m py_compile` over every module, the tests and the skill bundle — clean.
- `nano_settlement_verify.verify` end to end: the `block_info` call, the `confirmed`
  gate, the amount comparison, the send-only `_operation` gate, and the payee check
  through `_same_account`. Amounts are `int()` of a raw string at every step; no float
  touches an amount, and `expect_raw` refuses a non-`int` (including `bool`) before the
  node is called.
- `nano_quorum`: `distinct_endpoints` refuses a list that cannot support `agree` before
  any call; a `Mismatch` from any endpoint that has the block *confirmed* fails closed
  immediately rather than being out-voted by a node that has not caught up;
  `balance_corroborated` reads only `confirmed_*` figures and treats their absence as an
  endpoint that did not answer, so a proxy dropping `include_confirmed` cannot have its
  unconfirmed `balance` read as agreement. The "error before figure" ordering in
  `balance_corroborated` is correct — a node answering `Account not found` sends
  `balance: "0"` alongside the error.
- Secrets: no key, seed or token in the tree. The package holds no send path at all —
  `process` is deliberately absent, so nothing here can move XNO.

## Found and fixed

**The skill bundle shipped a stale copy of the library.**
`skills/nano-settlement-verify/nano_settlement_verify.py` was a pre-#9 version, and no
test in the repository ever looked at it. An agent that installs the skill runs that
copy, never the module at the top of the tree.

`verify_cli.py:27` catches `(OSError, ValueError)` and answers exit 4 with
`{"verdict": "node_unreachable"}`. In the stale copy, a JSON body that is not a node
reply — a proxy status page, a rate-limit envelope — reached
`nano_settlement_verify.py:188`, `if reply["confirmed"] != "true":`, and raised a bare
`KeyError`, which escapes that arm. Measured, driving the bundle as shipped with only
`post_json` replaced:

```
Traceback (most recent call last):
  File ".../verify_cli.py", line 20, in main
  File ".../nano_settlement_verify.py", line 188, in verify
    if reply["confirmed"] != "true":
KeyError: 'confirmed'
exit 1, nothing on stdout
```

against the library beside it, on the same input:

```
{"verdict": "node_unreachable", "detail": "the endpoint's reply carries no 'confirmed' ..."}
exit 4
```

`SKILL.md` documents four exit codes and no others, and tells the agent that exit 4 is
the one to retry rather than read as a bad payment. The bundle gave it exit 1 and an
empty stdout on exactly that outcome — so a seller following the documented table got no
verdict at all, on a public endpoint's ordinary bad day. This is #9's defect, still live
in the artefact agents actually install.

The fix is the sync: the vendored file is now byte for byte the library. Three tests in
`tests/test_skill_bundle.py` cover it — the bytes match (which keeps holding for fixes
nobody has written yet), the vendored copy raises `NotANodeReply` when loaded by path,
and the bundle's CLI, run as a subprocess the way an agent runs it, exits 4 with the
documented verdict. All three fail against the stale copy; the suite is 162 green.

README's test count moved 159 → 162 in the same change.

## Could not verify

- No live node was reached — the network policy here does not allow one. Every node
  reply in the suite and in both e2e scripts is stubbed or served from a loopback stub,
  which is the repository's own standing law (`e2e_check.py` fails any test that opens a
  socket), not a gap introduced by this run.
- Whether the published skill on agentskills.io/OpenClaw is rebuilt from this directory
  or was uploaded once from an older tree. If it is a one-off upload, the copy agents
  install stays stale until it is re-published; that is outside a routine's reach.
