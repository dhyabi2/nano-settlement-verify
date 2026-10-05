"""The skill bundle ships its own copy of the library, so test the copy.

`skills/nano-settlement-verify/` is a self-contained OpenClaw bundle: SKILL.md,
a CLI wrapper and a vendored `nano_settlement_verify.py`. An agent installing
the skill runs THAT copy, never the one at the top of the repository, and no
test here looked at it - so it silently stayed on a pre-#9 version in which a
JSON body that is not a node reply raised a bare `KeyError`.

These tests drive the vendored files as the skill ships them.
"""

import importlib.util
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

    SKILL.md documents four exit codes and no others. Before this fix the
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
