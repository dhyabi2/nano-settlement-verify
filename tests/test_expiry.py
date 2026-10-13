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
import os
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
    # NANO_RPC_URL is scrubbed: the CLI reads it, and a value exported in the
    # developer's shell (or a CI step) would otherwise choose - or refuse - the
    # node these tests stub.
    env = {name: value for name, value in os.environ.items() if name != "NANO_RPC_URL"}
    return subprocess.run(
        [sys.executable, "-c", _DRIVER, json.dumps(body), *args],
        cwd=SKILL, capture_output=True, text=True, env=env,
    )


def test_cli_old_three_argument_form_is_unchanged():
    done = run_cli(reply(), HASH, AMOUNT, ACCOUNT)
    assert done.returncode == 0, done.stderr
    # The one addition: with no RPC URL named, the verdict names the node asked.
    assert json.loads(done.stdout) == {
        "settled": True, "amount_raw": int(AMOUNT), "height": 42, "account": ACCOUNT,
        "rpc_url": "https://rpc.nano.to",
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


# --- a deadline that is not a number ------------------------------------------
#
# `not_after` is compared LAST - after the block is confirmed and the amount and
# payee have matched - so a deadline of the wrong type failed only on payments
# that had actually arrived, and it failed with a TypeError, which is neither
# OSError nor ValueError and so escaped the arm the README tells a seller to wrap
# `verify` in. It is checked beside `expect_raw` now, before the node is called.

RAW = int(AMOUNT)


@pytest.mark.parametrize("deadline", [str(SEEN), "2026-09-23T07:55:10Z", b"1790150110",
                                      [SEEN], {"not_after": 1}, True, False])
def test_a_deadline_that_is_not_a_number_is_refused_before_the_node_is_asked(node, deadline):
    calls = node(reply())

    with pytest.raises(ValueError) as caught:
        verify(HASH, RAW, ACCOUNT, URL, not_after=deadline)

    assert "not_after must be unix seconds" in str(caught.value)
    assert "parse_not_after" in str(caught.value), "the message names the function that reads a string"
    assert calls == [], "no node was asked for a deadline the caller cannot have meant"


def test_the_sellers_own_handler_catches_a_bad_deadline(node):
    """The README's seller wraps verify in (OSError, ValueError) and holds the
    call. A TypeError went straight through that and dropped a paid request."""
    node(reply())
    held = False
    try:
        verify(HASH, RAW, ACCOUNT, URL, not_after=str(SEEN))
    except (OSError, ValueError):
        held = True
    assert held, "the deadline check must land in the arm the README documents"


def test_a_string_deadline_read_through_parse_not_after_still_works(node):
    """The supported route is unchanged: parse it, then pass the int."""
    node(reply())

    receipt = verify(HASH, RAW, ACCOUNT, URL, not_after=parse_not_after(str(SEEN)))

    assert receipt.settled is True and receipt.seen_at == SEEN


def test_an_int_and_a_float_deadline_are_both_still_accepted(node):
    """`time.time() + 300` is a float and is how a deadline is ordinarily written."""
    for deadline in (SEEN, float(SEEN), SEEN + 0.5):
        node(reply())
        assert verify(HASH, RAW, ACCOUNT, URL, not_after=deadline).settled is True

    for deadline in (SEEN - 1, float(SEEN) - 0.5):
        node(reply())
        with pytest.raises(Late):
            verify(HASH, RAW, ACCOUNT, URL, not_after=deadline)


# --- a deadline that is a number, but not a finite int or float ---------------
#
# NaN compares False with everything, so `seen_at > NaN` was never true and a
# payment seen an hour after a NaN deadline verified as on time; +inf is later
# than every time and did the same. `json.loads` reads both from an order record
# ("NaN", "Infinity"). And a Decimal from a database NUMERIC column or a numpy
# number from a dataframe - deadlines that worked before #24 - was refused on
# every call, holding every paid order.

from decimal import Decimal  # noqa: E402
from fractions import Fraction  # noqa: E402


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), float("-inf"),
                                      json.loads("NaN"), json.loads("Infinity"),
                                      Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity")])
def test_a_deadline_that_is_not_finite_is_refused_before_the_node_is_asked(node, deadline):
    calls = node(reply())

    with pytest.raises(ValueError) as caught:
        verify(HASH, RAW, ACCOUNT, URL, not_after=deadline)

    assert "finite" in str(caught.value)
    assert calls == [], "no node was asked for a deadline that cannot decide a payment"


def test_a_late_payment_never_passes_a_nan_deadline(node):
    """The block was seen at SEEN; whatever the deadline, NaN must not settle it."""
    node(reply())
    settled = False
    try:
        settled = verify(HASH, RAW, ACCOUNT, URL, not_after=float("nan")).settled
    except (OSError, ValueError):
        pass
    assert settled is False


@pytest.mark.parametrize("kind", [Decimal, Fraction])
def test_a_decimal_or_fraction_deadline_decides_like_its_value(node, kind):
    node(reply())
    receipt = verify(HASH, RAW, ACCOUNT, URL, not_after=kind(SEEN + 60))
    assert receipt.settled is True
    assert json.loads(receipt.to_json())["not_after"] == SEEN + 60

    node(reply())
    assert verify(HASH, RAW, ACCOUNT, URL, not_after=kind(SEEN)).settled is True  # inclusive

    # a fraction of a second before the moment it was seen is late, never rounded up
    for deadline in (Decimal("1790150109.99999999999"), Fraction(SEEN * 10**12 - 1, 10**12)):
        node(reply())
        with pytest.raises(Late):
            verify(HASH, RAW, ACCOUNT, URL, not_after=deadline)


def test_a_numpy_deadline_is_accepted_and_its_nan_refused(node):
    numpy = pytest.importorskip("numpy")
    for deadline in (numpy.int64(SEEN + 60), numpy.float64(SEEN + 60)):
        node(reply())
        receipt = verify(HASH, RAW, ACCOUNT, URL, not_after=deadline)
        assert receipt.settled is True
        json.loads(receipt.to_json())  # a numpy number in the receipt would not serialise

    node(reply())
    with pytest.raises(Late):
        verify(HASH, RAW, ACCOUNT, URL, not_after=numpy.int64(SEEN - 1))

    node(reply())
    with pytest.raises(ValueError):
        verify(HASH, RAW, ACCOUNT, URL, not_after=numpy.float64("nan"))


# --- a deadline floored exactly, refused when it cannot be a unix time ---------
#
# math.floor() of a numpy.longdouble goes through float(), which rounds
# 1790150109.9999999 up to 1790150110, so a block seen ~100ns late settled. A
# float32 is 128 seconds apart near a 2026 unix time and a float16 cannot hold
# one at all. And flooring Decimal('1E+1000000') spent seconds of CPU or raised
# MemoryError, which is not in the family a seller's handler catches.


def test_a_longdouble_deadline_a_fraction_before_the_second_is_late(node):
    numpy = pytest.importorskip("numpy")
    if numpy.finfo(numpy.longdouble).nmant <= numpy.finfo(numpy.float64).nmant:
        pytest.skip("this platform's longdouble is a float64")
    deadline = numpy.longdouble(SEEN - 1) + numpy.longdouble("0.9999999")
    assert float(deadline) == SEEN, "the case only bites when float() rounds it up"

    calls = node(reply())
    with pytest.raises(Late):
        verify(HASH, RAW, ACCOUNT, URL, not_after=deadline)
    assert len(calls) == 1

    node(reply())
    receipt = verify(HASH, RAW, ACCOUNT, URL, not_after=numpy.longdouble(SEEN) + numpy.longdouble("0.5"))
    assert receipt.settled is True
    assert json.loads(receipt.to_json())["not_after"] == SEEN


def test_a_numpy_float_narrower_than_float64_is_refused_before_the_node_is_asked(node):
    numpy = pytest.importorskip("numpy")
    for deadline in (numpy.float32(SEEN), numpy.float16(1)):
        calls = node(reply())
        with pytest.raises(ValueError) as caught:
            verify(HASH, RAW, ACCOUNT, URL, not_after=deadline)
        assert type(deadline).__name__ in str(caught.value)
        assert calls == []


@pytest.mark.parametrize("deadline", [Decimal("1E+1000000"), Decimal("-1E+1000000"),
                                      2**63 + 1, -(2**63) - 1, 2**200, Fraction(2**70, 3), 1e300])
def test_a_deadline_beyond_2_63_seconds_is_refused_before_the_node_is_asked(node, deadline):
    calls = node(reply())
    with pytest.raises(ValueError) as caught:
        verify(HASH, RAW, ACCOUNT, URL, not_after=deadline)
    assert "2**63" in str(caught.value)
    assert calls == []


def test_a_deadline_at_2_63_seconds_is_still_a_deadline(node):
    node(reply())
    assert verify(HASH, RAW, ACCOUNT, URL, not_after=2**63).settled is True
    node(reply())
    with pytest.raises(Late):
        verify(HASH, RAW, ACCOUNT, URL, not_after=-(2**63))


@pytest.mark.parametrize("deadline", [10**5000, -(10**5000), Decimal("1E+1000000")],
                         ids=["10**5000", "-10**5000", "Decimal-1E+1000000"])
def test_a_huge_deadline_is_refused_with_a_message_that_renders(node, deadline):
    """repr() of 10**5000 raises its own ValueError past 4300 digits; the refusal must not."""
    calls = node(reply())
    with pytest.raises(ValueError) as caught:
        verify(HASH, RAW, ACCOUNT, URL, not_after=deadline)
    assert str(caught.value) == (
        f"not_after must be within 2**63 unix seconds of 1970, got a "
        f"{type(deadline).__name__} beyond that; pass None for no deadline"
    )
    assert calls == []
