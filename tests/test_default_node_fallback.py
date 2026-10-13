"""With no node named, a default node that refuses this host is not the end.

Reported 2026-10-13 by an agent that ran the documented three-argument command:
`rpc.nano.to` answered its host with 403, the block was fine, and the CLI had
nowhere else to look. With no RPC URL named (no argument, no NANO_RPC_URL), the
CLI asks the built-in fallback nodes in order while each refuses (403, 5xx) or
never answers, and names the node that answered. That list is empty today, so
these tests patch two nodes in. A node the operator named is still the only
node asked, the first node that answers decides, a 429 is not a refusal, and
when the default node gave the answer the output is exactly main's.

The CLI runs as an agent runs it, from inside the skill directory, with
`post_json` replaced by a stub keyed by URL, so no socket is opened.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "nano-settlement-verify"

HASH = "B749B757EE750FC9AEA72F33CB429EACCD2ABEC9F2CCF59BF17AFAC304C9A58F"
AMOUNT = "500000000000000000000000000"
ACCOUNT = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"
PAYER = "nano_3m8cz87zwxb1y16ob4bzp1eyek78qaig8ktohk7d45b18sh6u9exbowbnekr"
DEFAULT = "https://rpc.nano.to"
FIRST = "https://first-fallback.example/proxy"
SECOND = "https://second-fallback.example/api"
CHOSEN = "http://127.0.0.1:7076/chosen"


def settled(amount=AMOUNT):
    return {
        "block_account": PAYER, "amount": amount, "confirmed": "true", "height": "4",
        "subtype": "send",
        "contents": {"type": "state", "account": PAYER, "link_as_account": ACCOUNT},
    }


# Each node's behaviour is a JSON reply, or {"raise": "<expression>"}.
_DRIVER = """
import json, sys, urllib.error
sys.path.insert(0, ".")
import nano_settlement_verify
plan = json.loads(sys.argv[1])
asked = []
def _stub(rpc_url, payload):
    asked.append(rpc_url)
    step = plan[rpc_url]
    if isinstance(step, dict) and "raise" in step:
        raise eval(step["raise"])
    return step
nano_settlement_verify.post_json = _stub
import verify_cli
verify_cli.FALLBACK_RPC_URLS = (%r, %r)
code = verify_cli.main(["verify_cli.py"] + sys.argv[2:])
print("ASKED " + json.dumps(asked))
sys.exit(code)
""" % (FIRST, SECOND)

HTTP_403 = {"raise": "urllib.error.HTTPError(rpc_url, 403, 'Forbidden', {}, None)"}
HTTP_429 = {"raise": "urllib.error.HTTPError(rpc_url, 429, 'Too Many Requests', {}, None)"}
HTTP_503 = {"raise": "urllib.error.HTTPError(rpc_url, 503, 'Unavailable', {}, None)"}
REFUSED = {"raise": "urllib.error.URLError(ConnectionRefusedError(111, 'Connection refused'))"}


def run(plan, *extra_argv, env=None):
    environ = {name: value for name, value in __import__("os").environ.items()
               if name != "NANO_RPC_URL"}
    environ.update(env or {})
    done = subprocess.run(
        [sys.executable, "-c", _DRIVER, json.dumps(plan), HASH, AMOUNT, ACCOUNT, *extra_argv],
        cwd=SKILL, capture_output=True, text=True, env=environ, timeout=60,
    )
    lines = done.stdout.splitlines()
    assert lines and lines[-1].startswith("ASKED "), done.stdout + done.stderr
    verdict = json.loads(lines[0])
    return done.returncode, verdict, json.loads(lines[-1][len("ASKED "):])


def test_a_default_node_that_answers_403_falls_back_and_names_the_node_that_answered():
    code, verdict, asked = run({DEFAULT: HTTP_403, FIRST: settled(), SECOND: settled()})
    assert code == 0
    assert asked == [DEFAULT, FIRST]
    assert verdict == {
        "settled": True, "amount_raw": int(AMOUNT), "height": 4, "account": ACCOUNT,
        "rpc_url": FIRST,
        "failed_rpc_urls": [{"rpc_url": DEFAULT, "detail": "HTTP Error 403"}],
    }


def test_an_explicit_rpc_url_that_answers_403_is_never_replaced():
    for argv, env in (((CHOSEN,), None), ((), {"NANO_RPC_URL": CHOSEN})):
        code, verdict, asked = run({CHOSEN: HTTP_403, FIRST: settled(), SECOND: settled()},
                                   *argv, env=env)
        assert code == 4
        assert asked == [CHOSEN]
        # Exactly what it printed before: no fallback, and the URL not echoed.
        assert verdict == {"verdict": "node_unreachable", "detail": "HTTP Error 403"}


def test_a_default_node_that_answers_a_mismatch_decides_and_no_other_node_is_asked():
    code, verdict, asked = run({DEFAULT: settled(amount="1"), FIRST: settled(), SECOND: settled()})
    assert code == 3
    assert asked == [DEFAULT]
    # Only the default node was asked: exactly what main printed, no rpc_url.
    assert verdict == {"verdict": "mismatch", "expected": AMOUNT, "got": "1"}


def test_a_default_node_that_answers_with_a_non_node_reply_is_not_skipped():
    """A 200 with a rate-limit envelope is a reply, not a refusal: it is reported as such."""
    code, verdict, asked = run({DEFAULT: {"message": "rate limit exceeded"},
                                FIRST: settled(), SECOND: settled()})
    assert code == 4
    assert asked == [DEFAULT]
    assert verdict["verdict"] == "node_unreachable"
    assert "rpc_url" not in verdict and "hint" not in verdict


def test_when_every_built_in_node_fails_the_error_says_so_and_names_each_failure():
    code, verdict, asked = run({DEFAULT: HTTP_403, FIRST: HTTP_503, SECOND: REFUSED})
    assert code == 4
    assert asked == [DEFAULT, FIRST, SECOND]
    assert verdict == {
        "verdict": "node_unreachable",
        "detail": "every built-in node failed; set NANO_RPC_URL to a node you trust",
        "failed_rpc_urls": [
            {"rpc_url": DEFAULT, "detail": "HTTP Error 403"},
            {"rpc_url": FIRST, "detail": "HTTP Error 503"},
            {"rpc_url": SECOND, "detail": "<urlopen error [Errno 111] Connection refused>"},
        ],
    }


def test_a_settled_answer_from_the_default_node_is_main_output_byte_for_byte():
    code, verdict, asked = run({DEFAULT: settled(), FIRST: settled(), SECOND: settled()})
    assert code == 0
    assert asked == [DEFAULT]
    assert verdict == {"settled": True, "amount_raw": int(AMOUNT), "height": 4,
                       "account": ACCOUNT}


def test_a_429_from_the_default_node_does_not_walk_to_the_next_node():
    """A rate limit is not a refusal of the host: anyone can provoke one."""
    code, verdict, asked = run({DEFAULT: HTTP_429, FIRST: settled(), SECOND: settled()})
    assert code == 4
    assert asked == [DEFAULT]
    assert verdict["detail"] == "HTTP Error 429"
    assert set(verdict) == {"verdict", "detail", "hint"}
    assert "NANO_RPC_URL" in verdict["hint"]


def test_with_the_shipped_empty_list_a_refused_default_is_unreachable_with_a_hint():
    """The real FALLBACK_RPC_URLS: nothing else is asked, and the verdict says what to do."""
    driver = _DRIVER.replace("verify_cli.FALLBACK_RPC_URLS = ", "_unused = ")
    for step, detail in ((HTTP_403, "HTTP Error 403"), (HTTP_503, "HTTP Error 503"),
                         (REFUSED, "<urlopen error [Errno 111] Connection refused>")):
        environ = {name: value for name, value in __import__("os").environ.items()
                   if name != "NANO_RPC_URL"}
        done = subprocess.run(
            [sys.executable, "-c", driver, json.dumps({DEFAULT: step}), HASH, AMOUNT, ACCOUNT],
            cwd=SKILL, capture_output=True, text=True, env=environ, timeout=60,
        )
        lines = done.stdout.splitlines()
        assert done.returncode == 4, done.stdout + done.stderr
        assert json.loads(lines[-1][len("ASKED "):]) == [DEFAULT]
        verdict = json.loads(lines[0])
        assert set(verdict) == {"verdict", "detail", "hint"}
        assert verdict["verdict"] == "node_unreachable" and verdict["detail"] == detail
        assert "NANO_RPC_URL" in verdict["hint"]


def test_a_named_node_that_refuses_gets_no_hint():
    code, verdict, asked = run({CHOSEN: HTTP_503}, CHOSEN)
    assert code == 4 and asked == [CHOSEN]
    assert verdict == {"verdict": "node_unreachable", "detail": "HTTP Error 503"}
