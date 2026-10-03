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
    """`verify` raises KeyError on a reply with neither `error` nor `confirmed`.

    That is deliberate there - it must not read as settled or as unsettled - and
    here it means the same as a closed socket: this endpoint told us nothing.
    """
    nodes(monkeypatch, {A: {"status": "ok"}, B: send_block()})
    with pytest.raises(NotCorroborated) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    assert "KeyError" in caught.value.failures[A]


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
    assert balance_corroborated(PAYEE, [A, B]) == (ONE_XNO, 5)


def test_a_balance_comes_back_as_integers_of_raw_not_as_strings(monkeypatch):
    nodes(monkeypatch, {A: info(ONE_XNO), B: info(ONE_XNO)})
    balance, receivable = balance_corroborated(PAYEE, [A, B])
    assert isinstance(balance, int) and isinstance(receivable, int)
    assert balance == ONE_XNO  # not 1e30, which is 19884624838656 raw short


def test_two_nodes_reporting_different_confirmed_balances_disagree(monkeypatch):
    nodes(monkeypatch, {A: info(ONE_XNO), B: info(ONE_XNO - 1)})
    with pytest.raises(Disagreement):
        balance_corroborated(PAYEE, [A, B])


def test_an_account_neither_node_has_seen_opened_is_a_balance_of_nothing(monkeypatch):
    nodes(monkeypatch, {A: {"error": "Account not found"}, B: {"error": "Account not found"}})
    assert balance_corroborated(PAYEE, [A, B]) == (0, 0)


def test_a_balance_one_endpoint_short_is_not_corroborated(monkeypatch):
    nodes(monkeypatch, {A: OSError("refused"), B: info(ONE_XNO)})
    with pytest.raises(NotCorroborated):
        balance_corroborated(PAYEE, [A, B])


def test_the_default_agreement_is_two(monkeypatch):
    assert AGREE_DEFAULT == 2


def test_a_node_that_ignores_include_confirmed_has_not_answered(monkeypatch):
    """Its `balance` is the UNCONFIRMED figure - the one that must not be trusted.

    Reading it anyway would let two endpoints "agree" on a number neither was
    asked for, which is worse than no answer at all.
    """
    nodes(monkeypatch, {A: {"balance": str(ONE_XNO)}, B: info(ONE_XNO)})
    with pytest.raises(NotCorroborated) as caught:
        balance_corroborated(PAYEE, [A, B])
    assert "KeyError" in caught.value.failures[A]


def test_an_account_not_found_reply_is_zero_even_though_it_carries_a_balance(monkeypatch):
    """A real node answers `{"error": "Account not found", "balance": "0"}`."""
    not_found = {"error": "Account not found", "balance": "0"}
    nodes(monkeypatch, {A: not_found, B: not_found})
    assert balance_corroborated(PAYEE, [A, B]) == (0, 0)
