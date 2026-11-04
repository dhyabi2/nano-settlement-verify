"""Laws for nano_quorum: what several nodes have to say before a payment is believed.

No test touches the network: every endpoint is a dict of stubbed replies, and
the no_sockets fixture fails any test that tries to open one.

The shape under test is deliberately paranoid, so these laws are mostly about
what is REFUSED - a duplicate endpoint, a contradiction, a silence mistaken for
a "no" - rather than about the happy path.
"""

import json

import pytest

import nano_quorum
import nano_settlement_verify
from nano_quorum import (
    AGREE_DEFAULT,
    Corroboration,
    Disagreement,
    NotCorroborated,
    balance_corroborated,
    distinct_endpoints,
    post_json_failover,
    verify_corroborated,
)
from nano_settlement_verify import Mismatch

A = "http://127.0.0.1:7071"
B = "http://127.0.0.1:7072"
C = "http://127.0.0.1:7073"

HASH = "B2EC1E2A1F2C3D4E5F60718293A4B5C6D7E8F9012345678901234567890ABCDE"
PAYEE = "nano_1payee"
# balance_corroborated checks the address before any call, so it needs a real one.
HOLDER = "nano_3pmhddz8g4gxhcqwxw68paibs89wb1o7bqgc6twyfkz1a37puf88pa9stsp8"
ONE_XNO = 10**30


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a socket")

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


def send_block(amount=ONE_XNO, payee=PAYEE, height=7, confirmed="true"):
    """A node's block_info reply for a confirmed send of `amount` to `payee`."""
    return {
        "amount": str(amount),
        "height": str(height),
        "confirmed": confirmed,
        "subtype": "send",
        "contents": {"link_as_account": payee},
    }


def nodes(monkeypatch, replies):
    """Wire each endpoint to a reply, a callable, or an exception to raise.

    `replies` maps an endpoint URL to what it does when asked. Anything not in
    the map is an endpoint the test did not expect to be called at all, and
    calling it fails the test - which is how "stops asking once it has agreement"
    is proved rather than asserted.
    """
    called = []

    def post_json(rpc_url, payload):
        called.append(rpc_url)
        if rpc_url not in replies:
            raise AssertionError(f"unexpected call to {rpc_url}")
        reply = replies[rpc_url]
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return reply(payload)
        return reply

    monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)
    return called


# --- the endpoint list itself -------------------------------------------------


def test_one_endpoint_written_twice_is_one_witness_and_is_refused():
    """The whole point is a second opinion; a repeated URL is the same opinion."""
    with pytest.raises(ValueError, match="1 distinct endpoint"):
        distinct_endpoints([A, A], agree=2)


def test_a_trailing_slash_and_a_capital_do_not_make_a_second_witness():
    with pytest.raises(ValueError, match="1 distinct endpoint"):
        distinct_endpoints([A + "/", A.upper()], agree=2)


def test_distinct_endpoints_keeps_the_order_it_was_given():
    assert distinct_endpoints([B, A, C], agree=2) == (B, A, C)


def test_agree_of_one_is_refused_because_that_is_just_verify():
    with pytest.raises(ValueError, match="at least 2"):
        distinct_endpoints([A, B], agree=1)


def test_a_bare_url_string_is_not_a_list_of_endpoints():
    """"http://a" iterated character by character would be eleven "endpoints"."""
    with pytest.raises(TypeError, match="not one URL string"):
        distinct_endpoints(A, agree=2)


def test_fewer_endpoints_than_the_agreement_asks_for_is_refused_before_any_call():
    with pytest.raises(ValueError, match="2 distinct endpoint"):
        distinct_endpoints([A, B], agree=3)


# --- agreement ----------------------------------------------------------------


def test_two_nodes_reporting_the_same_send_settle_it(monkeypatch):
    nodes(monkeypatch, {A: send_block(), B: send_block()})
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    assert isinstance(out, Corroboration)
    assert out.receipt.settled is True
    assert out.receipt.amount_raw == ONE_XNO
    assert out.agreed == (A, B)
    assert out.failed == ()


def test_it_stops_asking_once_it_has_agreement(monkeypatch):
    """C is not in the map, so calling it fails the test."""
    called = nodes(monkeypatch, {A: send_block(), B: send_block()})
    verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C])
    assert called == [A, B]


def test_one_node_alone_does_not_settle_it(monkeypatch):
    """A says confirmed, B has not seen the block. That is not agreement."""
    nodes(monkeypatch, {A: send_block(), B: {"error": "Block not found"}})
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    assert out.receipt.settled is False
    assert out.agreed == ()
    assert out.answered == (A, B)


def test_two_nodes_that_both_say_not_yet_confirmed_are_an_honest_not_yet(monkeypatch):
    nodes(monkeypatch, {A: send_block(confirmed="false"), B: send_block(confirmed="false")})
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    assert out.receipt.settled is False
    assert out.answered == (A, B)
    assert out.failed == ()


def test_three_endpoints_can_be_asked_to_agree(monkeypatch):
    nodes(monkeypatch, {A: send_block(), B: send_block(), C: send_block()})
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C], agree=3)
    assert out.receipt.settled is True
    assert out.agreed == (A, B, C)


# --- refusing ------------------------------------------------------------------


def test_two_nodes_that_confirm_different_heights_for_one_hash_disagree(monkeypatch):
    """A block's contents are fixed by its hash. One of these endpoints is lying."""
    nodes(monkeypatch, {A: send_block(height=7), B: send_block(height=9)})
    with pytest.raises(Disagreement) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    assert set(caught.value.answers) == {A, B}


def test_a_disagreement_keeps_both_nodes_observations_not_just_the_refusal(monkeypatch):
    """A dispute record needs what each node said, in full, and nothing served."""
    nodes(monkeypatch, {A: send_block(height=7), B: send_block(height=9)})
    with pytest.raises(Disagreement) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    error = caught.value
    assert error.observations[A].height == 7 and error.observations[B].height == 9
    assert error.observations[A].settled and error.observations[B].settled
    assert error.observations[A].amount_raw == ONE_XNO
    assert error.observations[A].account == PAYEE
    record = json.loads(json.dumps(error.record()))
    assert record["verdict"] == "disagreement"
    assert record["served"] is None
    assert [o["rpc_url"] for o in record["observations"]] == [A, B]
    assert [o["payload"]["height"] for o in record["observations"]] == [7, 9]
    assert record["observations"][0]["payload"]["amount_raw"] == ONE_XNO
    assert record["observations"][0]["answer"] == error.answers[A]


def test_a_disagreement_message_and_answers_are_unchanged(monkeypatch):
    nodes(monkeypatch, {A: send_block(height=7), B: send_block(height=9)})
    with pytest.raises(Disagreement) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    a7 = str(("settled", ONE_XNO, 7, PAYEE))
    b9 = str(("settled", ONE_XNO, 9, PAYEE))
    assert caught.value.answers == {A: a7, B: b9}
    assert str(caught.value) == f"endpoints disagree: {A}: {a7}; {B}: {b9}"
    assert Disagreement({A: "x"}).observations == {}


def test_a_disagreement_after_two_agreeing_nodes_keeps_every_node_with_its_own_receipt(
    monkeypatch,
):
    """With agree=3, A and B agree and C contradicts: all three are on record.

    Each receipt is told apart by `seen_at`, which is not part of the answer
    compared, so the record shows whose receipt is attributed to whom.
    """
    heights = {A: 7, B: 7, C: 9}

    def verify(block_hash, expect_raw, account, rpc_url):
        return nano_settlement_verify.Receipt(
            settled=True,
            amount_raw=expect_raw,
            height=heights[rpc_url],
            account=account,
            seen_at={A: 1, B: 2, C: 3}[rpc_url],
        )

    monkeypatch.setattr(nano_settlement_verify, "verify", verify)
    with pytest.raises(Disagreement) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C], agree=3)
    error = caught.value
    assert list(error.observations) == [A, B, C]
    assert {url: r.seen_at for url, r in error.observations.items()} == {A: 1, B: 2, C: 3}
    assert list(error.observed_answers) == [A, B, C]
    assert error.observed_answers[A] == error.observed_answers[B] != error.observed_answers[C]
    record = json.loads(json.dumps(error.record(), allow_nan=False))
    assert [o["rpc_url"] for o in record["observations"]] == [A, B, C]
    assert [o["payload"]["height"] for o in record["observations"]] == [7, 7, 9]
    assert all(o["answer"] is not None for o in record["observations"])
    assert record["served"] is None


def test_a_balance_disagreement_after_two_agreeing_nodes_keeps_every_node(monkeypatch):
    nodes(monkeypatch, {A: info(ONE_XNO, 5), B: info(ONE_XNO, 5), C: info(ONE_XNO - 1, 5)})
    with pytest.raises(Disagreement) as caught:
        balance_corroborated(HOLDER, [A, B, C], agree=3)
    assert caught.value.observations == {A: (ONE_XNO, 5), B: (ONE_XNO, 5), C: (ONE_XNO - 1, 5)}


def test_a_disagreement_record_is_always_json_even_for_types_json_does_not_know():
    """record() feeds a dispute log; it must never raise in json.dumps."""

    class Opaque:
        def __repr__(self):
            return "Opaque()"

    class BadRepr:
        def __repr__(self):
            raise RuntimeError("no")

    from decimal import Decimal

    error = Disagreement(
        {A: "x", B: "y"},
        observations={
            A: {"blob": b"\x00\xff", "d": Decimal("1.5"), "o": Opaque(), 3: BadRepr()},
            B: (float("nan"), float("inf"), {Opaque()}, 10**60, None, True),
        },
    )
    text = json.dumps(error.record(), allow_nan=False)
    record = json.loads(text)
    a, b = (o["payload"] for o in record["observations"])
    assert a == [
        ["blob", repr(b"\x00\xff")],
        ["d", "Decimal('1.5')"],
        ["o", "Opaque()"],
        [3, "<unrepresentable BadRepr>"],
    ]
    assert b == ["nan", "inf", ["Opaque()"], 10**60, None, True]
    assert isinstance(b[3], int)
    looped = []
    looped.append(looped)
    json.dumps(Disagreement({A: "x"}, observations={A: looped}).record(), allow_nan=False)


def _three_receipts(monkeypatch, heights):
    def verify(block_hash, expect_raw, account, rpc_url):
        return nano_settlement_verify.Receipt(
            settled=True, amount_raw=expect_raw, height=heights[rpc_url], account=account
        )

    monkeypatch.setattr(nano_settlement_verify, "verify", verify)


def test_with_agree_3_a_disagreement_keeps_the_answers_and_message_it_always_had(monkeypatch):
    """`answers` and the message name the first settled endpoint and the contradictor.

    Callers written before observations existed read exactly two entries; the
    agreeing B is recorded only in the new, additive attributes.
    """
    _three_receipts(monkeypatch, {A: 7, B: 7, C: 9})
    with pytest.raises(Disagreement) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C], agree=3)
    a7 = str(("settled", ONE_XNO, 7, PAYEE))
    c9 = str(("settled", ONE_XNO, 9, PAYEE))
    assert caught.value.answers == {A: a7, C: c9}
    assert str(caught.value) == f"endpoints disagree: {A}: {a7}; {C}: {c9}"
    assert list(caught.value.observations) == [A, B, C]
    assert caught.value.observed_answers == {A: a7, B: a7, C: c9}
    record = caught.value.record()
    assert [(o["rpc_url"], o["answer"]) for o in record["observations"]] == [
        (A, a7), (B, a7), (C, c9)
    ]


def test_with_agree_3_a_balance_disagreement_keeps_the_answers_it_always_had(monkeypatch):
    nodes(monkeypatch, {A: info(ONE_XNO, 5), B: info(ONE_XNO, 5), C: info(ONE_XNO - 1, 5)})
    with pytest.raises(Disagreement) as caught:
        balance_corroborated(HOLDER, [A, B, C], agree=3)
    assert caught.value.answers == {A: str((ONE_XNO, 5)), C: str((ONE_XNO - 1, 5))}
    assert str(caught.value) == (
        f"endpoints disagree: {A}: {(ONE_XNO, 5)}; {C}: {(ONE_XNO - 1, 5)}"
    )


def test_a_disagreement_record_never_raises_on_caller_built_observations():
    """Hostile or odd values are written, not raised: record() feeds a dispute log."""

    class Hostile:
        def __getattr__(self, name):
            raise RuntimeError("no attribute for you")

    deep = []
    node = deep
    for _ in range(5000):
        node.append([])
        node = node[0]
    error = Disagreement(
        {b"raw-url": "x", A: "y"},
        observations={A: Hostile(), B: deep},
    )
    record = json.loads(json.dumps(error.record(), allow_nan=False))
    by_url = {o["rpc_url"]: o for o in record["observations"]}
    assert set(by_url) == {A, B, repr(b"raw-url")}
    assert by_url[A]["payload"].startswith("<") and "Hostile" in by_url[A]["payload"]
    assert by_url[repr(b"raw-url")]["answer"] == "x"
    assert record["served"] is None


def test_a_disagreement_built_from_non_mappings_is_a_type_error():
    """The one refusal is structural, a TypeError, before anything is recorded."""
    with pytest.raises(TypeError, match="observations must be a mapping"):
        Disagreement({A: "x"}, observations=[1, 2])
    with pytest.raises(TypeError, match="answers must be a mapping"):
        Disagreement([(A, "x")])
    error = Disagreement({A: "x"})
    error.observations = "not a mapping"
    with pytest.raises(TypeError, match="observations must be a mapping"):
        error.record()


def test_a_disagreement_record_keeps_seen_at_and_the_callers_keys():
    receipt = nano_settlement_verify.Receipt(
        settled=True, amount_raw=ONE_XNO, height=7, account=PAYEE, seen_at=1700000000
    )
    error = Disagreement(
        {A: "x", B: "y"},
        observations={A: receipt, B: {1: "int key", "1": "str key", "k": "v"}},
    )
    a, b = (o["payload"] for o in json.loads(json.dumps(error.record()))["observations"])
    assert a["seen_at"] == 1700000000 and a["not_after"] is None
    assert a["amount_raw"] == ONE_XNO and a["height"] == 7 and a["account"] == PAYEE
    assert b == [[1, "int key"], ["1", "str key"], ["k", "v"]]
    plain = Disagreement({A: "x"}, observations={A: {"Rpc-Url": 1, "seen_at": 2}}).record()
    assert plain["observations"][0]["payload"] == {"Rpc-Url": 1, "seen_at": 2}


def test_a_disagreement_is_not_resolved_by_a_third_vote(monkeypatch):
    """Two against one is still two nodes that cannot both be right."""
    nodes(monkeypatch, {A: send_block(height=7), B: send_block(height=9), C: send_block(height=9)})
    with pytest.raises(Disagreement):
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C], agree=2)


def test_one_node_saying_it_paid_someone_else_refuses_at_once(monkeypatch):
    """B is never asked: a confirmed block cannot be un-confirmed by another node."""
    called = nodes(monkeypatch, {A: send_block(payee="nano_1stranger")})
    with pytest.raises(Mismatch):
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    assert called == [A]


def test_one_node_reporting_a_short_amount_refuses_at_once(monkeypatch):
    nodes(monkeypatch, {A: send_block(amount=ONE_XNO - 1)})
    with pytest.raises(Mismatch) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    assert caught.value.got == ONE_XNO - 1


def test_a_receive_block_is_refused_however_many_nodes_confirm_it(monkeypatch):
    reply = send_block()
    reply["subtype"] = "receive"
    nodes(monkeypatch, {A: reply})
    with pytest.raises(Mismatch) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    assert caught.value.expected == "send"


def test_a_float_expectation_is_refused_before_a_single_endpoint_is_called(monkeypatch):
    called = nodes(monkeypatch, {})
    with pytest.raises(TypeError, match="must be an int of raw"):
        verify_corroborated(HASH, 1e30, PAYEE, [A, B])
    assert called == []


# --- silence is not a no --------------------------------------------------------


def test_every_endpoint_unreachable_is_not_checked_rather_than_not_paid(monkeypatch):
    """The costly confusion: refusing a call the buyer already paid for."""
    nodes(monkeypatch, {A: OSError("connection refused"), B: OSError("timed out")})
    with pytest.raises(NotCorroborated) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    assert caught.value.answered == 0
    assert set(caught.value.failures) == {A, B}


def test_one_endpoint_down_and_one_confirming_is_not_corroborated(monkeypatch):
    """Not an unsettled receipt: one witness short is "could not look", not "not yet"."""
    nodes(monkeypatch, {A: OSError("refused"), B: send_block()})
    with pytest.raises(NotCorroborated) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    assert caught.value.answered == 1
    assert caught.value.agree == 2


def test_a_proxys_html_error_page_counts_as_silence_not_as_an_answer(monkeypatch):
    """json.JSONDecodeError is a ValueError; it means the endpoint told us nothing."""
    nodes(
        monkeypatch,
        {A: json.JSONDecodeError("Expecting value", "<html>503</html>", 0), B: send_block()},
    )
    with pytest.raises(NotCorroborated) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    assert "JSONDecodeError" in caught.value.failures[A]


def test_json_that_is_not_a_node_reply_counts_as_silence(monkeypatch):
    """`verify` raises NotANodeReply on a reply with neither `error` nor `confirmed`.

    That is deliberate there - it must not read as settled or as unsettled - and
    here it means the same as a closed socket: this endpoint told us nothing.
    `NotANodeReply` is a ValueError, so `_UNREADABLE` catches it exactly as it
    caught the bare KeyError this used to raise; what changed is only that a
    seller calling `verify` on its own can now catch it too.
    """
    nodes(monkeypatch, {A: {"status": "ok"}, B: send_block()})
    with pytest.raises(NotCorroborated) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    assert "NotANodeReply" in caught.value.failures[A]


def test_a_reply_that_is_not_a_json_object_counts_as_failed_not_answered(monkeypatch):
    """A proxy answering `"error"` is silence, not a node saying "not seen".

    It used to reach `verify` as NotFound and count as an answer, so with A
    confirming the pair came back as an unsettled Corroboration. Now `verify`
    raises NotANodeReply, B is a failed endpoint, and one witness is short.
    """
    nodes(monkeypatch, {A: send_block(), B: "error"})
    with pytest.raises(NotCorroborated) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B], agree=2)
    assert caught.value.answered == 1
    assert list(caught.value.failures) == [B]
    assert "NotANodeReply" in caught.value.failures[B]


def test_a_dead_endpoint_does_not_stop_two_live_ones_agreeing(monkeypatch):
    nodes(monkeypatch, {A: OSError("refused"), B: send_block(), C: send_block()})
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C])
    assert out.receipt.settled is True
    assert out.agreed == (B, C)
    assert out.failed == (A,)


def test_an_endpoint_is_not_retried(monkeypatch):
    called = nodes(monkeypatch, {A: OSError("refused"), B: send_block(), C: send_block()})
    verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C])
    assert called.count(A) == 1


# --- the record ------------------------------------------------------------------


def test_the_json_record_carries_raw_as_an_integer_and_names_the_witnesses(monkeypatch):
    nodes(monkeypatch, {A: send_block(), B: send_block()})
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    text = out.to_json()
    assert f'"amount_raw": {ONE_XNO}' in text  # unquoted: a float would have truncated it
    assert json.loads(text)["agreed"] == [A, B]
    assert json.loads(text)["amount_raw"] == ONE_XNO


# --- failover, which is not corroboration -------------------------------------------


def test_failover_returns_the_first_endpoint_that_answers(monkeypatch):
    nodes(monkeypatch, {A: OSError("refused"), B: {"representative": "nano_1rep"}})
    reply, rpc_url = post_json_failover([A, B], {"action": "account_info"})
    assert reply == {"representative": "nano_1rep"}
    assert rpc_url == B


def test_failover_does_not_ask_a_second_endpoint_once_one_answered(monkeypatch):
    called = nodes(monkeypatch, {A: {"ok": True}})
    post_json_failover([A, B], {"action": "version"})
    assert called == [A]


def test_failover_with_every_endpoint_dead_says_nothing_was_read(monkeypatch):
    nodes(monkeypatch, {A: OSError("refused"), B: OSError("refused")})
    with pytest.raises(NotCorroborated):
        post_json_failover([A, B], {"action": "version"})


def test_failover_refuses_a_bare_url_string(monkeypatch):
    with pytest.raises(TypeError, match="not one URL string"):
        post_json_failover(A, {"action": "version"})


# --- balances ------------------------------------------------------------------------


def info(balance, receivable=0):
    return {"confirmed_balance": str(balance), "confirmed_receivable": str(receivable)}


def test_two_nodes_agreeing_on_a_confirmed_balance(monkeypatch):
    nodes(monkeypatch, {A: info(ONE_XNO, 5), B: info(ONE_XNO, 5)})
    assert balance_corroborated(HOLDER, [A, B]) == (ONE_XNO, 5)


def test_a_balance_comes_back_as_integers_of_raw_not_as_strings(monkeypatch):
    nodes(monkeypatch, {A: info(ONE_XNO), B: info(ONE_XNO)})
    balance, receivable = balance_corroborated(HOLDER, [A, B])
    assert isinstance(balance, int) and isinstance(receivable, int)
    assert balance == ONE_XNO  # not 1e30, which is 19884624838656 raw short


def test_two_nodes_reporting_different_confirmed_balances_disagree(monkeypatch):
    nodes(monkeypatch, {A: info(ONE_XNO), B: info(ONE_XNO - 1)})
    with pytest.raises(Disagreement):
        balance_corroborated(HOLDER, [A, B])


def test_a_balance_disagreement_keeps_both_figures(monkeypatch):
    nodes(monkeypatch, {A: info(ONE_XNO, 5), B: info(ONE_XNO - 1, 5)})
    with pytest.raises(Disagreement) as caught:
        balance_corroborated(HOLDER, [A, B])
    assert caught.value.observations == {A: (ONE_XNO, 5), B: (ONE_XNO - 1, 5)}
    record = json.loads(json.dumps(caught.value.record()))
    assert record["served"] is None
    assert [o["payload"] for o in record["observations"]] == [[ONE_XNO, 5], [ONE_XNO - 1, 5]]


def test_an_account_neither_node_has_seen_opened_is_a_balance_of_nothing(monkeypatch):
    nodes(monkeypatch, {A: {"error": "Account not found"}, B: {"error": "Account not found"}})
    assert balance_corroborated(HOLDER, [A, B]) == (0, 0)


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("error", id="the bare string 'error'"),
        pytest.param("no error here", id="a status page mentioning the word error"),
        pytest.param("rate limit error: slow down", id="a rate-limit envelope"),
        pytest.param(["error"], id="a JSON array containing 'error'"),
        pytest.param(7, id="a bare number"),
        pytest.param("maintenance", id="a maintenance page"),
    ],
)
def test_a_reply_that_is_not_a_json_object_counts_as_failed_not_as_a_zero_balance(
    monkeypatch, reply
):
    """README.md: "An endpoint whose reply is not a JSON object (a proxy's
    `"error"`, a bare number) is silence too: it counts as failed, not as
    answered."

    `verify` was given this refusal by #27; this function reads a reply
    directly and was not. `"error" in reply` on a string is a SUBSTRING test,
    so every proxy body carrying the word took the unopened-account branch and
    counted as an endpoint reporting a confirmed balance of nothing. Two of
    them corroborated a balance of nothing for an account that may hold a
    fortune - the worst possible direction for a seller deciding whether a
    buyer can pay.
    """
    nodes(monkeypatch, {A: reply, B: reply})
    with pytest.raises(NotCorroborated):
        balance_corroborated(HOLDER, [A, B])


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1e30, id="a JSON float that cannot represent 10**30"),
        pytest.param(True, id="the JSON true"),
        pytest.param(float("inf"), id="an infinite JSON number"),
        pytest.param(float("nan"), id="a NaN JSON number"),
    ],
)
def test_a_balance_that_is_a_json_number_is_refused_not_rounded(monkeypatch, value):
    """A node sends raw as a decimal string. `int(1e30)` is
    1000000000000000019884624838656 - 19884624838656 raw ABOVE one XNO, because
    no float represents 10**30 - and a balance read too high says an account
    can cover a price it cannot. `int(True)` is 1.

    The infinite case is also a crash: `int(float("inf"))` raises
    OverflowError, which is NOT a ValueError and so is not in `_UNREADABLE`.
    On main it escaped the `(OSError, ValueError)` family README.md tells a
    caller to catch, out of a function whose whole contract is to raise
    `NotCorroborated` or `Disagreement`.
    """
    reply = {"confirmed_balance": value, "confirmed_receivable": "0"}
    nodes(monkeypatch, {A: reply, B: reply})
    with pytest.raises(NotCorroborated):
        balance_corroborated(HOLDER, [A, B])


def test_a_receivable_that_is_a_json_number_is_refused_too(monkeypatch):
    """Both figures are money; the receivable gets the same rule."""
    reply = {"confirmed_balance": str(ONE_XNO), "confirmed_receivable": 1e30}
    nodes(monkeypatch, {A: reply, B: reply})
    with pytest.raises(NotCorroborated):
        balance_corroborated(HOLDER, [A, B])


def test_an_error_envelope_from_a_real_node_is_still_a_balance_of_nothing(monkeypatch):
    """The guard above must not take the unopened-account branch away: a node's
    own `{"error": ...}` object is still two nodes agreeing on nothing."""
    nodes(monkeypatch, {A: {"error": "Account not found"}, B: {"error": "Account not found"}})
    assert balance_corroborated(HOLDER, [A, B]) == (0, 0)


def test_a_balance_one_endpoint_short_is_not_corroborated(monkeypatch):
    nodes(monkeypatch, {A: OSError("refused"), B: info(ONE_XNO)})
    with pytest.raises(NotCorroborated):
        balance_corroborated(HOLDER, [A, B])


def test_the_default_agreement_is_two(monkeypatch):
    assert AGREE_DEFAULT == 2


def test_a_node_that_ignores_include_confirmed_has_not_answered(monkeypatch):
    """Its `balance` is the UNCONFIRMED figure - the one that must not be trusted.

    Reading it anyway would let two endpoints "agree" on a number neither was
    asked for, which is worse than no answer at all.
    """
    nodes(monkeypatch, {A: {"balance": str(ONE_XNO)}, B: info(ONE_XNO)})
    with pytest.raises(NotCorroborated) as caught:
        balance_corroborated(HOLDER, [A, B])
    assert "KeyError" in caught.value.failures[A]


def test_an_account_not_found_reply_is_zero_even_though_it_carries_a_balance(monkeypatch):
    """A real node answers `{"error": "Account not found", "balance": "0"}`."""
    not_found = {"error": "Account not found", "balance": "0"}
    nodes(monkeypatch, {A: not_found, B: not_found})
    assert balance_corroborated(HOLDER, [A, B]) == (0, 0)


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param({"error": "Rate limit exceeded"}, id="a rate-limit object"),
        pytest.param({"error": "Invalid API key"}, id="an API-key refusal"),
        pytest.param({"error": "Unable to parse JSON"}, id="a node refusing the request"),
        pytest.param({"error": None, "confirmed_balance": "5", "confirmed_receivable": "0"},
                     id="error null alongside figures"),
        pytest.param({"error": ""}, id="an empty error"),
    ],
)
def test_an_error_other_than_account_not_found_counts_as_failed_not_as_zero(
    monkeypatch, reply
):
    """Only "Account not found" is a balance of nothing; `nano_claim._opened`
    reads it the same way. Any other `error` is an endpoint that did not answer
    the question: two rate-limited endpoints corroborated a balance of nothing
    for an account that may hold a fortune. An `error` key of null next to
    figures is not a node's account_info reply either, so it is not read."""
    nodes(monkeypatch, {A: reply, B: reply})
    with pytest.raises(NotCorroborated) as caught:
        balance_corroborated(HOLDER, [A, B])
    assert "NotANodeReply" in caught.value.failures[A]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("-5", id="a negative string"),
        pytest.param(-5, id="a negative integer"),
        pytest.param("1_000", id="underscores"),
        pytest.param(" 7 ", id="surrounding whitespace"),
        pytest.param("+9", id="a plus sign"),
        pytest.param("٣", id="an Arabic-Indic digit"),
        pytest.param("", id="an empty string"),
        pytest.param(None, id="null"),
        pytest.param([], id="a list"),
    ],
)
def test_a_balance_that_is_not_a_digit_string_is_refused(monkeypatch, value):
    """`int()` reads all of these; a node sends none of them. This is the rule
    `nano_payers._int_field` aims at for a history row's amount, made
    ASCII-only: `_int_field` uses `isdigit()` alone, so it would pass the
    Arabic-Indic digit that a balance here refuses."""
    reply = {"confirmed_balance": value, "confirmed_receivable": "0"}
    nodes(monkeypatch, {A: reply, B: reply})
    with pytest.raises(NotCorroborated) as caught:
        balance_corroborated(HOLDER, [A, B])
    assert "NotANodeReply" in caught.value.failures[A]


def test_a_balance_sent_as_a_json_integer_is_still_read(monkeypatch):
    reply = {"confirmed_balance": ONE_XNO, "confirmed_receivable": 0}
    nodes(monkeypatch, {A: reply, B: reply})
    assert balance_corroborated(HOLDER, [A, B]) == (ONE_XNO, 0)


@pytest.mark.parametrize(
    "account",
    [
        pytest.param("nano_1payee", id="truncated"),
        pytest.param(HOLDER[:-1] + ("9" if HOLDER[-1] != "9" else "8"), id="a bad checksum"),
        pytest.param("bc1qexample", id="no nano prefix"),
        pytest.param("", id="empty"),
        pytest.param(None, id="not a string"),
    ],
)
def test_a_malformed_address_is_the_callers_error_and_no_node_is_asked(
    monkeypatch, account
):
    """A node answers a mistyped address with "Bad account number". Counted as
    a failed endpoint that would be NotCorroborated, which says "ask again" -
    forever, for an address that cannot exist. It is a ValueError instead."""
    bad = {"error": "Bad account number"}
    called = nodes(monkeypatch, {A: bad, B: bad})
    with pytest.raises(ValueError, match="not a Nano address") as caught:
        balance_corroborated(account, [A, B])
    assert not isinstance(caught.value, NotCorroborated)
    assert called == []


def test_a_valid_unopened_address_still_reads_as_nothing(monkeypatch):
    called = nodes(
        monkeypatch,
        {A: {"error": "Account not found"}, B: {"error": "Account not found"}},
    )
    assert balance_corroborated(HOLDER, [A, B]) == (0, 0)
    assert called == [A, B]


# --- one broken proxy must not override a node that read the ledger -----------


def test_a_proxy_that_ignores_json_block_is_skipped_not_believed(monkeypatch):
    """`verify_corroborated` raises on the first `mismatch` it hears, because a
    confirmed block's contents are fixed and no later endpoint can overturn
    them. That reasoning holds for an endpoint that READ the block. An endpoint
    whose `contents` came back as an opaque string read nothing, so its
    "mismatch" was manufactured - and raising on it refused a good payment and
    left the honest endpoints after it unasked.
    """
    good = send_block()
    proxy = {**good, "contents": json.dumps(good["contents"])}
    called = nodes(monkeypatch, {A: proxy, B: good, C: good})

    result = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C], agree=2)

    assert result.receipt.settled is True
    assert result.agreed == (B, C)
    assert A in result.failed
    assert called == [A, B, C], "the proxy was asked, skipped, and the others still answered"


def test_an_endpoint_that_really_reports_another_payee_still_refuses(monkeypatch):
    """The control: a readable mismatch is still immediate and still terminal."""
    nodes(monkeypatch, {A: send_block(payee="nano_1someoneelse"), B: send_block()})

    with pytest.raises(Mismatch):
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B], agree=2)


def test_every_endpoint_unreadable_is_not_corroborated_not_a_mismatch(monkeypatch):
    """"Nobody could tell us" keeps its own name even when every node is a proxy."""
    good = send_block()
    proxy = {**good, "contents": json.dumps(good["contents"])}
    nodes(monkeypatch, {A: proxy, B: proxy})

    with pytest.raises(NotCorroborated) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B], agree=2)

    assert caught.value.answered == 0
