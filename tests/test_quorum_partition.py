"""Laws for nano_quorum under a partition: 3 of 10 nodes (30%) cannot be reached.

The question this answers was put from outside: with a share of the node set
unreachable, do "not-seen" and "unreadable" stay two different facts? A node
that answered "Block not found" has said something about the block; a node
whose connection was refused or timed out has said nothing. If the second were
ever counted as the first, a partition would read as evidence that a payment
does not exist.

No test touches the network: `post_json` is replaced by a stub, the three
unreachable nodes raise the errors urllib raises (connection refused, a
timeout, a URLError), and any real socket fails the test.

Only what `verify_corroborated` documents is asserted. It has no verdict
"not seen by a quorum": fewer than `agree` answers raises `NotCorroborated`
("we could not look"), and enough answers without `agree` confirmations is an
unsettled `Corroboration`, which is "not yet", never a refusal.
"""

import json
import urllib.error
from collections import Counter

import pytest

import nano_settlement_verify
from nano_quorum import NotCorroborated, replay_receipt, verify_corroborated

HASH = "B2EC1E2A1F2C3D4E5F60718293A4B5C6D7E8F9012345678901234567890ABCDE"
PAYEE = "nano_1payee"
ONE_XNO = 10**30

NODES = [f"http://127.0.0.1:{7100 + n}" for n in range(10)]

# Three ways a node is unreachable, as urllib reports them. All are OSError.
UNREACHABLE_ERRORS = [
    ConnectionRefusedError(111, "Connection refused"),
    TimeoutError("timed out"),
    urllib.error.URLError("[Errno 113] No route to host"),
]

NOT_FOUND = {"error": "Block not found"}


def confirmed_send():
    return {
        "amount": str(ONE_XNO),
        "height": "7",
        "confirmed": "true",
        "subtype": "send",
        "contents": {"link_as_account": PAYEE},
    }


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a socket")

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


def partition(monkeypatch, behaviour):
    """Wire each node URL to a reply, or to an exception it raises when asked."""

    def post_json(rpc_url, payload):
        reply = behaviour[rpc_url]
        if isinstance(reply, BaseException):
            raise reply
        return reply() if callable(reply) else reply

    monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)


def layout(unreachable, not_found, confirmed):
    """10 nodes: the given indices unreachable / not-found / confirming."""
    assert sorted(unreachable + not_found + confirmed) == list(range(10))
    behaviour = {}
    for slot, index in enumerate(unreachable):
        behaviour[NODES[index]] = UNREACHABLE_ERRORS[slot % len(UNREACHABLE_ERRORS)]
    for index in not_found:
        behaviour[NODES[index]] = NOT_FOUND
    for index in confirmed:
        behaviour[NODES[index]] = confirmed_send
    return behaviour


def states(observations):
    return {seen.rpc_url: seen.state for seen in observations}


def assert_partition_kept_apart(observations, unreachable, not_found):
    """Unreachable nodes are unreadable, never not-seen; not-seen is exactly the not-found nodes."""
    by_node = states(observations)
    unreadable = {url for url, state in by_node.items() if state == "unreadable"}
    not_seen = {url for url, state in by_node.items() if state == "not-seen"}
    assert unreadable == {NODES[i] for i in unreachable}
    assert not_seen == {NODES[i] for i in not_found}
    assert not unreadable & not_seen
    for seen in observations:
        if seen.state == "unreadable":
            # It says what went wrong, and that no answer came back.
            assert seen.error_kind in {"ConnectionRefusedError", "TimeoutError", "URLError"}
            assert seen.returned_at == ""
        if seen.state == "not-seen":
            assert seen.error_kind == "" and seen.returned_at != ""


UNREACHABLE = [0, 3, 6]  # 30%, spread through the ask order


def test_30_percent_unreachable_mixed_answers_counts_each_state_once(monkeypatch):
    """3 unreachable, 3 "Block not found", 4 confirming, agree=4: settled, and
    every node is in exactly the state it actually reported."""
    not_found, confirmed = [1, 4, 7], [2, 5, 8, 9]
    partition(monkeypatch, layout(UNREACHABLE, not_found, confirmed))
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, NODES, agree=4)

    assert out.receipt.settled is True
    assert out.stop.reason == "quorum-reached"
    assert Counter(seen.state for seen in out.observations) == Counter(
        {"unreadable": 3, "not-seen": 3, "confirmed": 4}
    )
    assert_partition_kept_apart(out.observations, UNREACHABLE, not_found)
    assert set(out.failed) == {NODES[i] for i in UNREACHABLE}
    assert len(out.answered) == 7

    record = json.loads(out.to_json())
    assert Counter(o["state"] for o in record["observations"]) == Counter(
        {"unreadable": 3, "not-seen": 3, "confirmed": 4}
    )
    assert all("error" in o for o in record["observations"] if o["state"] == "unreadable")
    assert not any("error" in o for o in record["observations"] if o["state"] == "not-seen")
    assert replay_receipt(record)["verdict"] == "match"


def test_30_percent_unreachable_where_they_could_have_made_the_quorum_is_not_yet(monkeypatch):
    """Same partition, agree=5: 4 confirmed, and any one of the 3 unreachable
    nodes could have been the fifth. The result is unsettled - "ask again",
    not a refusal - and the 3 not-found nodes are still the only not-seen."""
    not_found, confirmed = [1, 4, 7], [2, 5, 8, 9]
    partition(monkeypatch, layout(UNREACHABLE, not_found, confirmed))
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, NODES, agree=5)

    assert out.receipt.settled is False
    assert out.stop.reason == "exhausted"
    assert_partition_kept_apart(out.observations, UNREACHABLE, not_found)
    record = json.loads(out.to_json())
    assert record["refusal"] is False
    assert record["ask_again"] is True
    assert replay_receipt(record)["verdict"] == "match"


def test_30_percent_unreachable_and_every_reachable_node_confirms(monkeypatch):
    """3 unreachable, 7 confirming: settled with agree=7, and nothing is not-seen."""
    confirmed = [1, 2, 4, 5, 7, 8, 9]
    partition(monkeypatch, layout(UNREACHABLE, [], confirmed))
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, NODES, agree=7)

    assert out.receipt.settled is True
    assert Counter(seen.state for seen in out.observations) == Counter(
        {"unreadable": 3, "confirmed": 7}
    )
    assert_partition_kept_apart(out.observations, UNREACHABLE, [])


def test_30_percent_unreachable_first_then_default_quorum_leaves_the_rest_not_asked(monkeypatch):
    """With the 3 unreachable nodes asked first and the default agree=2, the
    check settles on the next two and stops: the 5 nodes after it are
    "not-asked" - neither not-seen nor unreadable."""
    first = [0, 1, 2]
    partition(monkeypatch, layout(first, [], [3, 4, 5, 6, 7, 8, 9]))
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, NODES)

    assert out.receipt.settled is True
    assert Counter(seen.state for seen in out.observations) == Counter(
        {"unreadable": 3, "confirmed": 2, "not-asked": 5}
    )
    assert_partition_kept_apart(out.observations, first, [])


@pytest.mark.parametrize("seed", [None, 0, 1, 2**63])
def test_30_percent_unreachable_and_the_rest_not_found_is_unsettled_never_a_refusal(monkeypatch, seed):
    """3 unreachable, 7 "Block not found", agree=2, in any ask order: every
    node is asked, the 7 are not-seen, the 3 are unreadable, and the result is
    an unsettled receipt that says to ask again - not a "no"."""
    not_found = [1, 2, 4, 5, 7, 8, 9]
    partition(monkeypatch, layout(UNREACHABLE, not_found, []))
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, NODES, order_seed=seed)

    assert out.receipt.settled is False
    assert out.stop.reason == "exhausted"
    assert Counter(seen.state for seen in out.observations) == Counter(
        {"unreadable": 3, "not-seen": 7}
    )
    assert_partition_kept_apart(out.observations, UNREACHABLE, not_found)
    assert set(out.failed) == {NODES[i] for i in UNREACHABLE}
    record = json.loads(out.to_json())
    assert record["refusal"] is False
    assert record["ask_again"] is True
    assert replay_receipt(record)["verdict"] == "match"


def test_30_percent_unreachable_short_of_the_quorum_is_not_corroborated(monkeypatch):
    """3 unreachable, 7 "Block not found", agree=8: only 7 nodes could answer,
    so the check raises NotCorroborated ("we could not look") and the 3 are
    its failures - they are not counted among the answers."""
    not_found = [1, 2, 4, 5, 7, 8, 9]
    partition(monkeypatch, layout(UNREACHABLE, not_found, []))
    with pytest.raises(NotCorroborated) as short:
        verify_corroborated(HASH, ONE_XNO, PAYEE, NODES, agree=8)

    assert short.value.answered == 7
    assert set(short.value.failures) == {NODES[i] for i in UNREACHABLE}
    assert short.value.stop.reason == "exhausted"
    assert_partition_kept_apart(short.value.observations, UNREACHABLE, not_found)
