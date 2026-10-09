"""The skill bundle ships its own copy of the library, so test the copy.

`skills/nano-settlement-verify/` is a self-contained OpenClaw bundle: SKILL.md,
a CLI wrapper and a vendored `nano_settlement_verify.py`. An agent installing
the skill runs THAT copy, never the one at the top of the repository, and no
test here looked at it - so it silently stayed on a pre-#9 version in which a
JSON body that is not a node reply raised a bare `KeyError`.

These tests drive the vendored files as the skill ships them.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

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
    )
    assert done.returncode == 4, (
        "expected exit 4 (node_unreachable), got %d; stderr:\n%s"
        % (done.returncode, done.stderr)
    )
    assert '"node_unreachable"' in done.stdout, done.stdout or done.stderr


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
