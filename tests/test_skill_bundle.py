"""The skill bundle ships its own copy of the library, so test the copy.

`skills/nano-settlement-verify/` is a self-contained OpenClaw bundle: SKILL.md,
a CLI wrapper and a vendored `nano_settlement_verify.py`. An agent installing
the skill runs THAT copy, never the one at the top of the repository, and no
test here looked at it - so it silently stayed on a pre-#9 version in which a
JSON body that is not a node reply raised a bare `KeyError`.

These tests drive the vendored files as the skill ships them.
"""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "nano-settlement-verify"

HASH = "B" * 64
ONE_XNO_RAW = "1000000000000000000000000000000"
ACCOUNT = "nano_1natrium1o3z5519ifou7xii8crpxpk8y65qmkih8e8bpsjri651oza8imdd"


def test_the_vendored_library_is_the_library():
    """The bundle's copy is byte for byte the module the repository tests.

    Every fix to `nano_settlement_verify.py` has to reach the copy agents
    actually run. Comparing the bytes is the only check that keeps saying so
    for fixes nobody has written yet.
    """
    vendored = (SKILL / "nano_settlement_verify.py").read_bytes()
    library = (ROOT / "nano_settlement_verify.py").read_bytes()
    assert vendored == library, (
        "skills/nano-settlement-verify/nano_settlement_verify.py has drifted "
        "from nano_settlement_verify.py; the skill ships the stale one"
    )


def test_the_vendored_library_refuses_a_non_node_reply_as_a_value_error():
    """Loaded by path, the bundle's copy raises NotANodeReply, not KeyError.

    This is #9's guarantee, asserted against the file the skill ships rather
    than the one beside it.
    """
    name = "_vendored_nano_settlement_verify"
    spec = importlib.util.spec_from_file_location(
        name, SKILL / "nano_settlement_verify.py"
    )
    vendored = importlib.util.module_from_spec(spec)
    # Registered before exec because @dataclass resolves the defining module
    # through sys.modules while it builds Receipt.
    sys.modules[name] = vendored
    try:
        spec.loader.exec_module(vendored)
    finally:
        sys.modules.pop(name, None)

    vendored.post_json = lambda rpc_url, payload: {"message": "rate limit exceeded"}
    try:
        vendored.verify(HASH, int(ONE_XNO_RAW), ACCOUNT, "http://127.0.0.1:7076")
    except Exception as error:  # noqa: BLE001 - the type is the assertion
        assert isinstance(error, ValueError), (
            "a seller catches (OSError, ValueError); %s escapes that arm"
            % type(error).__name__
        )
        assert type(error).__name__ == "NotANodeReply"
    else:
        raise AssertionError("a reply carrying no 'confirmed' was not refused")


# Runs the bundle exactly as an agent does - `python3 verify_cli.py ...` from
# inside the skill directory - with only `post_json` replaced, so no socket is
# opened. `-c` rather than a temp file keeps the stub in sight of the test.
_DRIVER = """
import sys
sys.path.insert(0, ".")
import nano_settlement_verify
nano_settlement_verify.post_json = lambda rpc_url, payload: {"message": "rate limit exceeded"}
import verify_cli
sys.exit(verify_cli.main(["verify_cli.py", %r, %r, %r]))
""" % (HASH, ONE_XNO_RAW, ACCOUNT)


def _clean_environ():
    """os.environ without NANO_RPC_URL, so an exported value never steers a CLI test."""
    import os

    return {name: value for name, value in os.environ.items() if name != "NANO_RPC_URL"}


def test_the_skill_cli_answers_node_unreachable_the_way_skill_md_promises():
    """A proxy's JSON envelope must be exit 4 and a verdict, not a traceback.

    SKILL.md documents a fixed set of exit codes. Before this fix the
    bundle exited 1 with an empty stdout and a `KeyError` traceback, so an
    agent following that table got no verdict at all on the one outcome the
    table tells it to retry.
    """
    done = subprocess.run(
        [sys.executable, "-c", _DRIVER],
        cwd=SKILL,
        capture_output=True,
        text=True,
        env=_clean_environ(),
    )
    assert done.returncode == 4, (
        "expected exit 4 (node_unreachable), got %d; stderr:\n%s"
        % (done.returncode, done.stderr)
    )
    assert '"node_unreachable"' in done.stdout, done.stdout or done.stderr


# The same stub, answering with JSON that is not an object. verify_cli.py has
# no type check of its own: before the library refused these, `"error"` exited
# 3 (no_such_block) - a verdict about the money from a reply that says nothing
# about it - and a bare number crashed the CLI with a TypeError traceback.
_NON_OBJECT_DRIVER = """
import json
import sys
sys.path.insert(0, ".")
import nano_settlement_verify
_reply = json.loads(sys.argv[1])
nano_settlement_verify.post_json = lambda rpc_url, payload: _reply
import verify_cli
sys.exit(verify_cli.main(["verify_cli.py", %r, %r, %r]))
""" % (HASH, ONE_XNO_RAW, ACCOUNT)


def _run_with_reply(reply_json):
    return subprocess.run(
        [sys.executable, "-c", _NON_OBJECT_DRIVER, reply_json],
        cwd=SKILL,
        capture_output=True,
        text=True,
    )


def _assert_node_unreachable(done):
    assert done.returncode == 4, (
        "expected exit 4 (node_unreachable), got %d; stdout: %s stderr:\n%s"
        % (done.returncode, done.stdout, done.stderr)
    )
    assert json.loads(done.stdout)["verdict"] == "node_unreachable", done.stdout


def test_a_string_reply_through_the_cli_is_node_unreachable_not_no_such_block():
    """A proxy answering `"error"` used to exit 3: do not serve, from no answer."""
    _assert_node_unreachable(_run_with_reply('"error"'))


def test_a_bare_number_reply_through_the_cli_is_node_unreachable_not_a_traceback():
    """A proxy answering `42` used to crash the CLI with a TypeError."""
    _assert_node_unreachable(_run_with_reply("42"))


# The same defect as the test above, one line earlier in `main`. `EXPECT_RAW`
# is parsed by `int(argv[2])` BEFORE the try, so the one error the library is
# most careful about - an amount that is not a whole number of raw - is the one
# the front end cannot report. A seller following SKILL.md's table gets exit 1
# and an empty stdout, which `case $?` matches nowhere.
_AMOUNT_DRIVER = """
import sys
sys.path.insert(0, ".")
import nano_settlement_verify
nano_settlement_verify.post_json = lambda rpc_url, payload: (_ for _ in ()).throw(
    AssertionError("the node must not be asked about an unparseable amount")
)
import verify_cli
sys.exit(verify_cli.main(["verify_cli.py", %r, sys.argv[1], %r]))
""" % (HASH, ACCOUNT)


def _run_with_amount(amount):
    return subprocess.run(
        [sys.executable, "-c", _AMOUNT_DRIVER, amount],
        cwd=SKILL,
        capture_output=True,
        text=True,
        env=_clean_environ(),
    )


def test_a_decimal_amount_is_refused_with_a_verdict_not_a_traceback():
    """`0.001` is the mistake SKILL.md warns about, so it must get a verdict.

    The library refuses a float `expect_raw` with `TypeError` because raw has
    30 digits and a float keeps about 15 - the low digits being exactly where
    an underpayment hides. The CLI never reaches that refusal: `int("0.001")`
    raises `ValueError` outside the try and the process dies with exit 1 and
    nothing on stdout.
    """
    done = _run_with_amount("0.001")
    assert done.returncode == 3, (
        "expected exit 3 (do not serve), got %d; stderr:\n%s"
        % (done.returncode, done.stderr)
    )
    assert '"invalid_amount"' in done.stdout, done.stdout or done.stderr


def test_every_unparseable_amount_stays_inside_the_documented_table():
    """Every exit code the CLI emits is in SKILL.md's table; 1 is not one of them."""
    for amount in ("1e27", "abc", "", "0x10", "1.0", "  ", "1,000"):
        done = _run_with_amount(amount)
        assert done.returncode == 3, (
            "%r exited %d, outside SKILL.md's table; stderr:\n%s"
            % (amount, done.returncode, done.stderr)
        )
        assert '"invalid_amount"' in done.stdout, (
            "%r produced no verdict on stdout: %s" % (amount, done.stdout or done.stderr)
        )


def test_a_plain_integer_amount_is_untouched_by_the_guard():
    """The guard adds a refusal and takes nothing away: the node is still asked.

    `post_json` throws if it is called, so reaching it proves the amount
    parsed and the verdict path ran exactly as before.
    """
    done = _run_with_amount(ONE_XNO_RAW)
    assert done.returncode != 3, (
        "a valid raw amount was refused as invalid: %s" % (done.stdout or done.stderr)
    )
    assert '"invalid_amount"' not in done.stdout, done.stdout


# --------------------------------------------------------------------------
# The contract is written in three places and only one of them is executable.
#
# `verify_cli.py` RETURNS the exit codes; its module docstring LISTS them for
# whoever runs `--help`; SKILL.md's table is what an agent installing the skill
# reads. Nothing tied the three together, so `#21` could add `late` (5) and
# `unknown_time` (6) with every test green while a stale table said there were
# four codes - and a parked submission bundle carrying a fourth copy of the
# table shipped the old contract for a day. These two laws make the drift a
# test failure instead of something somebody has to notice.

_EXIT_TABLE_ROW = re.compile(r"^\|\s*(\d+)\s*\|")
#: `return 0` / `return 64` in the CLI's own control flow.
_CLI_RETURN = re.compile(r"^\s*return\s+(\d+)\s*$", re.MULTILINE)
#: `exit 0 settled, 2 not confirmed yet, ...` in the usage docstring.
_DOCSTRING_CODE = re.compile(r"\b(\d+)\s+(?:settled|not confirmed|mismatch|node unreachable|late|unknown_time|usage)")


def _documented_in_skill_md():
    codes = set()
    for line in (SKILL / "SKILL.md").read_text().splitlines():
        found = _EXIT_TABLE_ROW.match(line.strip())
        if found:
            codes.add(int(found.group(1)))
    return codes


def _emitted_by_the_cli():
    source = (SKILL / "verify_cli.py").read_text()
    body = source.split('"""', 2)[2]          # past the usage docstring
    codes = {int(n) for n in _CLI_RETURN.findall(body)}
    # The settled path is one statement with two outcomes, which the bare
    # `return <int>` pattern cannot see: `return 0 if receipt.settled else 2`.
    tail = re.search(r"return\s+(\d+)\s+if\s+receipt\.settled\s+else\s+(\d+)", body)
    assert tail, "the settled return changed shape; this reader needs updating"
    codes.update({int(tail.group(1)), int(tail.group(2))})
    return codes


def test_skill_md_documents_every_exit_code_the_cli_can_return():
    """A code the CLI emits and the table lacks is a seller with no rule for it.

    This is the one that bites: `case $?` in SKILL.md's own example has an arm
    per documented code, so an undocumented code falls through every arm and
    the gate neither serves nor refuses.
    """
    emitted = _emitted_by_the_cli()
    documented = _documented_in_skill_md()
    assert emitted - documented == set(), (
        "verify_cli.py returns %s, absent from SKILL.md's exit table (%s)"
        % (sorted(emitted - documented), sorted(documented))
    )


def test_the_table_promises_no_exit_code_the_cli_cannot_return():
    """The other direction: a row for a code that cannot happen is a false promise."""
    emitted = _emitted_by_the_cli()
    documented = _documented_in_skill_md()
    assert documented - emitted == set(), (
        "SKILL.md's table lists %s, which verify_cli.py never returns (%s)"
        % (sorted(documented - emitted), sorted(emitted))
    )


def test_the_usage_docstring_names_every_code_the_table_does():
    """`verify_cli.py` with no arguments prints the contract; it must be the same one."""
    docstring = (SKILL / "verify_cli.py").read_text().split('"""')[1]
    named = {int(n) for n in _DOCSTRING_CODE.findall(docstring)}
    missing = _documented_in_skill_md() - named
    assert missing == set(), (
        "the usage docstring names no outcome for exit %s, which SKILL.md's table has"
        % sorted(missing)
    )


# --------------------------------------------------------------------------
# The bundle's manifest declares the environment variables an installing agent
# may set. A variable the manifest declares and the bundle never reads is worse
# than an undeclared one: the operator believes they chose the node that decides
# whether money arrived, and every verification goes somewhere else.

_ENV_VAR_NAME = re.compile(r"^\s*-\s*name:\s*([A-Z_][A-Z0-9_]*)\s*$")


def _declared_env_vars():
    """The envVars SKILL.md's frontmatter declares, by name."""
    text = (SKILL / "SKILL.md").read_text()
    front = text.split("---", 2)[1]
    return {found.group(1) for found in map(_ENV_VAR_NAME.match, front.splitlines()) if found}


def test_the_bundle_reads_every_environment_variable_it_declares():
    """A declared variable nothing reads is a promise the bundle does not keep.

    SKILL.md's frontmatter is the contract an agent installing the skill
    configures against: a name listed under `envVars` is a name it will export.
    Nothing tied that list to the code, so `NANO_RPC_URL` was declared from the
    first version of the manifest and read nowhere in the tree - the one knob
    the manifest offers, over the one endpoint that decides whether a payment
    settled.
    """
    read = _env_vars_read_in_code()
    unread = sorted(name for name in _declared_env_vars() if name not in read)
    assert unread == [], (
        "SKILL.md declares %s under envVars, which no .py file in the bundle "
        "reads; an operator setting it would be ignored" % unread
    )


def _env_vars_read_in_code(sources=None):
    """Names the bundle's code reads from the environment, found in its syntax tree.

    A name in a docstring or a comment is not a read, so a plain substring
    search would pass on a bundle that only mentions the variable. This counts
    `os.environ.get("X")`, `os.getenv("X")` and `os.environ["X"]` with a
    literal name. That the read actually reaches `verify` is the behavioural
    tests' job below; this one only keeps the manifest and the code tied.
    """
    import ast

    def is_environ(node):
        return (isinstance(node, ast.Attribute) and node.attr == "environ"
                and isinstance(node.value, ast.Name) and node.value.id == "os")

    def literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    if sources is None:
        sources = [path.read_text() for path in sorted(SKILL.glob("*.py"))]
    found = set()
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and node.args and isinstance(node.func, ast.Attribute):
                func = node.func
                if (func.attr == "get" and is_environ(func.value)) or (
                    func.attr == "getenv" and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    name = literal(node.args[0])
                    if name:
                        found.add(name)
            elif isinstance(node, ast.Subscript) and is_environ(node.value):
                name = literal(node.slice)
                if name:
                    found.add(name)
    return found


def test_a_name_only_mentioned_in_prose_is_not_a_read():
    """The check above must not be satisfied by a docstring or a comment.

    The first version of it searched the source text for the name, and the
    CLI's usage docstring mentions `$NANO_RPC_URL`, so it would have passed
    with the read deleted.
    """
    prose = '"""Defaults to $NANO_RPC_URL."""\n# NANO_RPC_URL\nX = "NANO_RPC_URL"\n'
    assert _env_vars_read_in_code([prose]) == set()
    for read in (
        'import os\nu = os.environ.get("NANO_RPC_URL") or "d"\n',
        'import os\nu = os.getenv("NANO_RPC_URL")\n',
        'import os\nu = os.environ["NANO_RPC_URL"]\n',
    ):
        assert _env_vars_read_in_code([read]) == {"NANO_RPC_URL"}, read


# Runs the bundle as an agent does, with no RPC_URL argument, and records which
# endpoint the library was actually asked. `post_json` is replaced, so no socket
# is opened and the chosen URL never has to exist.
_RPC_DRIVER = """
import sys
sys.path.insert(0, ".")
import nano_settlement_verify
asked = []
def _record(rpc_url, payload):
    asked.append(rpc_url)
    return {"message": "rate limit exceeded"}
nano_settlement_verify.post_json = _record
import verify_cli
verify_cli.main(["verify_cli.py", %r, %r, %r] + sys.argv[1:])
print("ASKED " + (asked[0] if asked else "<nothing>"))
""" % (HASH, ONE_XNO_RAW, ACCOUNT)

CHOSEN = "http://127.0.0.1:7076/chosen-by-the-operator"


def _environment(env, unset=()):
    """This process's environment without its own NANO_RPC_URL, then env on top.

    A developer who exported NANO_RPC_URL for the e2e scripts must not have it
    choose the node for these tests.
    """
    merged = {**_clean_environ(), **env}
    for name in unset:
        merged.pop(name, None)
    return merged


def _asked_endpoint(env, *extra_argv, unset=()):
    done = subprocess.run(
        [sys.executable, "-c", _RPC_DRIVER, *extra_argv],
        cwd=SKILL,
        capture_output=True,
        text=True,
        env=_environment(env, unset),
    )
    for line in done.stdout.splitlines():
        if line.startswith("ASKED "):
            return line[len("ASKED "):]
    raise AssertionError(
        "the driver asked no endpoint; stdout:\n%s\nstderr:\n%s" % (done.stdout, done.stderr)
    )


def test_nano_rpc_url_chooses_the_node_that_decides_the_payment():
    """The variable SKILL.md declares must select the endpoint `verify` asks.

    Measured before this fix: with `NANO_RPC_URL` pointed at a stub, the
    documented three-argument command sent the stub nothing and asked
    `https://rpc.nano.to` instead. A seller that set the variable *because*
    `rpc.nano.to` wants a key and rate-limits a burst got rate-limited into
    exit 4 and held calls it had been paid for, while README.md's "ask a
    second node you chose, not one the seller named" was silently untrue.
    """
    assert _asked_endpoint({"NANO_RPC_URL": CHOSEN}) == CHOSEN


def test_an_explicit_rpc_url_argument_still_wins_over_the_variable():
    """The argument is the narrower instruction, so the variable is only a default."""
    argued = "http://127.0.0.1:7076/argued"
    assert _asked_endpoint({"NANO_RPC_URL": CHOSEN}, argued) == argued


def test_the_default_endpoint_is_unchanged_when_the_variable_is_unset():
    """With nothing set and nothing passed, the bundle asks what it always asked."""
    assert _asked_endpoint({}, unset=("NANO_RPC_URL",)) == "https://rpc.nano.to"


def test_an_empty_variable_counts_as_unset():
    """`export NANO_RPC_URL=` is how a shell clears it; it must not become the URL."""
    assert _asked_endpoint({"NANO_RPC_URL": ""}) == "https://rpc.nano.to"


def test_a_blank_variable_counts_as_unset():
    """A value of only whitespace is truthy, and used to be asked as the URL.

    Measured before this fix: `NANO_RPC_URL="   "` was handed to urllib as the
    endpoint, which refused it, and every verification exited 4 - a seller
    holding paid calls over a variable that names no node at all.
    """
    assert _asked_endpoint({"NANO_RPC_URL": " \t \n"}) == "https://rpc.nano.to"


def test_a_padded_variable_is_stripped():
    """Whitespace around the URL, as a quoted `export` or a .env file leaves it, is not the URL."""
    assert _asked_endpoint({"NANO_RPC_URL": "  %s \n" % CHOSEN}) == CHOSEN


SECRET = "s3cr3t-do-not-print"
USERINFO_URLS = [
    # urllib refuses this one before connecting: InvalidURL, "nonnumeric port".
    "http://operator:%s@127.0.0.1/" % SECRET,
    # This one parses, and fails to connect to a port nothing listens on.
    "http://operator:%s@127.0.0.1:9/" % SECRET,
]


def _run_cli(env, *extra_argv):
    return subprocess.run(
        [sys.executable, "verify_cli.py", HASH, ONE_XNO_RAW, ACCOUNT, *extra_argv],
        cwd=SKILL,
        capture_output=True,
        text=True,
        env=_environment(env),
        timeout=60,
    )


def _assert_unreachable_without_the_secret(done):
    assert SECRET not in done.stdout and SECRET not in done.stderr, (
        "the URL's password reached the output:\nstdout:\n%s\nstderr:\n%s"
        % (done.stdout, done.stderr)
    )
    assert done.returncode == 4, (
        "a URL the node cannot be reached at must be exit 4, got %d\nstdout:\n%s\nstderr:\n%s"
        % (done.returncode, done.stdout, done.stderr)
    )
    import json

    assert json.loads(done.stdout)["verdict"] == "node_unreachable"


def test_a_url_with_userinfo_in_the_variable_is_unreachable_and_not_echoed():
    """`http://user:pw@host/` used to escape as a traceback, exit 1, password on stderr.

    urllib raises `http.client.InvalidURL` for it - an `HTTPException`, neither
    an `OSError` nor a `ValueError` - so it passed the CLI's handler, exited 1
    with no row in SKILL.md's table, and printed the URL's password into
    whatever log keeps the seller's stderr.
    """
    for url in USERINFO_URLS:
        _assert_unreachable_without_the_secret(_run_cli({"NANO_RPC_URL": url}))


def test_a_url_with_userinfo_as_the_argument_is_unreachable_and_not_echoed():
    """The same for a per-call `RPC_URL` argument."""
    for url in USERINFO_URLS:
        _assert_unreachable_without_the_secret(_run_cli({}, url))


# --------------------------------------------------------------------------
# The RPC URL is checked before anything is sent. A URL with an `@` in it, or
# one that is not http or https, is refused as node_unreachable (exit 4) with a
# fixed detail that quotes no part of the URL. These used to be tried, and
# urllib's error text echoed the URL back: percent-decoded (`hunter%32` came
# out as `hunter2`), and cut at the first `/`, `?` or `#` of a password, which
# no scrubbing of that text could follow.

CREDENTIALS_DETAIL = (
    "the RPC URL contains '@': user credentials are never sent, so remove them;"
    " an '@' in a path or query must be written %40"
)
SCHEME_DETAIL = "the RPC URL must be http or https"
UNPARSEABLE_DETAIL = "the RPC URL could not be parsed"

# (url, the userinfo it carries, raw and decoded)
CREDENTIAL_URLS = {
    "percent-encoded digit": ("http://zqop:hunter%32@127.0.0.1/", ["zqop:hunter%32", "zqop:hunter2"]),
    "percent-encoded @": ("http://zqop:pq%40xv@127.0.0.1/", ["zqop:pq%40xv", "zqop:pq@xv"]),
    "percent-encoded newline": ("http://zqop:pq%0Axv@127.0.0.1/", ["zqop:pq%0Axv", "zqop:pq\nxv"]),
    "percent-encoded NUL": ("http://zqop:pq%00xv@127.0.0.1/", ["zqop:pq%00xv", "zqop:pq\x00xv"]),
    "unencoded slash": ("http://zqop:pq/xv@127.0.0.1/", ["zqop:pq/xv"]),
    "unencoded question mark": ("http://zqop:pq?xv@127.0.0.1/", ["zqop:pq?xv"]),
    "unencoded hash": ("http://zqop:pq#xv@127.0.0.1/", ["zqop:pq#xv"]),
    "user only": ("http://k3yqzw@127.0.0.1:1/", ["k3yqzw"]),
    "@ in the path": ("http://127.0.0.1:1/zqdir@xvq", ["zqdir@xvq"]),
    "@ in the query": ("http://127.0.0.1:1/?zkey=zq@xv", ["zkey=zq@xv"]),
}

SCHEME_URLS = {
    "file": "file:///zqdir/xvreply.json",
    "ftp": "ftp://127.0.0.1:1/zqreply",
    "no scheme": "127.0.0.1:1/zqreply",
    "bare word": "zqxvhost",
}


def _pieces(secret, shortest=4):
    """Every substring of secret at least `shortest` characters long."""
    return {secret[i:j] for i in range(len(secret)) for j in range(i + shortest, len(secret) + 1)}


def _assert_refused(done, detail, secrets):
    output = done.stdout + done.stderr
    leaked = sorted(piece for secret in secrets for piece in _pieces(secret) if piece in output)
    assert not leaked, "pieces of the URL reached the output: %r\n%s" % (leaked, output)
    assert done.returncode == 4, "got exit %d\n%s" % (done.returncode, output)
    assert done.stderr == ""
    import json

    assert json.loads(done.stdout) == {"verdict": "node_unreachable", "detail": detail}


def _both_ways(url):
    """The CLI run with url as the RPC_URL argument, and as NANO_RPC_URL."""
    return [_run_cli({}, url), _run_cli({"NANO_RPC_URL": url})]


@pytest.mark.parametrize("case", sorted(CREDENTIAL_URLS))
def test_a_url_with_an_at_sign_is_refused_before_any_request(case):
    url, secrets = CREDENTIAL_URLS[case]
    for done in _both_ways(url):
        _assert_refused(done, CREDENTIALS_DETAIL, secrets + [url])


@pytest.mark.parametrize("case", sorted(SCHEME_URLS))
def test_a_url_that_is_not_http_or_https_is_refused(case):
    url = SCHEME_URLS[case]
    for done in _both_ways(url):
        _assert_refused(done, SCHEME_DETAIL, [url])


def test_a_crafted_file_url_cannot_settle_a_payment(tmp_path):
    """`file://` used to be read as the node's reply, so a file someone wrote said settled."""
    import json

    reply = tmp_path / "reply.json"
    reply.write_text(json.dumps({
        "block_account": ACCOUNT,
        "amount": ONE_XNO_RAW,
        "confirmed": "true",
        "height": "1",
        "subtype": "send",
        "contents": {"type": "state", "account": ACCOUNT, "link_as_account": ACCOUNT},
    }))
    url = reply.as_uri()
    for done in _both_ways(url):
        assert '"settled": true' not in done.stdout
        _assert_refused(done, SCHEME_DETAIL, [str(reply)])


def test_an_uppercase_http_scheme_is_still_asked():
    """The scheme is compared without case, as URLs define it."""
    assert _asked_endpoint({"NANO_RPC_URL": "HTTP://127.0.0.1:7076/x"}) == "HTTP://127.0.0.1:7076/x"


def test_an_error_for_a_url_without_credentials_is_reported_unchanged():
    """Only refused URLs get a fixed detail; every other failure keeps main's text.

    Expected is computed the way main's CLI computes it - `str(error)[:200]` of
    what the library raises for the same URL - so no rewriting pass is allowed
    to touch it.
    """
    import json

    url = "http://127.0.0.1:1/"
    sys.path.insert(0, str(SKILL))
    try:
        import nano_settlement_verify as vendored

        try:
            vendored.verify(HASH, int(ONE_XNO_RAW), ACCOUNT, url)
        except OSError as error:
            expected = str(error)[:200]
        else:
            raise AssertionError("nothing listens on 127.0.0.1:1, yet verify returned")
    finally:
        sys.path.remove(str(SKILL))
    for done in _both_ways(url):
        assert done.returncode == 4, done.stdout + done.stderr
        assert json.loads(done.stdout) == {"verdict": "node_unreachable", "detail": expected}


# --------------------------------------------------------------------------
# An `@` is refused wherever it falls, so one that belongs in a path or query
# is written `%40`. That spelling must reach the node untouched.

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:7076/?key=zq%40xv",
    "http://127.0.0.1:7076/v1/acct%40zone/rpc",
])
def test_an_at_sign_written_as_percent_40_is_asked(url):
    assert _asked_endpoint({"NANO_RPC_URL": url}) == url
    assert _asked_endpoint({}, url) == url


def test_a_percent_40_in_a_query_reaches_a_loopback_node():
    """The request is really made, to the path and query as written."""
    with _loopback(_http_reply(_settled_reply())) as server:
        url = "http://127.0.0.1:%d/rpc?key=zq%%40xv" % server.port
        for done in _both_ways(url):
            assert done.returncode == 0, done.stdout + done.stderr
            assert json.loads(done.stdout)["settled"] is True
        assert server.paths == ["/rpc?key=zq%40xv"] * 2


# A URL urlsplit cannot parse has its own fixed detail. urllib's message for it
# quotes the netloc or the bracketed host, so it must never be passed on.
UNPARSEABLE_URLS = {
    "unclosed bracket": "http://[::1",
    "bracketed host that is no address": "http://[SECRETV6]:1/",
    "fullwidth solidus in the netloc": "http://h／SECRETX/",
}


@pytest.mark.parametrize("case", sorted(UNPARSEABLE_URLS))
def test_a_url_that_cannot_be_parsed_is_refused_with_its_own_detail(case):
    url = UNPARSEABLE_URLS[case]
    for done in _both_ways(url):
        assert "SECRET" not in done.stdout and "SECRET" not in done.stderr
        _assert_refused(done, UNPARSEABLE_DETAIL, [url])


# --------------------------------------------------------------------------
# Failures past the URL check, against a real socket on loopback. Each is
# "nothing was checked": exit 4 with a node_unreachable verdict, never a
# traceback with exit 1, which SKILL.md's table has no row for.

import contextlib
import io
import json
import os
import socket
import threading


def _settled_reply():
    return {
        "block_account": ACCOUNT,
        "amount": ONE_XNO_RAW,
        "confirmed": "true",
        "height": "1",
        "subtype": "send",
        "contents": {"type": "state", "account": ACCOUNT, "link_as_account": ACCOUNT},
    }


def _http_reply(body, declared_length=None):
    """A raw HTTP/1.0 response carrying body (JSON-encoded unless bytes)."""
    if not isinstance(body, bytes):
        body = json.dumps(body).encode("utf-8")
    length = len(body) if declared_length is None else declared_length
    head = "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % length
    return head.encode("ascii") + body


class _Server:
    def __init__(self, response):
        self.response = response
        self.paths = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.stopping = False
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        while not self.stopping:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                conn.settimeout(10)
                data = b""
                try:
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    head, _, rest = data.partition(b"\r\n\r\n")
                    lines = head.decode("latin-1").split("\r\n")
                    self.paths.append(lines[0].split(" ")[1])
                    length = 0
                    for line in lines[1:]:
                        name, _, value = line.partition(":")
                        if name.strip().lower() == "content-length":
                            length = int(value.strip())
                    while len(rest) < length:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        rest += chunk
                    conn.sendall(self.response)
                except OSError:
                    pass


@contextlib.contextmanager
def _loopback(response):
    server = _Server(response)
    server.thread.start()
    try:
        yield server
    finally:
        server.stopping = True
        server.thread.join(5)
        server.sock.close()


def _assert_unreachable(done, detail=None):
    output = done.stdout + done.stderr
    assert "Traceback" not in output, output
    assert done.returncode == 4, "got exit %d\n%s" % (done.returncode, output)
    verdict = json.loads(done.stdout)
    assert verdict["verdict"] == "node_unreachable", output
    if detail is not None:
        assert verdict == {"verdict": "node_unreachable", "detail": detail}


def test_a_non_numeric_port_is_unreachable_not_a_traceback():
    """urllib raises http.client.InvalidURL for it - an HTTPException."""
    for done in _both_ways("http://127.0.0.1:abc/"):
        _assert_unreachable(done)


def test_a_reply_shorter_than_its_content_length_is_unreachable():
    """The node closes early: http.client.IncompleteRead, an HTTPException."""
    with _loopback(_http_reply(b'{"block_acc', declared_length=4096)) as server:
        for done in _both_ways("http://127.0.0.1:%d/" % server.port):
            _assert_unreachable(done)
        assert len(server.paths) == 2


REPLY_NOT_AN_OBJECT = "the node's reply was not a JSON object"
REPLY_TOO_DEEP = "the node's reply was nested too deeply to read"

NOT_AN_OBJECT = {"list": [], "string": "x", "null": None, "number": 1}


@pytest.mark.parametrize("case", sorted(NOT_AN_OBJECT))
def test_a_reply_that_is_json_but_not_an_object_is_unreachable(case):
    """The library raised TypeError on it: a traceback and exit 1."""
    with _loopback(_http_reply(NOT_AN_OBJECT[case])) as server:
        for done in _both_ways("http://127.0.0.1:%d/" % server.port):
            _assert_unreachable(done, REPLY_NOT_AN_OBJECT)


@pytest.mark.parametrize("opening, closing", [("[", "]"), ('{"a":', "}")])
def test_a_reply_nested_too_deeply_is_unreachable(opening, closing):
    """json.loads raised RecursionError on it: a traceback and exit 1."""
    depth = 200000
    body = (opening * depth + "1" * (opening != "[") + closing * depth).encode("ascii")
    with _loopback(_http_reply(body)) as server:
        for done in _both_ways("http://127.0.0.1:%d/" % server.port):
            _assert_unreachable(done, REPLY_TOO_DEEP)


# --------------------------------------------------------------------------
# Review round four. urllib percent-decodes the netloc before it complains, so
# `user:hunter2%40host` came out as "nonnumeric port: 'hunter2@host'"; a
# credential spelled with %40 or %3A in the netloc is refused like an '@'.

PERCENT_CREDENTIAL_URLS = {
    "%40 before the host": ("http://zqop:hunter2%40127.0.0.1/", ["zqop:hunter2", "hunter2%40127"]),
    "%40 with a port": ("http://zqop:hunter2%40127.0.0.1:1/", ["zqop:hunter2", "hunter2%40127"]),
    "%3A and %40": ("http://zqop%3Ahunter2%40127.0.0.1/", ["zqop%3Ahunter2", "zqop:hunter2"]),
    "lowercase %3a only": ("http://zqop%3ahunter2.example/", ["zqop%3ahunter2", "zqop:hunter2"]),
}


@pytest.mark.parametrize("case", sorted(PERCENT_CREDENTIAL_URLS))
def test_a_percent_encoded_at_or_colon_in_the_netloc_is_refused(case):
    url, secrets = PERCENT_CREDENTIAL_URLS[case]
    for done in _both_ways(url):
        _assert_refused(done, CREDENTIALS_DETAIL, secrets)


def _verify_cli_module():
    sys.path.insert(0, str(SKILL))
    try:
        import verify_cli
    finally:
        sys.path.remove(str(SKILL))
    return verify_cli


def _class_only(class_name):
    return "the request to the RPC URL failed (%s)" % class_name


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:1/?key=SECRETZQ KEY",
    "http://127.0.0.1:1/?key=SECRETZQ\x7f",
    "http://127.0.0.1:1/?key=SECRETZQ\x01",
])
def test_a_query_with_a_control_character_is_not_echoed(url):
    """urllib's InvalidURL quotes the path and query back, control character and all."""
    for done in _both_ways(url):
        assert "SECRETZQ" not in done.stdout + done.stderr
        _assert_unreachable(done, _class_only("InvalidURL"))


# post_json replaced by one that raises what sys.argv[1] names, so the CLI's
# own handling of each failure is seen without a socket.
_RAISING_DRIVER = """
import http.client, socket, ssl, sys, urllib.error
sys.path.insert(0, ".")
import nano_settlement_verify
def _raise(rpc_url, payload):
    raise eval(sys.argv[1])
nano_settlement_verify.post_json = _raise
import verify_cli
sys.exit(verify_cli.main(["verify_cli.py", %r, %r, %r] + sys.argv[2:]))
""" % (HASH, ONE_XNO_RAW, ACCOUNT)


def _run_raising(expression, *extra_argv):
    return subprocess.run(
        [sys.executable, "-c", _RAISING_DRIVER, expression, *extra_argv],
        cwd=SKILL,
        capture_output=True,
        text=True,
        env=_clean_environ(),
        timeout=60,
    )


def test_the_librarys_own_reply_text_longer_than_200_characters_is_cut_at_200():
    """NotANodeReply keeps main's `str(error)[:200]`: the library builds it from the reply."""
    done = _run_raising("nano_settlement_verify.NotANodeReply('z' * 300)", "http://127.0.0.1:7076/rpc")
    _assert_unreachable(done, "z" * 200)


def test_an_os_error_is_shown_by_errno_and_strerror_only():
    """A filename, or any text OSError carries besides its errno and strerror, is dropped."""
    done = _run_raising("FileNotFoundError(2, 'No such file or directory', '/zqrpc/v2')",
                        "http://127.0.0.1:7076/zqrpc/v2")
    _assert_unreachable(done, "[Errno 2] No such file or directory")
    assert "zqrpc" not in done.stdout + done.stderr


def test_an_os_error_without_an_errno_is_named_by_its_class():
    done = _run_raising("OSError('cannot open /zqrpc/v2')", "http://127.0.0.1:7076/zqrpc/v2")
    _assert_unreachable(done, _class_only("OSError"))
    assert "zqrpc" not in done.stdout + done.stderr


def test_a_memory_error_is_unreachable_with_a_fixed_detail():
    _assert_unreachable(_run_raising("MemoryError()"), "the node's reply was too large to read")


@pytest.mark.parametrize("expression, name", [
    ("RuntimeError('zq reply text')", "RuntimeError"),
    ("KeyError('zq')", "KeyError"),
    ("AssertionError('zq')", "AssertionError"),
])
def test_any_other_failure_is_unreachable_never_a_traceback(expression, name):
    done = _run_raising(expression)
    _assert_unreachable(done, "the node's reply could not be checked (%s)" % name)
    assert "zq" not in done.stdout + done.stderr


REPLY_NUMBER_TOO_LARGE = "the node's reply carried a number too large to read"


@pytest.mark.parametrize("number", ["1e999", "Infinity", "-Infinity"])
def test_an_amount_too_large_for_an_integer_is_unreachable(number):
    """The library's int() of an infinite float raised OverflowError: a traceback, exit 1."""
    body = ('{"confirmed":"true","amount":%s,"height":"1"}' % number).encode("ascii")
    with _loopback(_http_reply(body)) as server:
        for done in _both_ways("http://127.0.0.1:%d/" % server.port):
            _assert_unreachable(done, REPLY_NUMBER_TOO_LARGE)


REPLY_WRONG_TYPE = "the node's reply was not a JSON object, or a field in it had the wrong type"


@pytest.mark.parametrize("amount", [[], {}])
def test_a_field_of_the_wrong_type_is_named_as_such(amount):
    reply = dict(_settled_reply(), amount=amount)
    with _loopback(_http_reply(reply)) as server:
        for done in _both_ways("http://127.0.0.1:%d/" % server.port):
            _assert_unreachable(done, REPLY_WRONG_TYPE)


@pytest.mark.parametrize("reply", ["error", "no error here", ["error"]])
def test_a_reply_that_mentions_error_but_is_not_an_object_is_unreachable(reply):
    """The library's `"error" in reply` made these no_such_block: exit 3, "do not serve"."""
    with _loopback(_http_reply(reply)) as server:
        for done in _both_ways("http://127.0.0.1:%d/" % server.port):
            _assert_unreachable(done, REPLY_NOT_AN_OBJECT)
        assert len(server.paths) == 2


def test_the_library_post_json_is_restored_after_main():
    """The CLI's object check wraps post_json only for its own call."""
    done = subprocess.run(
        [sys.executable, "-c", _RPC_DRIVER + "print('SAME', nano_settlement_verify.post_json is _record)"],
        cwd=SKILL,
        capture_output=True,
        text=True,
        env=_clean_environ(),
    )
    assert "SAME True" in done.stdout, done.stdout + done.stderr


def test_an_empty_rpc_url_argument_is_refused_not_replaced():
    """`""` as RPC_URL is an argument, so neither NANO_RPC_URL nor the default stands in."""
    assert _asked_endpoint({"NANO_RPC_URL": CHOSEN}, "") == "<nothing>"
    assert _asked_endpoint({}, "", unset=("NANO_RPC_URL",)) == "<nothing>"
    for done in [_run_cli({}, ""), _run_cli({"NANO_RPC_URL": CHOSEN}, "")]:
        _assert_refused(done, SCHEME_DETAIL, ["127.0.0.1:7076/chosen-by-the-operator"])


# --------------------------------------------------------------------------
# Review round five. http.client.InvalidURL's message is "URL can't contain
# control characters. %r" of the selector, and repr escapes a quote only when
# the string holds both kinds, so `/"?my'KEY x` came out as `/"?my\'KEY x` - a
# spelling no piece of the URL matched. InvalidURL's text is now never shown.

QUOTE_URLS = [
    "http://127.0.0.1:1/\"?my'SECRETKEY x",
    "http://127.0.0.1:1/\"?token'SECRETKEY\x01",
    "http://127.0.0.1:1/my'SECRETKEY x?\"",
    "http://127.0.0.1:1/SECRETKEY'x y?\"=",
    "http://127.0.0.1:1/p'SECRETKEY\x01?\"",
]


@pytest.mark.parametrize("url", QUOTE_URLS)
def test_an_invalid_url_with_both_quote_kinds_is_not_echoed(url):
    for done in _both_ways(url):
        assert "SECRET" not in done.stdout + done.stderr, done.stdout + done.stderr
        _assert_unreachable(done, _class_only("InvalidURL"))


def test_invalid_url_text_is_never_shown_even_when_it_names_no_piece():
    """The class alone decides: InvalidURL's message is the URL, in some spelling."""
    import http.client

    cli = _verify_cli_module()
    error = http.client.InvalidURL("anything at all")
    assert cli._error_detail(error) == _class_only("InvalidURL")


@pytest.mark.parametrize("url", QUOTE_URLS)
def test_an_other_error_quoting_the_selector_with_escaped_quotes_is_not_shown(url):
    """An OSError with no errno is named by its class, whatever spelling it quotes."""
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    selector = parts.path + ("?" + parts.query if parts.query else "")
    text = "could not send %r" % selector
    done = _run_raising("OSError(%r)" % text, url)
    assert "SECRET" not in done.stdout + done.stderr, done.stdout + done.stderr
    _assert_unreachable(done, _class_only("OSError"))
    cli = _verify_cli_module()
    for spelling in (text, text.replace("\\'", "'"), "could not send \"%s\"" % selector):
        assert cli._error_detail(OSError(spelling)) == _class_only("OSError"), spelling


# Each URL below leaked its secret under a mutation of the piece matching that
# an earlier round of this branch used, and stays as a regression case. Each is run for real, and with post_json raising an
# OSError that repeats the secret the way urllib would, as the argument and as
# NANO_RPC_URL; the secret must reach neither stdout nor stderr.
PINNED = {
    # the query dropped from the pieces
    "query with a control character": (
        "http://127.0.0.1:1/?SECRETKEY\x01", "SECRET",
        ["URL can't contain control characters. %r" % "/?SECRETKEY\x01",
         "bad request %r" % "?SECRETKEY\x01"]),
    # unquote against unquote_plus: urllib decodes the port with unquote
    "percent and plus in the port": (
        "http://127.0.0.1:SEC+RET%4B/", "RET",
        ["nonnumeric port: 'SEC+RETK'", "nonnumeric port: 'SEC RETK'"]),
    # repr against ascii: repr keeps the é, ascii writes \\xe9
    "non-ASCII path with a control character": (
        "http://127.0.0.1:1/SECRETé\x01", "SECRET",
        ["bad path %r" % "/SECRETé\x01", "bad path %s" % ascii("/SECRETé\x01")]),
    # `>=` against `>`: a piece of exactly four characters still counts
    "four-character port": (
        "http://127.0.0.1:SECR/", "SECR", ["nonnumeric port: 'SECR'"]),
    # the port after an IPv6 literal, whose colons are not the port's
    "port after an IPv6 host": (
        "http://[::1]:SECRETKEY/", "SECRET", ["nonnumeric port: 'SECRETKEY'"]),
}


@pytest.mark.parametrize("case", sorted(PINNED))
def test_a_pinned_secret_never_reaches_the_output(case):
    url, secret, texts = PINNED[case]
    runs = _both_ways(url)
    for text in texts:
        runs.append(_run_raising("OSError(%r)" % text, url))
        runs.append(subprocess.run(
            [sys.executable, "-c", _RAISING_DRIVER, "OSError(%r)" % text],
            cwd=SKILL, capture_output=True, text=True, timeout=60,
            env=_environment({"NANO_RPC_URL": url}),
        ))
    for done in runs:
        output = done.stdout + done.stderr
        assert secret not in output, output
        _assert_unreachable(done)


def test_an_uppercase_percent_3a_in_the_netloc_is_refused():
    """`%3A` is compared without case: the uppercase spelling is refused too."""
    for done in _both_ways("http://zqop%3AhunterSECRET.example/"):
        assert "SECRET" not in done.stdout + done.stderr
        _assert_refused(done, CREDENTIALS_DETAIL, ["zqop%3AhunterSECRET", "zqop:hunterSECRET"])


def test_page_1_with_connection_refused_shows_mains_exact_text():
    """`?page=1` keeps main's `str(error)[:200]`, computed the way main computes it."""
    url = "http://127.0.0.1:1/?page=1"
    sys.path.insert(0, str(SKILL))
    try:
        import nano_settlement_verify as vendored

        try:
            vendored.verify(HASH, int(ONE_XNO_RAW), ACCOUNT, url)
        except OSError as error:
            expected = str(error)[:200]
        else:
            raise AssertionError("nothing listens on 127.0.0.1:1, yet verify returned")
    finally:
        sys.path.remove(str(SKILL))
    assert "Connection refused" in expected or "Errno" in expected, expected
    for done in _both_ways(url):
        assert done.returncode == 4, done.stdout + done.stderr
        assert json.loads(done.stdout) == {"verdict": "node_unreachable", "detail": expected}


# --------------------------------------------------------------------------
# verify() reads post_json from the library's globals, so the CLI's object
# check is a swap of that global. Two callers of main in one process must not
# interleave it.

def test_two_overlapping_callers_leave_the_library_as_they_found_it():
    """Before the lock, the first to finish restored the library's own post_json
    and the second then put back the first caller's wrapper, left in place for
    every later caller in the process. Now the second waits for the first.
    """
    cli = _verify_cli_module()
    library = cli._library
    entered = {"first": threading.Event(), "second": threading.Event()}
    gate = {"first": threading.Event(), "second": threading.Event()}

    def stub(rpc_url, payload):
        name = rpc_url.rsplit("/", 1)[1]
        entered[name].set()
        assert gate[name].wait(30)
        if name == "first":
            return {"error": "Block not found"}
        return _settled_reply()

    original = library.post_json
    library.post_json = stub
    codes = {}

    def call(name):
        with contextlib.redirect_stdout(io.StringIO()):
            codes[name] = cli.main(["verify_cli.py", HASH, ONE_XNO_RAW, ACCOUNT,
                                    "http://127.0.0.1:7076/" + name])

    try:
        first = threading.Thread(target=call, args=("first",))
        second = threading.Thread(target=call, args=("second",))
        first.start()
        assert entered["first"].wait(30)
        second.start()
        second.join(0.5)
        gate["first"].set()
        first.join(30)
        gate["second"].set()
        second.join(30)
        assert not first.is_alive() and not second.is_alive()
        assert library.post_json is stub, "the library's post_json was left swapped"
    finally:
        library.post_json = original
    assert codes == {"first": 3, "second": 0}, codes


def test_a_post_json_replaced_during_the_call_is_not_overwritten():
    """Restore only our own wrapper: one set by someone else meanwhile stays."""
    cli = _verify_cli_module()
    library = cli._library
    original = library.post_json

    def theirs(rpc_url, payload):
        return {"error": "x"}

    def stub(rpc_url, payload):
        library.post_json = theirs
        return {"error": "Block not found"}

    library.post_json = stub
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli.main(["verify_cli.py", HASH, ONE_XNO_RAW, ACCOUNT,
                             "http://127.0.0.1:7076/"]) == 3
        assert library.post_json is theirs
    finally:
        library.post_json = original



def test_a_reply_that_is_not_an_object_is_a_value_error_to_library_users():
    """The README's `(OSError, ValueError)` handler still catches the CLI's refusal."""
    cli = _verify_cli_module()
    library = cli._library
    original = library.post_json
    library.post_json = lambda rpc_url, payload: ["error"]
    try:
        with pytest.raises(ValueError):
            cli._verify_objects_only(block_hash=HASH, expect_raw=int(ONE_XNO_RAW),
                                     account=ACCOUNT, rpc_url="http://127.0.0.1:7076/",
                                     not_after=None)
    finally:
        library.post_json = original
    assert issubclass(cli._NotAnObject, ValueError)


# --------------------------------------------------------------------------
# Review round seven. The detail of a failed request is decided by the error's
# class, never by matching pieces of the URL against its text: that matching
# leaked a key the node echoed in one sub-segment (`/rpc;key=KEY`,
# `/rpc/KEY.json`), and hid ordinary text that shared a word with the URL
# (`/api/node` hid the library's "not a node's block_info reply"). Each row:
# the expression raised, and the detail printed for it.

def _http_error(code, phrase):
    return "urllib.error.HTTPError('http://h/', %d, %r, {}, None)" % (code, phrase)


def _cert_error(message):
    return (
        "(lambda e: (setattr(e, 'reason', 'CERTIFICATE_VERIFY_FAILED'),"
        " setattr(e, 'verify_message', %r), e)[-1])"
        "(ssl.SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate"
        " verify failed: ' + %r + ' (_ssl.c:1006)'))" % (message, message)
    )


MAPPING = {
    # main's exact text
    "connection refused": (
        "urllib.error.URLError(ConnectionRefusedError(111, 'Connection refused'))",
        "<urlopen error [Errno 111] Connection refused>"),
    "DNS failure": (
        "urllib.error.URLError(socket.gaierror(-2, 'Name or service not known'))",
        "<urlopen error [Errno -2] Name or service not known>"),
    "connect timeout": (
        "urllib.error.URLError(TimeoutError('timed out'))", "<urlopen error timed out>"),
    "read timeout": ("TimeoutError('timed out')", "timed out"),
    "TLS read timeout": (
        "TimeoutError('The read operation timed out')", "The read operation timed out"),
    "reset": ("ConnectionResetError(104, 'Connection reset by peer')",
              "[Errno 104] Connection reset by peer"),
    "closed without a response": (
        "http.client.RemoteDisconnected('Remote end closed connection without response')",
        "Remote end closed connection without response"),
    "not JSON": ("__import__('json').JSONDecodeError('Expecting value', 'zqKEYzq', 0)",
                 "Expecting value: line 1 column 1 (char 0)"),
    "not UTF-8": ("UnicodeDecodeError('utf-8', b'\\xff', 0, 1, 'invalid start byte')",
                  "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"),
    "not a node reply": ("nano_settlement_verify.NotANodeReply('keys present: [\\'status\\']')",
                         "keys present: ['status']"),
    # the server's reason phrase is dropped
    "HTTP 500": (_http_error(500, "Internal Server Error"), "HTTP Error 500"),
    "HTTP 401 echoing a key": (_http_error(401, "bad key SECRETKEY"), "HTTP Error 401"),
    # SSL: reason code and certificate message only
    "certificate hostname mismatch": (
        "urllib.error.URLError(%s)" % _cert_error(
            "Hostname mismatch, certificate is not valid for 'proxy.example'."),
        "<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:"
        " Hostname mismatch, certificate is not valid for 'proxy.example'.>"),
    "other SSL error": (
        "(lambda e: (setattr(e, 'reason', 'WRONG_VERSION_NUMBER'), e)[-1])"
        "(ssl.SSLError(1, '[SSL: WRONG_VERSION_NUMBER] SECRETKEY'))",
        "[SSL: WRONG_VERSION_NUMBER]"),
    # the class only
    "InvalidURL": ("http.client.InvalidURL('nonnumeric port: SECRETKEY')", _class_only("InvalidURL")),
    "other HTTPException": ("http.client.BadStatusLine('HTTP/1.0 abc /SECRETKEY')",
                            _class_only("BadStatusLine")),
    "URLError with a text reason": ("urllib.error.URLError('unknown url type: SECRETKEY')",
                                    _class_only("URLError")),
    "URLError around an OSError with no errno": (
        "urllib.error.URLError(OSError('/SECRETKEY'))", _class_only("URLError")),
    "other ValueError": ("ValueError(\"invalid literal for int(): 'SECRETKEY'\")",
                         _class_only("ValueError")),
    "UnicodeEncodeError": ("UnicodeEncodeError('ascii', '/SECRETKEY\\xe9', 10, 11, 'ordinal')",
                           _class_only("UnicodeEncodeError")),
    "OSError with errno but no strerror": ("OSError(5)", _class_only("OSError")),
}


@pytest.mark.parametrize("case", sorted(MAPPING))
def test_each_failure_class_prints_its_own_detail(case):
    expression, detail = MAPPING[case]
    done = _run_raising(expression, "http://127.0.0.1:7076/rpc?key=SECRETKEY")
    _assert_unreachable(done, detail)
    assert "CRETKEY" not in done.stdout + done.stderr


def test_main_and_this_cli_print_the_same_text_for_ordinary_failures():
    """Computed the way main computes it: `str(error)[:200]` of the same error."""
    cli = _verify_cli_module()
    for case in ("connection refused", "DNS failure", "connect timeout", "read timeout",
                 "TLS read timeout", "reset", "closed without a response", "not JSON",
                 "not UTF-8", "not a node reply"):
        expression, _ = MAPPING[case]
        error = eval(expression, {
            "urllib": __import__("urllib.error"), "socket": socket, "http": __import__("http.client"),
            "nano_settlement_verify": cli._library,
        })
        assert cli._error_detail(error) == str(error)[:200], case


def test_the_detail_does_not_depend_on_the_url():
    """No piece of the URL is consulted, so `/api/node` hides nothing."""
    import inspect

    cli = _verify_cli_module()
    assert list(inspect.signature(cli._error_detail).parameters) == ["error"]
    assert not hasattr(cli, "_url_pieces")


def test_a_non_node_reply_at_a_path_named_node_shows_mains_text():
    """`/api/node` used to hide the library's NotANodeReply text, which says "node's"."""
    reply = {"message": "rate limited, slow down"}
    with _loopback(_http_reply(reply)) as server:
        url = "http://127.0.0.1:%d/api/node" % server.port
        sys.path.insert(0, str(SKILL))
        try:
            import nano_settlement_verify as vendored

            try:
                vendored.verify(HASH, int(ONE_XNO_RAW), ACCOUNT, url)
            except vendored.NotANodeReply as error:
                expected = str(error)[:200]
            else:
                raise AssertionError("a rate-limit envelope verified")
        finally:
            sys.path.remove(str(SKILL))
        assert "node's" in expected
        for done in _both_ways(url):
            _assert_unreachable(done, expected)


def test_a_certificate_mismatch_at_a_proxy_path_shows_the_reason():
    """`/proxy` used to hide an SSL hostname mismatch, because the message said "proxy"."""
    expression, detail = MAPPING["certificate hostname mismatch"]
    _assert_unreachable(_run_raising(expression, "https://proxy.example/proxy"), detail)


def test_a_padded_rpc_url_argument_is_stripped():
    """SKILL.md says whitespace around the URL is ignored: the argument too."""
    assert _asked_endpoint({}, "  http://127.0.0.1:7076/x \n") == "http://127.0.0.1:7076/x"


def test_a_blank_rpc_url_argument_is_refused_not_replaced():
    assert _asked_endpoint({"NANO_RPC_URL": CHOSEN}, "   ") == "<nothing>"
    for done in [_run_cli({}, " \t "), _run_cli({"NANO_RPC_URL": CHOSEN}, "  ")]:
        _assert_refused(done, SCHEME_DETAIL, ["127.0.0.1:7076/chosen-by-the-operator"])


# --------------------------------------------------------------------------
# Review round six. The URL check took the port after the FIRST colon and
# http.client takes it after the LAST, so `https://localhost:SECRETKEY:443/` was
# asked as host `localhost:SECRETKEY` - and through an HTTPS proxy the TLS
# hostname-mismatch error named it. A port that is not a number is refused up
# front, before any request, so none of these needs a network.

PORT_DETAIL = "the RPC URL's port must be a number from 0 to 65535"

BAD_PORT_URLS = {
    "two colons, https": "https://localhost:SECRETKEY:443/",
    "two colons, http": "http://127.0.0.1:SECRETKEY:1/",
    "secret host, two ports": "https://SECRETKEY.localhost:1:2/",
    "word as port": "http://127.0.0.1:SECRETKEY/",
    "IPv6 host, word as port": "http://[::1]:SECRETKEY/",
    "IPv6 host, two ports": "http://[::1]:1:SECRETKEY/",
    "fullwidth digits": "http://127.0.0.1:７０７６/SECRETKEY",
    "above 65535": "http://127.0.0.1:65536/SECRETKEY",
    "far above 65535": "http://127.0.0.1:99999999999999999999/SECRETKEY",
}


@pytest.mark.parametrize("case", sorted(BAD_PORT_URLS))
def test_a_port_that_is_not_a_number_is_refused_before_any_request(case):
    url = BAD_PORT_URLS[case]
    for done in _both_ways(url):
        _assert_refused(done, PORT_DETAIL, ["SECRETKEY"])


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:7076/", "http://127.0.0.1/", "http://127.0.0.1:/",
    "https://[::1]:7076/", "https://[::1]/", "https://node.example:443/rpc?k=a:b",
    "http://127.0.0.1:65535/", "http://127.0.0.1:0/", "http://127.0.0.1:080/",
])
def test_a_numeric_or_absent_port_passes_the_url_check(url):
    assert _verify_cli_module()._refuse_rpc_url(url) is None


# A node may echo the whole path, one segment of it, or one value in it in its
# reason phrase ("HTTP Error 401: bad key SECRETKEY" for `/rpc/SECRETKEY`), raw
# or decoded. The reason phrase is never printed, so none of them reaches stdout.

import http.server
import urllib.parse


def _echoed(path, how):
    if how == "whole":
        return path
    if how == "decoded":
        return urllib.parse.unquote(path)
    # the last value or segment, as a server might name "the key"
    value = urllib.parse.unquote(path).replace("?", "=").replace(";", "=").split("=")[-1]
    return value.split("/")[-1].split(".")[0]


@contextlib.contextmanager
def _echoing_node(how):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self):
            self.send_response(401, "bad key " + _echoed(self.path, how))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


ECHO_PATHS = [
    "/rpc/SECRETKEY", "/x;SECRETKEY", "/a%2FSECRETKEY", "/S%45CRETKEY",
    "/rpc;key=SECRETKEY", "/rpc/key=SECRETKEY", "/rpc/SECRETKEY.json",
    "/rpc?a=1;key=SECRETKEY", "/?key=SECRETKEY",
]


@pytest.mark.parametrize("how", ["whole", "decoded", "value"])
@pytest.mark.parametrize("path", ECHO_PATHS)
def test_a_reason_phrase_echoing_the_url_is_not_shown(path, how):
    with _echoing_node(how) as port:
        for done in _both_ways("http://127.0.0.1:%d%s" % (port, path)):
            output = done.stdout + done.stderr
            assert "CRETKEY" not in output, output
            _assert_unreachable(done, "HTTP Error 401")


def test_a_reason_phrase_is_dropped_even_when_it_holds_no_secret():
    """The rule is by class: the status code is shown, the server's phrase never."""
    with _echoing_node("value") as port:
        done = _run_cli({}, "http://127.0.0.1:%d/v1/rpc" % port)
        _assert_unreachable(done, "HTTP Error 401")


# A post_json that calls main again on the same thread blocked forever on the
# plain Lock its own thread held. The lock is re-entrant now, and each frame
# restores only the wrapper it installed. Run in a subprocess with a timeout,
# so a regression fails rather than hanging the suite.

_REENTRY_DRIVER = r'''
import contextlib, io, sys
sys.path.insert(0, ".")
import nano_settlement_verify as library
import verify_cli
ARGS = ["verify_cli.py", %(hash)r, %(raw)r, %(account)r, "http://127.0.0.1:7076/"]
seen = {}
def nested(rpc_url, payload):
    if "inner" not in seen:
        outer_wrapper = library.post_json
        with contextlib.redirect_stdout(io.StringIO()):
            seen["inner"] = verify_cli.main(ARGS)
        seen["restored_to_outer"] = library.post_json is outer_wrapper
    return {"error": "Block not found"}
library.post_json = nested
with contextlib.redirect_stdout(io.StringIO()):
    seen["outer"] = verify_cli.main(ARGS)
seen["restored"] = library.post_json is nested
print(sorted(seen.items()))
''' % {"hash": HASH, "raw": ONE_XNO_RAW, "account": ACCOUNT}


def test_a_post_json_that_calls_main_again_does_not_deadlock():
    try:
        done = subprocess.run(
            [sys.executable, "-c", _REENTRY_DRIVER],
            cwd=SKILL, capture_output=True, text=True, env=_clean_environ(), timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("main called again from inside post_json deadlocked")
    assert done.stdout.strip() == (
        "[('inner', 3), ('outer', 3), ('restored', True), ('restored_to_outer', True)]"
    ), done.stdout + done.stderr
