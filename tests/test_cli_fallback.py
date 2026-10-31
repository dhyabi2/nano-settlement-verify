"""The CLI's fallback node list: RPC_URL may name several nodes, tried in order.

A stranger re-walking a proof with the CLI should not fail because one endpoint
went away. So RPC_URL (the argument or $NANO_RPC_URL) may be a comma-separated
list. The next node is asked ONLY when the one before could not be reached or
read - the exit 4 case, where nothing was checked. Any answer from a node is
final: a mismatch from the first node is never retried until some node says yes.
Agreement between nodes is nano_quorum's job, not this one's.

No test touches the network: the library's post_json is replaced by a fake that
answers per URL and records which URLs it was asked.
"""

import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "nano-settlement-verify"

_spec = importlib.util.spec_from_file_location("verify_cli_fallback_under_test",
                                               SKILL / "verify_cli.py")
verify_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_cli)

HASH = "B" * 64
RAW = 10**30
ACCOUNT = "nano_3abc"
FIRST = "http://127.0.0.1:7076/FIRSTSECRETKEY"
SECOND = "https://node-two.example/SECONDSECRETKEY"


def block_info(amount=RAW, confirmed="true"):
    return {
        "block_account": "nano_3payer",
        "amount": str(amount),
        "confirmed": confirmed,
        "height": "42",
        "subtype": "send",
        "contents": {"type": "state", "account": "nano_3payer", "link_as_account": ACCOUNT},
    }


def refused_connection():
    return urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


def http_503(url):
    # The reason phrase repeats the URL, as a node or proxy may: it must not print.
    return urllib.error.HTTPError(url, 503, "unavailable: " + url, {}, None)


@pytest.fixture
def nodes(monkeypatch):
    """Give each URL a reply (or an exception to raise); get back the URLs asked."""
    asked = []

    def install(answers):
        def post_json(rpc_url, payload):
            asked.append(rpc_url)
            answer = answers[rpc_url]
            if isinstance(answer, BaseException):
                raise answer
            return answer

        monkeypatch.setattr(verify_cli._library, "post_json", post_json)
        return asked

    monkeypatch.delenv("NANO_RPC_URL", raising=False)
    for name in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(name, raising=False)
    return install


def run(capsys, *rpc_argv):
    code = verify_cli.main(["verify_cli.py", HASH, str(RAW), ACCOUNT, *rpc_argv])
    out, err = capsys.readouterr()
    return code, out, err


# --- failover on an unreachable node --------------------------------------------


def test_an_unreachable_first_node_fails_over_to_the_second(nodes, capsys):
    asked = nodes({FIRST: refused_connection(), SECOND: block_info()})

    code, out, _ = run(capsys, FIRST + "," + SECOND)

    assert code == 0
    assert asked == [FIRST, SECOND]
    verdict = json.loads(out)
    assert verdict["settled"] is True
    assert verdict["rpc_index"] == 1


def test_a_node_whose_reply_cannot_be_read_also_fails_over(nodes, capsys):
    asked = nodes({FIRST: {"message": "rate limit exceeded"}, SECOND: block_info()})

    code, out, _ = run(capsys, FIRST + "," + SECOND)

    assert (code, asked, json.loads(out)["rpc_index"]) == (0, [FIRST, SECOND], 1)


def test_the_variable_may_hold_the_list_too(nodes, capsys, monkeypatch):
    asked = nodes({FIRST: http_503(FIRST), SECOND: block_info()})
    monkeypatch.setenv("NANO_RPC_URL", " %s , %s " % (FIRST, SECOND))

    code, out, _ = run(capsys)

    assert (code, asked, json.loads(out)["rpc_index"]) == (0, [FIRST, SECOND], 1)


def test_a_first_node_that_answers_is_the_only_one_asked(nodes, capsys):
    asked = nodes({FIRST: block_info(), SECOND: AssertionError("must not be asked")})

    code, out, _ = run(capsys, FIRST + "," + SECOND)

    assert (code, asked, json.loads(out)["rpc_index"]) == (0, [FIRST], 0)


# --- no failover after a real answer --------------------------------------------


@pytest.mark.parametrize("reply, code, verdict", [
    (block_info(amount=RAW - 1), 3, "mismatch"),
    ({"error": "Block not found"}, 3, "no_such_block"),
])
def test_a_real_answer_from_the_first_node_is_final(nodes, capsys, reply, code, verdict):
    asked = nodes({FIRST: reply, SECOND: block_info()})

    got, out, _ = run(capsys, FIRST + "," + SECOND)

    assert got == code
    assert asked == [FIRST], "a node's answer was retried on the next node"
    assert json.loads(out)["verdict"] == verdict
    assert json.loads(out)["rpc_index"] == 0


def test_not_confirmed_yet_from_the_first_node_is_final(nodes, capsys):
    asked = nodes({FIRST: block_info(confirmed="false"), SECOND: block_info()})

    code, out, _ = run(capsys, FIRST + "," + SECOND)

    assert (code, asked) == (2, [FIRST])
    assert json.loads(out)["settled"] is False


# --- every node unreachable ------------------------------------------------------


def test_every_node_unreachable_is_exit_4_and_quotes_no_url(nodes, capsys):
    asked = nodes({FIRST: refused_connection(), SECOND: http_503(SECOND)})

    code, out, err = run(capsys, FIRST + "," + SECOND)

    assert code == 4
    assert asked == [FIRST, SECOND]
    assert json.loads(out) == {
        "verdict": "node_unreachable",
        "detail": "node 1 of 2: <urlopen error [Errno 111] Connection refused>;"
                  " node 2 of 2: HTTP Error 503",
    }
    for piece in ("SECRETKEY", "127.0.0.1", "node-two", "7076", "http"):
        assert piece not in out + err


# --- every URL checked before any call --------------------------------------------


GOOD = "http://127.0.0.1:7076"


@pytest.mark.parametrize("rpc_url, detail", [
    ("http://alice:hunter2@host/," + GOOD, verify_cli.AT_SIGN_REFUSED),
    (GOOD + ",http://alice:hunter2@host/", verify_cli.AT_SIGN_REFUSED),
    (GOOD + ",file:///etc/passwd", verify_cli.SCHEME_REFUSED),
    (GOOD + ",https://host:key:443/", verify_cli.PORT_REFUSED),
    (GOOD + ",https://localhost%2FKEY/", verify_cli.HOST_REFUSED),
    (GOOD + ",," + SECOND, "the RPC URL list has an empty entry"),
    (GOOD + ",", "the RPC URL list has an empty entry"),
    (GOOD + "," + GOOD, "the RPC URL list names the same URL twice"),
    (GOOD + ", " + GOOD + " ", "the RPC URL list names the same URL twice"),
])
def test_a_bad_url_anywhere_in_the_list_is_refused_before_any_call(nodes, capsys,
                                                                  rpc_url, detail):
    asked = nodes({GOOD: block_info(), SECOND: block_info()})

    code, out, err = run(capsys, rpc_url)

    assert code == 4
    assert asked == [], "a node was asked before the whole list was checked"
    assert json.loads(out) == {"verdict": "node_unreachable", "detail": detail}
    for piece in ("alice", "hunter2", "KEY", "passwd", "127.0.0.1"):
        assert piece not in out + err


# --- one URL: output byte for byte as before ----------------------------------------


def test_a_single_url_prints_exactly_the_receipt_as_before(nodes, capsys):
    nodes({FIRST: block_info()})

    code, out, _ = run(capsys, FIRST)

    receipt = verify_cli._library.Receipt(settled=True, amount_raw=RAW, height=42,
                                         account=ACCOUNT)
    assert (code, out) == (0, receipt.to_json() + "\n")
    assert "rpc_index" not in out


def test_a_single_unreachable_url_prints_exactly_the_old_detail(nodes, capsys):
    nodes({FIRST: refused_connection()})

    code, out, _ = run(capsys, FIRST)

    assert code == 4
    assert out == json.dumps({"verdict": "node_unreachable",
                              "detail": "<urlopen error [Errno 111] Connection refused>"}) + "\n"


def test_a_single_mismatch_prints_exactly_as_before(nodes, capsys):
    nodes({FIRST: block_info(amount=RAW - 1)})

    code, out, _ = run(capsys, FIRST)

    assert code == 3
    assert out == json.dumps({"verdict": "mismatch", "expected": str(RAW),
                              "got": str(RAW - 1)}) + "\n"


# --- no downgrade: a list is https (or this machine), and TLS failure stops ----


@pytest.mark.parametrize("rpc", [
    "https://rpc.nano.to,http://plain.example",
    "http://plain.example,https://rpc.nano.to",
    "http://10.0.0.5:7076," + SECOND,
])
def test_a_list_refuses_plain_http_to_another_machine(nodes, capsys, rpc):
    asked = nodes({})

    code, out, _ = run(capsys, rpc)

    assert (code, asked) == (4, [])
    assert json.loads(out)["detail"] == verify_cli.PLAIN_HTTP_IN_LIST_REFUSED


def test_a_single_plain_http_url_is_still_asked_as_before(nodes, capsys):
    asked = nodes({"http://plain.example": block_info()})

    code, _, _ = run(capsys, "http://plain.example")

    assert (code, asked) == (0, ["http://plain.example"])


def test_a_tls_failure_is_not_a_reason_to_ask_the_next_node(nodes, capsys):
    import ssl
    tls = urllib.error.URLError(ssl.SSLError(1, "certificate verify failed"))
    other = "https://node-three.example"
    asked = nodes({SECOND: tls, other: AssertionError("must not be asked")})

    code, out, _ = run(capsys, SECOND + "," + other)

    assert (code, asked) == (4, [SECOND])
    assert json.loads(out)["verdict"] == "node_unreachable"


def test_a_comma_inside_one_url_is_not_a_list(nodes, capsys):
    url = "https://rpc.example/api?keys=a,b"
    asked = nodes({url: block_info()})

    code, out, _ = run(capsys, url)

    assert (code, asked) == (0, [url])
    assert "rpc_index" not in json.loads(out)


def test_the_same_node_written_two_ways_is_a_duplicate(nodes, capsys):
    asked = nodes({})

    code, out, _ = run(capsys, "https://rpc.example,HTTPS://RPC.EXAMPLE/")

    assert (code, asked) == (4, [])
    assert json.loads(out)["detail"] == verify_cli.DUPLICATE_REFUSED


def test_a_loopback_http_entry_is_refused_when_a_proxy_would_carry_it(nodes, capsys, monkeypatch):
    asked = nodes({})
    monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)

    code, out, _ = run(capsys, FIRST + "," + SECOND)

    assert (code, asked) == (4, [])
    assert json.loads(out)["detail"] == verify_cli.PLAIN_HTTP_IN_LIST_REFUSED


def test_the_default_port_written_out_is_a_duplicate(nodes, capsys):
    asked = nodes({})

    code, out, _ = run(capsys, "https://a.example,https://a.example:443")

    assert (code, asked) == (4, [])
    assert json.loads(out)["detail"] == verify_cli.DUPLICATE_REFUSED
