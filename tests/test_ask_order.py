"""Laws for the ask order: who went first is in the receipt, and a replay agrees.

Once `verify_corroborated` can stop early, the order it asks in decides which
nodes made up the k and which ended up "not-asked". So the receipt records that
order (`ask_order`, the rule and seed it came from, each node's `ask_index`), and
`replay_receipt` re-runs the stop from the record alone.

No test touches the network: every endpoint is a stubbed reply.
"""

import json

import pytest

import nano_quorum
import nano_settlement_verify
from nano_quorum import replay_receipt, verify_corroborated

A = "http://127.0.0.1:7071"
B = "http://127.0.0.1:7072"
C = "http://127.0.0.1:7073"
D = "http://127.0.0.1:7074"

HASH = "B2EC1E2A1F2C3D4E5F60718293A4B5C6D7E8F9012345678901234567890ABCDE"
PAYEE = "nano_1payee"
ONE_XNO = 10**30


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a socket")

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


def send_block(confirmed="true"):
    return {
        "amount": str(ONE_XNO),
        "height": "7",
        "confirmed": confirmed,
        "subtype": "send",
        "contents": {"link_as_account": PAYEE},
    }


def nodes(monkeypatch, replies):
    """Wire each endpoint to a reply or an exception; return the calls, in order."""
    called = []

    def post_json(rpc_url, payload):
        called.append(rpc_url)
        reply = replies[rpc_url]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)
    return called


def asked_in_record(record):
    return [entry["rpc_url"] for entry in record["observations"] if entry["state"] != "not-asked"]


def not_asked_in_record(record):
    return [entry["rpc_url"] for entry in record["observations"] if entry["state"] == "not-asked"]


ALL_CONFIRM = {A: send_block(), B: send_block(), C: send_block(), D: send_block()}


# --- (1) the receipt records the order, and it is the order the nodes were called in


@pytest.mark.parametrize("seed", [None, 7])
def test_ask_order_is_recorded_and_is_the_sequence_the_nodes_were_called_in(monkeypatch, seed):
    replies = {A: OSError("refused"), B: {"error": "Block not found"},
               C: send_block(confirmed="false"), D: send_block()}
    called = nodes(monkeypatch, replies)
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C, D], order_seed=seed)
    record = json.loads(out.to_json())
    assert record["configured_order"] == [A, B, C, D]
    assert record["ask_order"] == list(out.ask_order) == called  # every node asked here
    assert record["order_rule"] == ("as-given" if seed is None else "seed-sha256")
    assert record["order_seed"] == seed
    assert [entry["ask_index"] for entry in record["observations"]] == [0, 1, 2, 3]
    assert [entry["rpc_url"] for entry in record["observations"]] == record["ask_order"]
    assert replay_receipt(record)["verdict"] == "match"


def test_as_given_is_the_caller_order_after_duplicates_are_folded(monkeypatch):
    called = nodes(monkeypatch, ALL_CONFIRM)
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [C, A, C + "/", B], agree=3)
    assert out.ask_order == (C, A, B) == tuple(called)
    assert out.order_rule == "as-given" and out.order_seed is None


# --- (2) on an early stop, the not-asked nodes are the tail of ask_order


@pytest.mark.parametrize("seed", [None, 1, 2, 3])
def test_on_quorum_reached_the_not_asked_set_is_the_tail_of_ask_order(monkeypatch, seed):
    called = nodes(monkeypatch, ALL_CONFIRM)
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C, D], agree=2, order_seed=seed)
    record = json.loads(out.to_json())
    assert record["stop"]["reason"] == "quorum-reached"
    order = record["ask_order"]
    assert len(order) == 4 and sorted(order) == sorted([A, B, C, D])
    assert called == order[:2] == record["agreed"] == asked_in_record(record)
    assert not_asked_in_record(record) == order[2:]
    # The not-asked nodes keep their planned place; their ask_index is not null.
    tail = [entry for entry in record["observations"] if entry["state"] == "not-asked"]
    assert [entry["ask_index"] for entry in tail] == [2, 3]
    replayed = replay_receipt(record)
    assert replayed["verdict"] == "match", replayed["problems"]
    assert replayed["expected_not_asked"] == order[2:]
    assert replayed["expected_stop"] == "quorum-reached"


# --- (3) same seed, same order; another order, another not-asked set, both replay


def test_the_same_seed_gives_the_same_order_and_the_same_not_asked_set(monkeypatch):
    runs = []
    for _ in range(2):
        nodes(monkeypatch, ALL_CONFIRM)
        record = json.loads(
            verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C, D], order_seed=42).to_json()
        )
        runs.append((record["ask_order"], not_asked_in_record(record)))
    assert runs[0] == runs[1]
    # And the order is the documented rule, recomputable without running the check.
    assert list(nano_quorum._ask_order([A, B, C, D], 42)) == runs[0][0]


def test_another_order_of_the_same_answers_leaves_others_not_asked_and_both_replay(monkeypatch):
    nodes(monkeypatch, ALL_CONFIRM)
    given = json.loads(verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C, D]).to_json())
    seed = next(
        s for s in range(100)
        if set(nano_quorum._ask_order([A, B, C, D], s)[2:]) != set(given["ask_order"][2:])
    )
    nodes(monkeypatch, ALL_CONFIRM)
    shuffled = json.loads(
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C, D], order_seed=seed).to_json()
    )
    # Same answers from the same nodes, same verdict ...
    assert given["settled"] is shuffled["settled"] is True
    assert given["stop"]["reason"] == shuffled["stop"]["reason"] == "quorum-reached"
    # ... but a different set of nodes made the k, and a different set was not asked.
    assert set(not_asked_in_record(given)) != set(not_asked_in_record(shuffled))
    assert set(given["agreed"]) != set(shuffled["agreed"])
    for record in (given, shuffled):
        assert replay_receipt(record)["verdict"] == "match"


# --- (4) a tampered order fails the replay


@pytest.mark.parametrize("seed", [None, 5])
def test_a_receipt_with_two_ask_order_entries_swapped_fails_the_replay(monkeypatch, seed):
    nodes(monkeypatch, ALL_CONFIRM)
    record = json.loads(
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C, D], order_seed=seed).to_json()
    )
    assert replay_receipt(record)["verdict"] == "match"
    tampered = json.loads(json.dumps(record))
    order = tampered["ask_order"]
    order[1], order[3] = order[3], order[1]
    out = replay_receipt(tampered)
    assert out["verdict"] == "mismatch"
    assert any("ask_order" in problem for problem in out["problems"])


def test_swapping_the_order_and_the_observations_together_still_fails(monkeypatch):
    """A consistent rewrite - ask_order, the observations and their ask_index all
    moved together - is still not the order the rule gives, and the walk no
    longer stops where the record says."""
    nodes(monkeypatch, ALL_CONFIRM)
    record = json.loads(verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C, D]).to_json())
    tampered = json.loads(json.dumps(record))
    tampered["ask_order"][0], tampered["ask_order"][3] = tampered["ask_order"][3], tampered["ask_order"][0]
    seen = tampered["observations"]
    seen[0], seen[3] = seen[3], seen[0]
    seen[0]["ask_index"], seen[3]["ask_index"] = 0, 3
    out = replay_receipt(tampered)
    assert out["verdict"] == "mismatch"
    assert len(out["problems"]) >= 2


def test_a_node_marked_not_asked_before_the_quorum_fails_the_replay(monkeypatch):
    nodes(monkeypatch, {A: {"error": "Block not found"}, B: send_block(), C: send_block()})
    record = json.loads(verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C]).to_json())
    assert replay_receipt(record)["verdict"] == "match"
    tampered = json.loads(json.dumps(record))
    tampered["observations"][0].update(
        state="not-asked", read_at=None, attempted_at=None, returned_at=None, detail="not asked"
    )
    assert replay_receipt(tampered)["verdict"] == "mismatch"


def test_the_replay_flag_exits_0_on_a_match_and_1_on_a_mismatch(monkeypatch, tmp_path, capsys):
    nodes(monkeypatch, ALL_CONFIRM)
    record = json.loads(verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C, D]).to_json())
    good = tmp_path / "good.json"
    good.write_text(json.dumps(record), encoding="utf-8")
    record["ask_order"].reverse()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(record), encoding="utf-8")
    assert nano_quorum._main(["--replay", str(good)]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "match"
    assert nano_quorum._main(["--replay", str(bad)]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "mismatch"


@pytest.mark.parametrize("seed", [True, 1.5, "7", -1, 2**64])
def test_a_seed_that_is_not_a_small_int_is_refused_before_any_call(monkeypatch, seed):
    called = nodes(monkeypatch, ALL_CONFIRM)
    with pytest.raises((TypeError, ValueError), match="order_seed"):
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B], order_seed=seed)
    assert called == []
