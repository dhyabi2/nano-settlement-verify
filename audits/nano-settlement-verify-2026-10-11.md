# nano-settlement-verify — audit 2026-10-11

Previous audit 2026-10-10. Audited at `e39e37e` (`main` after #25).
Lens unchanged: can an agent get PAID in XNO with this today without being hurt —
refused when it was paid, told it was paid when it was not, or dropped mid-request.

`python3 -m pytest -q`: **350 passed** on `e39e37e`, **510** on this branch (354 after
the first commit; each review round below gives its own count).

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

Four new tests, two of which fail on `e39e37e`:

- `test_nano_rpc_url_chooses_the_node_that_decides_the_payment` — fails before
  (asks `https://rpc.nano.to`), passes after.
- `test_the_bundle_reads_every_environment_variable_it_declares` — the law the
  2026-10-10 audit prescribed: every name SKILL.md declares under `envVars` must
  appear in a `.py` file of the bundle. Fails before.
- `test_an_explicit_rpc_url_argument_still_wins_over_the_variable` and
  `test_the_default_endpoint_is_unchanged_when_the_variable_is_unset` are
  controls: both pass before and after, which is what makes this a narrowing.

The README test-count law (#19) required the page's count to move 350 → 354.

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

## The redaction was unsound, and was replaced

The redaction in gap 2 did not hold. urllib's error text echoes the URL back
percent-decoded (`http://op:hunter%32@127.0.0.1/` gave `nonnumeric port:
'hunter2@127.0.0.1'`; `%40`, `%0A` and `%00` likewise) and cut at the first `/`,
`?` or `#` of a password (`pa/ss` gave `'pa'`), and replacing short user or
password parts garbled unrelated words. The README's claim that the password
never reached stdout or stderr was false. A `file://` URL was also accepted, and
a crafted file was read as the node's reply and settled.

The CLI now checks the RPC URL - argument or `NANO_RPC_URL` - before anything is
sent: any `@` in it, a scheme other than http or https, or a URL urlsplit cannot
parse is `node_unreachable` (exit 4) with a fixed detail that quotes no part of
the URL. The redaction is gone; every other failure's detail is `str(error)[:200]`
as on `main`. Suite: **376 passed**.

## Third review round: the `@` rule, unparseable URLs and non-object replies

Review of the refusal above found five things; all fixed in `verify_cli.py` and
the tests, the vendored library untouched (still byte-identical to the root copy).

1. **The `@` refusal's detail was wrong for half of what it refuses.** Any `@`
   is still refused - an unencoded `/` in a password moves the userinfo split,
   so telling a credential from a path `@` is the parse not to trust - but
   `http://h/?key=a@b` and `/v1/acct@zone/rpc` carry no credentials, and
   both worked on `main`. The detail now says so and names the workaround:
   *"the RPC URL contains '@': user credentials are never sent, so remove them;
   an '@' in a path or query must be written %40"*. The usage docstring,
   SKILL.md and README state the rule; a test shows `%40` in a query reaches a
   loopback node exactly as written.
2. **An unparseable URL got the scheme message.** urlsplit's `ValueError` now
   has its own fixed detail, *"the RPC URL could not be parsed"*; urllib's own
   text for it quotes the bracketed host or netloc and is never passed on.
   Pinned with `http://[::1`, `http://[SECRETV6]:1/` and a fullwidth solidus in
   the netloc, none of whose pieces reach stdout or stderr.
3. **The `HTTPException` arm is pinned**: a non-numeric port (`InvalidURL`) and a
   loopback node that closes short of its Content-Length (`IncompleteRead`) are
   both exit 4, no traceback.
4. **Pre-existing: a reply that is JSON but not an object** (`[]`, `"x"`, `null`,
   `1`) raised `TypeError` in the library, and one nested past the parser's
   recursion limit `RecursionError` - a traceback and exit 1, outside SKILL.md's
   table. The CLI now answers both as `node_unreachable` (exit 4) with fixed
   details, *"the node's reply was not a JSON object"* and *"the node's reply was
   nested too deeply to read"*. Each is tested against a loopback node.
5. **The CLI tests inherited `NANO_RPC_URL`.** `tests/test_expiry.py`'s `run_cli`
   and the skill-bundle helpers passed the developer's environment through, so
   with `NANO_RPC_URL=ws://x` exported four expiry tests failed and one bundle
   test passed for the wrong reason (the refusal, not the stubbed proxy reply).
   They now drop it; the suite passes with it exported.

Suite: **391 passed** (376 before this round).

## Fourth review round: decoded credentials, infinite amounts and "error" strings

Review found these; all fixed in `verify_cli.py`, the tests and the docs. The
library is untouched, root and vendored copies still byte-identical.

1. **A percent-encoded credential leaked through urllib's decoded error text.**
   `http://user:hunter2%40127.0.0.1/` and `http://user%3Ahunter2%40127.0.0.1/`
   carry no literal `@`, so they passed the refusal, and urllib answered
   *"nonnumeric port: 'hunter2@127.0.0.1'"*. Two fixes: a netloc containing
   `%40` or `%3A` (any case) is refused with the `@` detail; and, generally,
   before `str(error)[:200]` is printed, the URL's userinfo, path, query,
   fragment and a non-numeric port - raw, percent-decoded and as `repr`
   escapes them, four characters or longer, plus every query value at any
   length - are looked for in the error text. If one is there, the detail is
   *"the request to the RPC URL failed (<ErrorClass>); its text would repeat
   part of the URL, so it is not shown"*. That also covers `?key=SECRET KEY`
   and other control characters in a query, which `InvalidURL` quotes back.
   An error that repeats no piece keeps main's text exactly, cut at 200
   characters; both are pinned.
2. **`{"confirmed":"true","amount":1e999,"height":"1"}`** (and `Infinity`,
   `-Infinity`) made the library's `int()` raise `OverflowError`: traceback,
   exit 1. `OverflowError` and `MemoryError` are now exit 4 with fixed details,
   and a last `except Exception` in the CLI answers anything else as exit 4,
   *"the node's reply could not be checked (<ErrorClass>)"*, so no reply can
   end in a traceback.
3. **The `TypeError` detail was wrong** for an object whose field has the wrong
   type (`"amount": []`); it is now *"the node's reply was not a JSON object, or
   a field in it had the wrong type"*.
4. **SKILL.md's exit 4 row said only "retry"**; it now says the three fixed URL
   refusals are configuration errors that retrying will not fix.
5. **Pre-existing: a string or list reply containing "error" was exit 3.** The
   library's `"error" in reply` is a substring or membership test on a
   non-object, so `"error"`, `"no error here"` and `["error"]` came out as
   `no_such_block` - "do not serve" from a reply that says nothing about the
   money. The README claimed every non-object reply was exit 4. The CLI now
   wraps the library's `post_json` for the duration of its own call and answers
   any non-object reply as `node_unreachable` before the library reads it; the
   original function is restored afterwards (tested).
6. **An empty `RPC_URL` argument (`""`) is pinned as refused** - not replaced by
   `NANO_RPC_URL` or the default.

Seen in passing, library behaviour this PR does not change: an `amount` of
`1.9` or `true`, or a `height` of `1.5`, is read through `int()` and can settle
(`1.9` as 1 raw). A node that answers JSON numbers there is not a real node, but
the library's `_required` could refuse non-strings; that is a separate change.

Suite: **416 passed** (391 before this round).

## Fifth review round: quote styles, short query values and the post_json swap

Review found these; all fixed in `verify_cli.py`, the tests and the docs. The
library is untouched, root and vendored copies still byte-identical.

1. **`InvalidURL` leaked through repr's quote style.** Its message is *"URL
   can't contain control characters. %r"* of the selector, and repr escapes a
   `'` only when the string also holds a `"`. So `http://127.0.0.1:1/"?my'SECRETKEY x`
   printed `/"?my\'SECRETKEY x`, a spelling no piece of the URL matched, and the
   key reached stdout (also `/"?token'SECRETKEY\x01`, `/my'SECRETKEY x?"`,
   `/SECRETKEY'x y?"=` and `/p'SECRETKEY\x01?"`). Now the text of any
   `http.client.InvalidURL` is never shown: it is always named by its class
   only. For every other error the piece matching compares the error text and
   each piece both as written and with `\'` and `\"` undone, so a quote-style
   mismatch cannot hide a piece. Tested through the CLI for those five URLs, as
   the argument and as `NANO_RPC_URL`.
2. **Mutations that leaked are pinned.** The query dropped from the pieces
   (`?SECRETKEY\x01`), `unquote` against `unquote_plus` (`:SEC+RET%4B`), repr
   against ascii (`/SECRETé\x01`), `>=` against `>` at four characters
   (`:SECR`), the port after an IPv6 host (`[::1]:SECRETKEY`) and an uppercase
   `%3A` in the netloc: each is a test that the secret reaches neither stdout
   nor stderr, run for real and with an `OSError` that repeats the secret the
   way urllib would. A table of further cases (fragment, a query field with no
   value, userinfo, raw percent-encoding, and a numeric port, an IPv6 host and a
   three-character value that must *not* hide an error) pins the rest; every
   remaining mutation of the matching is killed by the bundle's tests.
3. **Short query values hid ordinary errors.** Matching every query value at any
   length meant `?page=1` hid *"[Errno 111] Connection refused"*, and a reviewer
   measured about a third of ordinary errors hidden for one-to-three character
   values. Query values now use the same four-character minimum as every other
   piece; `?page=1` with connection refused shows main's exact text (tested).
4. **The `post_json` swap was not thread-safe.** `verify` takes no function to
   call instead of `post_json`, so the swap stays, but under a module-level
   lock held for the whole `verify` call, and the original is put back only if
   the global is still the CLI's wrapper. Tested with two overlapping callers
   (the library is left as it was found, and the second gets its own correct
   verdict) and with a `post_json` replaced during the call (it stays).
   `_NotAnObject` is now a `ValueError`, so the README's `(OSError, ValueError)`
   handler catches it.
5. **Docs.** README and SKILL.md say `InvalidURL` text is never shown and that a
   piece under four characters hides nothing.

Suite: **449 passed** (416 before this round).

## Sixth review round: the port, path segments and re-entry

Review found these on `5f98855`; all fixed in `verify_cli.py`, the tests and the
docs. The library is untouched, root and vendored copies still byte-identical.

1. **The port was split at a different colon than the request used.** The
   piece matching took the port after the *first* colon of the netloc and
   `http.client` takes it after the *last*, so `https://localhost:SECRETKEY:443/`
   was asked as host `localhost:SECRETKEY` port 443, and through an HTTPS proxy
   the TLS hostname-mismatch error named `localhost:SECRETKEY` - a piece the
   check never listed. Now any netloc whose port part (after the host, or after
   a bracketed IPv6 address) is not ASCII digits, or that has more than one
   colon outside brackets, is refused before anything is sent, with the fixed
   detail *"the RPC URL's port must be a number"* (exit 4, a configuration
   error). An empty port still means the default. Tested through the CLI, as
   the argument and as `NANO_RPC_URL`, for two-colon, word, IPv6 and fullwidth
   digit ports; no network is needed, since the refusal comes first.
2. **A path was a piece only whole.** A node whose reason phrase echoes one
   segment of the path (`/rpc/SECRETKEY` answered *"HTTP Error 401: bad key
   SECRETKEY"*) put the key in stdout. The path is now also split at `/` and
   `;`, both as written and percent-decoded, and each segment is a piece under
   the same four-character rule. Tested against a loopback node echoing the
   last segment raw and decoded, for `/rpc/SECRETKEY`, `/x;SECRETKEY`,
   `/a%2FSECRETKEY` and `/S%45CRETKEY`; a three-character segment (`/v1/rpc`)
   still shows the node's text.
3. **Same-thread re-entry deadlocked.** A `post_json` that calls `main` again
   on its own thread blocked forever on the plain `threading.Lock` its thread
   already held. The lock is now an `RLock`; each frame still restores the
   global only if it is still the wrapper that frame installed, so nested calls
   unwind to exactly what each found. Tested in a subprocess with a timeout, so
   a regression fails rather than hangs.
4. **In-process callers take turns.** The lock is held for the whole network
   call, so threads calling `main` in one process wait on a slow node one after
   another. Not changed - it is what keeps the swap correct - and now said in
   the README; running the CLI as its own process, the documented use, is
   unaffected.

Suite: **472 passed** (449 before this round).

## Seventh review round: the detail is chosen by class, not by matching the URL

Six rounds of review kept finding the same thing from both sides. Matching
pieces of the URL against an error's text leaked a key whenever the node split
the URL differently than the check did - a 401 reason phrase echoing
`SECRETKEY` from `/rpc;key=SECRETKEY`, `/rpc/key=SECRETKEY`,
`/rpc/SECRETKEY.json` or `?a=1;key=SECRETKEY` - and hid ordinary errors whenever
a word of the URL turned up in them: at `/api/node` the library's own
"not a node's block_info reply" text was hidden, at `/proxy` an SSL hostname
mismatch was. No list of pieces closes both sides, so the matching is gone:
`_url_pieces`, the quote-unescaping and their tests are deleted, and
`_error_detail(error)` no longer takes the URL at all. The detail is built
only from parts of the error that cannot hold the URL:

- `NotANodeReply`, `json.JSONDecodeError`, `UnicodeDecodeError`: the library's or
  the parser's own text about the reply, `str(error)[:200]` as on `main`.
- `urllib.error.HTTPError`: `HTTP Error <code>`; the server's reason phrase is
  dropped, which is a change from `main` (`HTTP Error 401: Unauthorized`).
- A `URLError` around an `OSError`, or a bare `OSError`: `[Errno N] strerror`,
  in `<urlopen error ...>` when wrapped; an `OSError` with no errno only when
  its text is one the standard library writes word for word (`timed out`,
  `Remote end closed connection without response`). Byte-identical with `main`
  for connection refused, DNS failure, connect and read timeouts, reset and
  unreachable network, measured by running both CLIs.
- `ssl.SSLError`: its reason code, and for a certificate its verify message,
  which may name the host (the host can carry no credential: userinfo and
  non-numeric ports are refused first). `main`'s `(_ssl.c:NNNN)` suffix is not
  repeated.
- Anything else - `InvalidURL`, other `http.client` errors, a `URLError` with a
  text reason, any other `ValueError` - `the request to the RPC URL failed
  (<class>)`.

Also: a padded `RPC_URL` argument is stripped as SKILL.md already said (a blank
one is still refused, not defaulted); a port above 65535 is refused up front
with the port detail, now *"the RPC URL's port must be a number from 0 to
65535"*; `ValueError` stays in the handler so `NotANodeReply` keeps its text.

Out of scope and documented in the README: a node given a key in its URL can
repeat it in its reply, and the CLI prints the reply's `got` on a mismatch and
the reply's keys in a `NotANodeReply` detail, as `main` does.

Re-run of the reviewers' probes on the new code: the 11,025-URL random fuzz
(three seeds), the echoing-server fuzz (72 runs), the TLS-through-proxy probe,
the sub-segment 401 probe and the re-entry probes - no secret printed by the transport
error path (HTTP status, OS, TLS, `InvalidURL`); it appears only where the probe's node
puts it in its reply (a mismatch's `got`, the keys of a `NotANodeReply`), as on
`main`; a class-built rerun of the 31 ordinary messages hides none of
them, and differs from `main` only by the dropped reason phrase (11) and SSL
suffix (2).

Suite: **510 passed** (472 before this round).

## Eighth review round: the decoded host, proxy tunnels and the missing host

- **The host, percent-decoded (LOW).** urllib unquotes the host before it
  connects, and through an HTTPS proxy a TLS hostname mismatch names the decoded
  host: `https://localhost%2FSECRETKEY/` printed *"not valid for
  'localhost/SECRETKEY'"*, and `%3F`, `%23`, `%20`, `%25`, `%3B` the same way; on
  3.12 a `%C2%AD` (soft hyphen) vanished in IDNA mapping. `_ssl_text`'s docstring
  claimed the host could carry nothing. Now the host is refused before anything
  is sent unless it is plain ASCII letters, digits, `-`, `.` and `_`, or an IP
  address (*"the RPC URL's host must be ASCII letters, digits, '-', '.' and '_',
  or an IP address"*). The only `%` allowed in the host is a bracketed IPv6 zone
  id written `%25name` (`[fe80::1%25eth0]`), whose name may hold no further `%`.
  Unicode (IDNA) hosts are refused rather than mapped, a deliberate change from
  `main` (which tried them): the mapping is where a decoded delimiter or a
  vanishing character hides. Write the name in its `xn--` form. `_` is allowed
  because container names use it and it decodes to nothing. The host as written
  is still named in a certificate error (`SECRETKEY.localhost` is printed as
  such): a host is not a credential.
- **Proxy tunnel failures (LOW).** http.client raises
  `OSError("Tunnel connection failed: 407 Proxy Authentication Required")` with
  no errno, which the last round printed as the class only. Now
  `<urlopen error Tunnel connection failed: 407>`: the three-digit status,
  matched whole by a strict pattern, and none of the proxy's own text. Tested
  against a loopback `CONNECT` proxy answering 407 and 403.
- **No host (LOW).** `http:///x`, `http://:7076/` and `http:x` are refused up
  front with *"the RPC URL has no host"*, in place of the class-only text that
  replaced `main`'s `<urlopen error no host given>`.
- **TypeError from the transport (INFO).** A NUL in a tunnelled host made
  http.client raise `TypeError` on 3.12, which was reported as a reply problem.
  A control character (C0, DEL or C1) anywhere in the URL is now refused before
  the request (*"the RPC URL contains a control character"*), and a `TypeError`
  from `post_json` itself - which only sends and parses JSON text - becomes
  *"the request to the RPC URL failed (TypeError)"* (an `OSError` to library
  callers). A `TypeError` from reading the reply keeps its own detail.
- **3.12's timeouts (INFO).** *"_ssl.c:993: The handshake operation timed out"*
  did not match the fixed-text list. The ssl timeouts are now matched whole by
  a pattern that allows that prefix and nothing else, and the text is printed
  as `main` printed it. `error.args[0] in _FIXED_OS_TEXTS` raised `TypeError`
  for an unhashable argument (`OSError(['x'])`), out of the except arm, as a
  traceback; the argument must now be a string first.
- **Pinned.** An uppercase `HTTP://` or `Http://` reaches a loopback node, as
  the argument and as the variable. A long `<urlopen error ...>` or
  `[Errno N] strerror` is cut at 200.

Two existing tests changed expectation: a URL with `\x01` or `\x7f` in its
path or query was `InvalidURL` by class, and is now the control-character
refusal.

Probes rerun on 3.11, 3.12 and 3.13: the decoded-host probe (13 URLs) prints no
secret except the two whose host is literally `SECRETKEY.localhost` or
`xn--secretkey-`, which are named as written; the TLS-through-proxy probe is
unchanged; the users' network probe now shows `Tunnel connection failed: 407` and
`403` without the reason phrase, and refuses the unicode host; the random URL
fuzz (11,025 URLs, three seeds) flags nothing; the echoing-server fuzz (72
runs) prints no secret. 39 of the new or changed tests fail on the code before
this round.

Suite: **576 passed** (529 before this round).

## A separate finding of this run, on its own branch (#27)

Checking one claim of the round above — that the "not a JSON object" refusal is a
`ValueError` so a library caller's handler catches it — showed that the refusal
exists only in the skill bundle's CLI (`verify_cli.py::_objects_only`, added
deliberately "without changing the library"). The library, which `README.md`
documents first and which `nano_quorum` and `nano_claim` call internally, still
read a non-object reply through `"error" in reply` — a substring test on a
string, a membership test on a list. Measured on `main`:

| reply | raises | in README's `except (OSError, ValueError)` |
| --- | --- | --- |
| `"error"`, `"no error here"`, `["error"]` | `NotFound` — *do not serve* | no |
| `[1,2,3]`, `7`, `1.5`, `None` | `TypeError` | no |

The first row is a verdict about the money from a reply that says nothing about
it: a seller whose buyer really paid refuses the call. The second escapes the arm
`README.md:78` tells a seller to write.

Fixed in **#27**, off `main`, as its own concern: `verify` raises
`NotANodeReply` for a reply that is not a dict, before the `in` test. 14 tests
fail on `main` and pass there; 2 controls hold either way. 350 → 366.

The lesson for a later run: a guard at one layer can hide a defect at the layer
the README actually documents. The CLI's own tests passed either way, which is
why nothing here caught it.

## Still open — the owner's one sentence, not a blocker

The repository spells the variable two ways: SKILL.md declares `NANO_RPC_URL`,
while `e2e_payers_check.py:171` and `e2e_quorum_check.py:178` read `NANO_RPC`.
This fix honours the declared name, because that is the one an installing agent
configures against and the one that was broken; the two e2e scripts are
developer tools and are untouched. If `NANO_RPC` is meant to be canonical, the
cheap follow-up is the CLI accepting it as a second fallback — one line, and the
new law already covers whichever name the manifest ends up declaring.

## Checked and deliberately not changed: redirects to another scheme

`nano-receipt-ledger`'s own run today found its node opener following a redirect
to `ftp`, `file` or `data` over a connection its deadline never watched, and
hardened the opener to http and https only. The same question here, since
`post_json` uses `urllib.request.urlopen` with the default handlers and this
PR's scheme check covers only the URL the caller gives, not where a redirect
leads. Measured against an HTTP stub that answers `302` with a `Location` of
each scheme:

- `file:///…/crafted.json` — **refused by urllib itself**:
  `HTTPError: Redirection to url 'file://…' is not allowed`. An `HTTPError` is
  an `OSError`, so it lands in the arm `README.md:78` tells a seller to write.
  Tried on `main` and on this branch; a crafted local file cannot be reached
  this way. (Contrast the *configured* `file://` URL, which `main` does follow
  and settle from — fixed in round 3 above.)
- `ftp://127.0.0.1:1/x` — **followed**: the error is
  `URLError: [Errno 111] Connection refused`, i.e. urllib attempted the FTP
  connection rather than refusing the scheme. `urllib`'s redirect handler allows
  `http`, `https` and `ftp`.

Not fixed, and not a defect I can show hurting anyone: reaching it requires the
node the operator chose to answer `302` to an attacker's FTP server, and a node
that will do that can simply answer with a crafted reply directly — the reply is
already trusted by design (`README.md`: "Fetch this block from any public Nano
node", the operator picks it). So the ceiling is the same either way and no
privilege is gained. An opener restricted to http and https would be tidier, but
it is hardening with no demonstrated harm, and this repository's rule is that a
finding has to be shown before it is built against.

Recorded so a later run does not repeat the experiment. If the owner would
rather have the opener restricted anyway, it is a handful of lines and one test.

## Could not verify

- **No live payment of our own.** The measurement above uses a loopback stub
  whose reply shape is copied from a real `rpc.nano.to` answer; no XNO moved in
  this run.
- **Whether a seller has already refused a paid buyer on this.** Not knowable
  from here. The exposure is every installation of the skill bundle that set the
  variable, for as long as the manifest has declared it.
