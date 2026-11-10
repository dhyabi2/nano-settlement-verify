"""A wall clock never decides a verdict, and a record is read back only as written.

`verify_corroborated` reads its clock twice per node asked. A wall clock can
step backwards between those reads (NTP, a VM resumed, a test clock); the
verdict must then be exactly what a fixed clock gives - never an exception.
Beside that: the one UTC time shape a record carries, `Observation.from_dict`
refusing entries `to_dict()` never writes, and a `Corroboration` refusing a
delay that would make its record non-JSON or overflow.

No test touches the network: every endpoint is a stubbed reply.
"""

import dataclasses
import json
import math
import random
from datetime import datetime, timedelta, timezone

import pytest

import nano_quorum
import nano_settlement_verify
from nano_quorum import Observation, verify_corroborated

A = "http://127.0.0.1:7071"
B = "http://127.0.0.1:7072"
C = "http://127.0.0.1:7073"
D = "http://127.0.0.1:7074"

HASH = "B2EC1E2A1F2C3D4E5F60718293A4B5C6D7E8F9012345678901234567890ABCDE"
PAYEE = "nano_1payee"
ONE_XNO = 10**30
T0 = datetime(2026, 11, 9, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a socket")

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


def send_block(confirmed="true", amount=ONE_XNO):
    return {
        "amount": str(amount),
        "height": "7",
        "confirmed": confirmed,
        "subtype": "send",
        "contents": {"link_as_account": PAYEE},
    }


def nodes(monkeypatch, replies):
    def post_json(rpc_url, payload):
        reply = replies[rpc_url]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)


def clock_of(moments):
    queue = list(moments)
    return lambda: queue.pop(0)


def fixed_clock():
    return lambda: T0


def jitter_clock(rng):
    """Steps of -20 to +50 ms per read: often backwards, as NTP may step it."""
    state = {"now": T0}

    def now():
        state["now"] += timedelta(milliseconds=rng.randint(-20, 50))
        return state["now"]

    return now


def outcome(urls, clock, **kwargs):
    """What was decided - never when: the verdict a clock must not change."""
    try:
        out = verify_corroborated(HASH, ONE_XNO, PAYEE, urls, clock=clock, **kwargs)
    except (nano_quorum.NotCorroborated, nano_quorum.Disagreement, nano_settlement_verify.Mismatch) as error:
        observed = getattr(error, "node_observations", None) or getattr(error, "observations", ())
        states = [(o.rpc_url, o.state) for o in observed] if isinstance(observed, (list, tuple)) else None
        return (type(error).__name__, states)
    return ("receipt", out.receipt.settled, out.agreed, out.answered, out.failed,
            [(o.rpc_url, o.state) for o in out.observations])


def test_a_clock_that_steps_back_between_the_two_reads_still_settles(monkeypatch):
    """The review's repro: two nodes confirm, the clock reads .005 then .004.
    On 7e2b716 this raised ValueError ("attempted_at ... is after read_at")."""
    nodes(monkeypatch, {A: send_block(), B: send_block()})
    ms = timedelta(milliseconds=1)
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B], clock=clock_of([T0 + 5 * ms, T0 + 4 * ms] * 2))
    assert out.receipt.settled is True
    assert out.agreed == (A, B)
    first = out.observations[0]
    assert first.read_at == first.returned_at == "2026-11-09T00:00:00.004Z"
    assert first.attempted_at == first.read_at  # clamped, not after it
    record = json.loads(out.to_json())
    assert nano_quorum.replay_receipt(record)["verdict"] == "match"


def test_a_clock_that_steps_back_on_an_unreadable_node_records_it_and_goes_on(monkeypatch):
    nodes(monkeypatch, {A: OSError("timed out"), B: send_block(), C: send_block()})
    ms = timedelta(milliseconds=1)
    moments = [T0 + 9 * ms, T0 + 1 * ms, T0 + 10 * ms, T0 + 11 * ms, T0 + 12 * ms, T0 + 13 * ms]
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C], clock=clock_of(moments))
    assert out.receipt.settled is True
    failed = out.observations[0]
    assert failed.state == "unreadable"
    assert failed.attempted_at == failed.read_at == "2026-11-09T00:00:00.001Z"


REPLIES = [
    lambda: send_block(),
    lambda: send_block(),
    lambda: send_block(confirmed="false"),
    lambda: {"error": "Block not found"},
    lambda: OSError("timed out"),
    lambda: send_block(amount=ONE_XNO - 1),
]


def test_a_jittering_clock_never_changes_the_verdict_a_fixed_clock_gives(monkeypatch):
    """Over many scenarios, a clock that wanders back and forth by tens of ms
    gives exactly the verdict of a fixed clock: no exception, nothing settled
    or refused that the fixed clock did not settle or refuse."""
    rng = random.Random(43)
    backwards = 0
    for _ in range(600):
        urls = [A, B, C, D][: rng.randint(2, 4)]
        replies = {url: rng.choice(REPLIES)() for url in urls}
        agree = rng.randint(2, len(urls))
        nodes(monkeypatch, replies)
        want = outcome(urls, fixed_clock(), agree=agree)
        clock_rng = random.Random(rng.random())
        jitter = jitter_clock(clock_rng)
        reads = []

        def recording():
            moment = jitter()
            reads.append(moment)
            return moment

        got = outcome(urls, recording, agree=agree)
        assert got == want, (replies, agree)
        backwards += any(later < earlier for earlier, later in zip(reads, reads[1:]))
    assert backwards > 100  # the clock really did step back, often


@pytest.mark.parametrize("text", [
    "2026-11-09T12:00:00Z",
    "2026-11-09T12:00:00.123Z",
    "2026-11-09T12:00:00.123456Z",
    "2026-11-09T12:00:00+00:00",
    "2026-11-09T12:00:00.123+00:00",
])
def test_a_utc_time_ending_in_z_or_plus_zero_is_accepted(text):
    assert nano_quorum._is_utc_iso(text)
    assert Observation(A, "not-seen", text).read_at == text


@pytest.mark.parametrize("text", [
    "2026-11-09T12:00Z",            # no seconds
    "2026-W45-1T12:00:00Z",         # week date
    "2026-313T12:00:00Z",           # ordinal date
    "20261109T120000Z",             # basic form
    "2026-11-09T12:00:00.1234567Z",  # 7-digit fraction
    "2026-11-09T12:00:00+01:00",    # not UTC
    "2026-11-09T12:00:00-00:00",
    "2026-11-09T12:00:00",          # no zone
    "2026-11-09 12:00:00Z",
    "2026-13-09T12:00:00Z",
])
def test_any_other_time_shape_is_refused(text):
    assert not nano_quorum._is_utc_iso(text)
    with pytest.raises(ValueError, match="read_at"):
        Observation(A, "not-seen", text)


def entry(**changes):
    base = {
        "ask_index": 0, "rpc_url": A, "state": "confirmed", "read_at": "2026-11-09T12:00:01.000Z",
        "attempted_at": "2026-11-09T12:00:00.000Z", "returned_at": "2026-11-09T12:00:01.000Z", "detail": "",
    }
    base.update(changes)
    return base


def test_from_dict_reads_back_what_to_dict_wrote():
    assert Observation.from_dict(entry()).to_dict() == entry()


@pytest.mark.parametrize("bad", [
    entry(error=dict(kind="", message="")),
    entry(state="not-seen", error=dict(kind="", message="")),
    entry(attempted_at=""),
    entry(attempted_at=None, returned_at=""),
    entry(state="not-asked", read_at="", attempted_at=None, returned_at=None),
])
def test_from_dict_refuses_an_entry_to_dict_never_writes(bad):
    with pytest.raises(ValueError):
        Observation.from_dict(bad)


@pytest.fixture
def unsettled(monkeypatch):
    nodes(monkeypatch, {A: {"error": "Block not found"}, B: send_block(confirmed="false")})
    return verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B], clock=fixed_clock())


@pytest.mark.parametrize("delay", [math.nan, math.inf, -math.inf, -1, 10**10, 1e300])
def test_a_corroboration_refuses_a_delay_that_is_not_a_finite_range_number(unsettled, delay):
    with pytest.raises(ValueError, match="retry_after"):
        dataclasses.replace(unsettled, retry_after=delay)


@pytest.mark.parametrize("delay", [True, "5", None])
def test_a_corroboration_refuses_a_delay_that_is_not_a_number(unsettled, delay):
    with pytest.raises(TypeError, match="retry_after"):
        dataclasses.replace(unsettled, retry_after=delay)


def test_a_delay_past_year_9999_writes_the_latest_moment_instead_of_overflowing(unsettled):
    late = dataclasses.replace(unsettled.stop, at="9999-12-31T23:59:59.000Z")
    out = dataclasses.replace(unsettled, stop=late, retry_after=10**9)
    assert out.not_before == "9999-12-31T23:59:59.999Z"
    assert json.loads(out.to_json())["ask_again_not_before"] == "9999-12-31T23:59:59.999Z"


def test_a_sub_millisecond_delay_rounds_up_so_not_before_is_after_the_stop(unsettled):
    out = dataclasses.replace(unsettled, retry_after=0.0004)
    assert out.not_before == "2026-11-09T00:00:00.001Z" > out.stop.at == "2026-11-09T00:00:00.000Z"
    assert dataclasses.replace(unsettled, retry_after=0).not_before == unsettled.stop.at
    assert dataclasses.replace(unsettled, retry_after=1.5).not_before == "2026-11-09T00:00:01.500Z"


def test_json_of_a_corroboration_is_strict(unsettled):
    json.loads(unsettled.to_json(), parse_constant=lambda name: pytest.fail(f"non-JSON {name}"))
