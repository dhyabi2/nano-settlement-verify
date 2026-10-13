# nano-settlement-verify — audit 2026-10-13

Audited at `1cc4d0f` (`main` after #28). Ninth audit; all eight earlier notes
(2026-09-25 … 2026-10-11) and the full README read first.

Lens: can an agent prove an XNO payment settled with this today, and decide
whether a buyer can pay, without being hurt.

Baseline on `main`:

```
$ /usr/bin/python3 -m pytest -q        # 607 passed            (Python 3.13, numpy present)
$ python3 e2e_check.py                 # exit 0
$ python3 e2e_quorum_check.py          # exit 0  (includes a live rpc.nano.to read)
$ python3 e2e_payers_check.py          # exit 0  (reproduces the README's live figures)
```

## Found and fixed — `balance_corroborated` read three kinds of reply the rest of the package refuses

PR #27 gave `verify` a guard: a reply that is valid JSON but not an object is
`NotANodeReply`, not a verdict. `nano_quorum.balance_corroborated` reads a
reply **directly** rather than through `verify`, so it never got that guard —
and the 2026-10-04 note said so in as many words ("reads a balance reply on
its own path, which this change deliberately does not touch") without anyone
measuring what that path does. `_UNREADABLE`'s own comment
(`nano_quorum.py:169`) records the same fact. Measured:

```
$ python3 -I r1.py                       # two endpoints, both answering the same thing
the string "error"                     -> (0, 0)
"no error here"                        -> (0, 0)
["error"]  (a list)                    -> (0, 0)
"rate limit error: slow down"          -> (0, 0)
a bare number 7                        -> raised NotCorroborated
"maintenance"                          -> raised NotCorroborated

confirmed_balance as float 1e30        -> (1000000000000000019884624838656, 0)
1 XNO in raw                           -> 1000000000000000000000000000000
confirmed_balance as bool True         -> (1, 0)
```

Three defects, all on the figure a seller uses to decide whether a buyer can pay:

1. **`nano_quorum.py:345`, `if "error" in reply:` with no type check.** On a
   string that is a **substring** test; on a list, membership. So any proxy body
   carrying the word "error" took the branch meant for a node's
   `{"error": "Account not found"}` and **counted as an endpoint answering a
   confirmed balance of nothing**. Two such endpoints corroborated a balance of
   nothing for an account that may hold a fortune. `README.md:297` already
   promised the opposite: *"An endpoint whose reply is not a JSON object (a
   proxy's `"error"`, a bare number) is silence too: it counts as failed, not
   as answered."* — a README claim the code did not support.
2. **`nano_quorum.py:362`, `int(reply["confirmed_balance"])` with no guard
   against a JSON number.** `verify` refuses exactly this for a block's
   `amount` (`_raw_amount`, `nano_settlement_verify.py:448`);
   `balance_corroborated` did not. No float represents `10**30`, so `int(1e30)`
   is `1000000000000000019884624838656` — **19884624838656 raw ABOVE** one XNO,
   and a balance read too high is the dangerous direction: it says an account
   can cover a price it cannot. `int(True)` is 1. The repo's own test
   `test_a_balance_comes_back_as_integers_of_raw_not_as_strings`
   (`tests/test_quorum.py:336`) is named for this hazard and its comment names
   the exact 19884624838656-raw error, but it feeds the balance as a **string**,
   so the float path was never exercised.
3. **An infinite balance escaped the documented exception family.**
   `int(float("inf"))` raises `OverflowError`, which is **not** a `ValueError`
   and so is **not** in `_UNREADABLE = (OSError, ValueError, KeyError,
   TypeError)` (`nano_quorum.py:172`). On `main` it therefore escaped the
   `(OSError, ValueError)` arm the README tells a caller to write, out of a
   function whose whole contract is to raise `NotCorroborated` or
   `Disagreement`:

   ```
   main raises: OverflowError -> cannot convert float infinity to integer
   ```

**The fix adds refusals and nothing else.** An `isinstance(reply, dict)` guard
raising `NotANodeReply` — the same refusal, with the same exception type, that
`verify` already makes — and `_figure_of_raw`, which applies `_raw_amount`'s
rule to both figures before `int()`. `NotANodeReply` is a `ValueError`, so
`_UNREADABLE` already catches it and the endpoint becomes a `failure`, which is
what the README says should happen. A node's own `{"error": ...}` object still
reads as a balance of nothing; that control is pinned by a test. No amount,
destination, rounding or key path is touched.

### Failing, then passing

Twelve tests added. With `nano_quorum.py` alone reverted to `main` and the
tests kept, **8 fail**:

```
8 failed, 44 passed
  test_a_reply_that_is_not_a_json_object_counts_as_failed_not_as_a_zero_balance
      [the bare string 'error'] [a status page mentioning the word error]
      [a rate-limit envelope]   [a JSON array containing 'error']
  test_a_balance_that_is_a_json_number_is_refused_not_rounded
      [a JSON float that cannot represent 10**30] [the JSON true]
      [an infinite JSON number]
  test_a_receivable_that_is_a_json_number_is_refused_too
```

The bare-number and maintenance-page cases pass either way and are kept as
controls. With the fix: **619 passed**, and all three e2e scripts exit 0. The
README's test-count law (`tests/test_readme_test_count.py`) is satisfied —
`607` is now `619`, and the sentence it heads describes the new laws.

The skill bundle vendors only `nano_settlement_verify.py` and `verify_cli.py`,
not `nano_quorum.py`, so there is no vendored copy to keep in step
(`diff` of the vendored library against the root one is still empty).

## Found, NOT fixed — listed for the owner rather than built

Each is measured, and each is left alone for a stated reason.

- **`nano_independence.py:76`, `:80` — the same missing guard, with two
  outcomes.** `_funder` does `if "error" in info:` with no type check, then
  `info.get("frontier")`. A proxy body containing "error" returns
  `_UNOPENED` → **the payer is reported `unopened` and silently dropped from
  the independence count, with no error at all**; any other non-object reply
  raises `AttributeError`, which is neither `OSError` nor `ValueError` and so
  escapes the arm `README.md:78` tells a caller to write. Measured over a
  loopback node:

  ```
  proxy answers the JSON string "error"  -> unopened: [nano_1natrium…], independent: 0
  proxy answers the JSON string "maintenance" -> AttributeError: 'str' object has no attribute 'get'
  proxy answers a JSON array             -> AttributeError: 'list' object has no attribute 'get'
  ```

  It is the same one-line refusal, at three `post(...)` sites (lines 75, 88,
  97). Kept out of this pull request only to hold it to one concern; it is the
  obvious next one.
- **`nano_payers.py:286` — `str.isdigit()` admits strings `int()` rejects.**
  `"²".isdigit()` is `True`, `int("²")` raises `ValueError`. `_price_raw` gates
  on the first and calls the second, and the whole premise of
  `payees_from_manifest` is reading a document the **seller** controls while
  promising to refuse an unpayable Nano entry as `ManifestRefused` — which
  `_main` (line 800) is the only thing it catches. Through the README's own
  command (`README.md:358`) against a loopback x402 catalogue whose Nano entry
  has `"amount": "²"`:

  ```
  ValueError: invalid literal for int() with base 10: '²'
  EXIT=1
  ```

  The same gap sits at `nano_payers.py:455` (benign — the `ValueError` is
  inside `_UNREADABLE`) and at `nano_claim.py:269`, where such an amount is
  accepted and echoed as `"amount_raw": "²"`. The fix is one token —
  `value.isascii() and value.isdigit()`, or the `[0-9]+` fullmatch
  `nano_terms._RAW` (`nano_terms.py:59`) already uses.
- **SECURITY — `nano_claim.py:246`, `:367` and `nano_payers.py:814` print the
  RPC URL verbatim, userinfo included.** `verify_cli.py` spent seven review
  rounds ensuring no part of the RPC URL can reach stdout or stderr
  (`_refuse_rpc_url` refuses any `@` outright); the other two documented CLIs
  were left out. A node credential placed in the URL's userinfo is printed in
  the JSON these tools emit — and that JSON is designed to be **kept and shown
  as the record of a read**, so this is stdout, not a traceback. The same
  channel is `NotCorroborated`/`Disagreement` details and
  `PayerReport.to_json()`'s `"asked"` field (`nano_payers.py:229`).
  **No credential exists in this repository** — the reproduction used a dummy —
  so this is a disclosure path, not a leaked secret, and nothing needs rotating.
  Not fixed here because it changes what two published CLIs print, and because
  the right shape (refuse up front, as `verify_cli` does, versus strip the
  userinfo in one helper used by every site that prints or stores a URL) is a
  decision about the tools' contract.

## Also checked, found clean

- **`verify` end to end against 17 crafted replies.** Nothing settles that
  should not: a float `amount` is `NotANodeReply`; `confirmed` as the bool
  `true` or as `"TRUE"` is *not yet*, not settled; `subtype` `"Send"` or
  `" send"` is a `Mismatch`; a payee one character off is a `Mismatch`;
  `+1000…`, `1_000_…`, padded and Arabic-Indic-digit amounts all parse to the
  **correct** integer, so none over- or under-pays.
- **No float on an amount anywhere else, and no short `decimal` context.**
  `decimal` is imported only in `_deadline`, for a *time*; `getcontext().prec`
  is never touched; the only non-integer arithmetic in the package is
  `nano_payers.py:214`, a ratio. Finding 2 above was the one place a float
  could reach a money figure.
- **Nothing trusts the payer.** The presenter supplies only a block hash.
  `confirmed`, `amount`, `subtype`/`contents.type` and the payee all come from
  the node; `expect_raw`/`account` come from the seller. `nano_terms.settle`
  verifies against the sha256-pinned `amount_raw` and `payee`, never against
  anything the counterparty says afterwards. No field was found copied from
  presenter input and then treated as proof.
- **`nano_terms.py`** — `_RAW = re.compile(r"[1-9][0-9]*")` with `fullmatch` is
  ASCII-only, so it does not have the `isdigit` gap; `_HEX64`, the duplicate-key
  refusal via `object_pairs_hook`, the unknown/missing field checks and the
  blake2b payee checksum are sound.
- **`verify_cli.py`'s URL handling** (lines 298-391) — the port test is
  `set(port) <= frozenset("0123456789")`, ASCII-only before `int()`; `%40`,
  `%3A`, control characters, non-http(s) schemes, missing hosts and non-ASCII
  hosts are all refused before any request.
- **No committed secret** — tree and `git log --all -p` over all 80 commits, for
  api-key / bearer / `ghp_` / `sk-` / `AKIA` / PEM private-key patterns: only
  the deliberate test fixtures in `tests/test_skill_bundle.py`. No
  `subprocess`, `os.system`, `shell=`, `eval` or `exec`, and no `open()` on
  anything derived from input outside `tests/`.
- **Docs consistency and URLs** — the curl body in *Check it without running
  our code* is pinned to the real payload; SKILL.md's exit table matches
  `verify_cli`'s codes in all three directions; the vendored library is
  byte-identical; every real URL in README / SKILL.md / pyproject answers 200
  (github.com/dhyabi2/nano-settlement-verify,
  extract.paypercall.dev/.well-known/x402, rpc.nano.to). **No 404.**
- **README quickstart and the documented examples run.**
  `python3 nano_claim.py 243AF6BF…F57C` reproduces the README's printed JSON
  field for field; `verify_cli.py` on the genesis open block gives
  `{"verdict": "mismatch"}`, exit 3.
- **No database, so the `CREATE TABLE IF NOT EXISTS` migration class does not
  apply** — `grep` for `sqlite|CREATE TABLE|ALTER TABLE` matches nothing, and
  the README states the package deliberately keeps no record. There is no
  old-shaped-file experiment to run.

## Already known — not re-reported

- `int(_required(reply, "height"))` (`nano_settlement_verify.py:577`) raises
  `TypeError` for a `height` of the wrong type, escaping the README's
  `(OSError, ValueError)` arm for library callers. Named by the 2026-10-11 note,
  which calls `_required` refusing non-strings "a separate change". Still
  reproduces.
- The `NANO_RPC` vs `NANO_RPC_URL` spelling split between the two e2e scripts
  and SKILL.md — still open, as the 2026-10-11 note records.

## What I could not verify

- **No live payment.** Every finding is measured against loopback HTTP nodes
  over the real `urllib` path, or against an already-settled chain on the live
  ledger. No XNO moved.
- **Whether a real public endpoint answers a bare string or a JSON-number
  balance.** The reply shapes are constructed. The behaviour *class* is
  first-hand in this repo's own notes (`rpc.nano.to` silently dropping a
  request flag; proxies answering 200 with their own JSON), which is why the
  README already promises the refusal — but these exact shapes were not
  observed in the wild.
- **Whether any operator has already been given a wrong balance, lost a payer,
  or printed a credential.** Not knowable from here.
- **Python 3.11 without numpy** skips 3 tests in `tests/test_expiry.py`
  (`could not import 'numpy'`); the count law holds on an interpreter with
  numpy, which is what CI uses.
