"""An order's expiry: a right amount that arrives late is not settled on time.

Asked by an outside agent (modeltruthcheck, Moltbook 2026-10-09): "Do you reject
a payment that arrives with the right amount but after the order's expiry, or
does your check only compare amount and account?" Before this, verify() only
compared amount and account.

What the check can and cannot know: a Nano block carries no sender-signed time.
The only time on offer is the node's `local_timestamp` - the moment THAT node
first saw the block, on its own clock. Another node can differ by seconds, and a
node may report 0 for a block it has no record of seeing (very old blocks). A
missing or 0 timestamp is therefore `UnknownTime`, never "on time".

The deadline is inclusive: a block seen at exactly `not_after` is on time.

No test touches the network: the node reply is stubbed, as in test_verify.py.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import nano_settlement_verify
from nano_settlement_verify import Late, Mismatch, UnknownTime, parse_not_after, verify

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "nano-settlement-verify"

HASH = "B2EC1E2A1F2C3D4E5F60718293A4B5C6D7E8F9012345678901234567890ABCDE"
ACCOUNT = "nano_3abc"
PAYER = "nano_3payer"
URL = "http://127.0.0.1:7076"
AMOUNT = "1000000000000000000000000"
SEEN = 1790150110  # 2026-09-23T07:55:10Z


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a socket")

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


def reply(local_timestamp=str(SEEN), confirmed="true", amount=AMOUNT, account=ACCOUNT):
    body = {
        "block_account": PAYER,
        "amount": amount,
        "confirmed": confirmed,
        "height": "42",
        "subtype": "send",
        "contents": {"type": "state", "account": PAYER, "link_as_account": account},
    }
    if local_timestamp is not None:
        body["local_timestamp"] = local_timestamp
    return body


@pytest.fixture
def node(monkeypatch):
    calls = []

    def stub(body):
        def post_json(rpc_url, payload):
            calls.append(payload)
            return body

        monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)
        return calls

    return stub


# --- the library -------------------------------------------------------------


def test_on_time_settles_and_records_when_it_was_seen(node):
    calls = node(reply())
    receipt = verify(HASH, int(AMOUNT), ACCOUNT, URL, not_after=SEEN + 60)
    assert receipt.settled is True
    assert receipt.seen_at == SEEN
    assert receipt.not_after == SEEN + 60
    assert len(calls) == 1  # the same single block_info read


def test_late_is_refused_with_both_times(node):
    node(reply())
    with pytest.raises(Late) as caught:
        verify(HASH, int(AMOUNT), ACCOUNT, URL, not_after=SEEN - 1)
    assert caught.value.seen_at == SEEN
    assert caught.value.not_after == SEEN - 1
    # the money did arrive: the receipt is carried so a refund can cite it
    assert caught.value.receipt.amount_raw == int(AMOUNT)


def test_exactly_at_the_deadline_is_on_time(node):
    """Inclusive: seen_at == not_after settles."""
    node(reply())
    assert verify(HASH, int(AMOUNT), ACCOUNT, URL, not_after=SEEN).settled is True


@pytest.mark.parametrize("stamp", [None, "0", "", "not-a-number"])
def test_a_missing_or_zero_timestamp_is_unknown_never_on_time(node, stamp):
    node(reply(local_timestamp=stamp))
    with pytest.raises(UnknownTime) as caught:
        verify(HASH, int(AMOUNT), ACCOUNT, URL, not_after=SEEN + 10**9)
    assert caught.value.not_after == SEEN + 10**9


def test_amount_is_still_checked_before_time(node):
    node(reply(amount="1"))
    with pytest.raises(Mismatch):
        verify(HASH, int(AMOUNT), ACCOUNT, URL, not_after=SEEN - 1)


def test_unconfirmed_stays_not_yet_with_a_deadline(node):
    node(reply(confirmed="false"))
    assert verify(HASH, int(AMOUNT), ACCOUNT, URL, not_after=SEEN + 60).settled is False


def test_no_deadline_leaves_the_receipt_exactly_as_before(node):
    node(reply(local_timestamp="0"))
    receipt = verify(HASH, int(AMOUNT), ACCOUNT, URL)
    assert json.loads(receipt.to_json()) == {
        "settled": True,
        "amount_raw": int(AMOUNT),
        "height": 42,
        "account": ACCOUNT,
    }


def test_a_deadline_adds_its_fields_and_the_time_source_to_the_json(node):
    node(reply())
    out = json.loads(verify(HASH, int(AMOUNT), ACCOUNT, URL, not_after=SEEN).to_json())
    assert out["seen_at"] == SEEN and out["not_after"] == SEEN
    assert "local_timestamp" in out["time_source"]
    assert "not signed" in out["time_source"]


@pytest.mark.parametrize(
    "text,expected",
    [
        (str(SEEN), SEEN),
        ("2026-09-23T07:55:10Z", SEEN),
        ("2026-09-23T07:55:10+00:00", SEEN),
        ("2026-09-23T09:55:10+02:00", SEEN),
    ],
)
def test_not_after_parses_unix_and_iso_utc(text, expected):
    assert parse_not_after(text) == expected


@pytest.mark.parametrize("text", ["", "soon", "2026-09-23T07:55:10", "1.5", "-5"])
def test_not_after_refuses_what_it_cannot_read_as_utc(text):
    with pytest.raises(ValueError):
        parse_not_after(text)


# --- the skill's CLI, run as an agent runs it -----------------------------------

_DRIVER = """
import json, sys
sys.path.insert(0, ".")
import nano_settlement_verify
nano_settlement_verify.post_json = lambda rpc_url, payload: json.loads(sys.argv[1])
import verify_cli
sys.exit(verify_cli.main(["verify_cli.py"] + sys.argv[2:]))
"""


def run_cli(body, *args):
    return subprocess.run(
        [sys.executable, "-c", _DRIVER, json.dumps(body), *args],
        cwd=SKILL, capture_output=True, text=True,
    )


def test_cli_old_three_argument_form_is_unchanged():
    done = run_cli(reply(), HASH, AMOUNT, ACCOUNT)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {
        "settled": True, "amount_raw": int(AMOUNT), "height": 42, "account": ACCOUNT,
    }


def test_cli_old_four_argument_form_is_unchanged():
    done = run_cli(reply(), HASH, AMOUNT, ACCOUNT, URL)
    assert done.returncode == 0, done.stderr
    assert "seen_at" not in json.loads(done.stdout)


def test_cli_on_time_exits_0():
    done = run_cli(reply(), HASH, AMOUNT, ACCOUNT, "--not-after", "2026-09-23T08:00:00Z")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["settled"] is True and out["seen_at"] == SEEN


def test_cli_late_exits_5():
    done = run_cli(reply(), HASH, AMOUNT, ACCOUNT, URL, "--not-after", str(SEEN - 1))
    assert done.returncode == 5, done.stderr
    out = json.loads(done.stdout)
    assert out["verdict"] == "late"
    assert out["seen_at"] == SEEN and out["not_after"] == SEEN - 1
    assert "local_timestamp" in out["time_source"]


def test_cli_at_the_deadline_exits_0():
    done = run_cli(reply(), HASH, AMOUNT, ACCOUNT, f"--not-after={SEEN}")
    assert done.returncode == 0, done.stderr


def test_cli_unknown_time_exits_6():
    done = run_cli(reply(local_timestamp="0"), HASH, AMOUNT, ACCOUNT, "--not-after", str(SEEN))
    assert done.returncode == 6, done.stderr
    out = json.loads(done.stdout)
    assert out["verdict"] == "unknown_time" and out["seen_at"] is None


def test_cli_an_unreadable_deadline_is_a_usage_error():
    done = run_cli(reply(), HASH, AMOUNT, ACCOUNT, "--not-after", "tomorrow")
    assert done.returncode == 64
    assert json.loads(done.stdout)["verdict"] == "invalid_not_after"
