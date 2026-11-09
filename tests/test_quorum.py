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


# --- which node said which, and when ---------------------------------------------


def ticking_clock(*moments):
    """A clock for `verify_corroborated(clock=...)`: one fixed UTC moment per read."""
    from datetime import datetime, timezone

    queue = [datetime(2026, 11, 9, 12, 0, s, tzinfo=timezone.utc) for s in moments]
    return lambda: queue.pop(0)


def test_an_unsettled_receipt_keeps_not_seen_apart_from_unconfirmed(monkeypatch):
    """A has no such block, B has it but not confirmed. Both leave the receipt
    unsettled, but they are different facts, and the record names who said which
    and when rather than folding them into one "not settled"."""
    nodes(monkeypatch, {A: {"error": "Block not found"}, B: send_block(confirmed="false")})
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B], clock=ticking_clock(1, 2))
    assert out.receipt.settled is False
    assert [(o.rpc_url, o.state, o.read_at) for o in out.observations] == [
        (A, "not-seen", "2026-11-09T12:00:01.000Z"),
        (B, "unconfirmed", "2026-11-09T12:00:02.000Z"),
    ]


def test_an_unreadable_node_is_listed_beside_the_two_that_settle(monkeypatch):
    nodes(monkeypatch, {A: OSError("timed out"), B: send_block(), C: send_block()})
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C], clock=ticking_clock(1, 2, 3))
    assert out.receipt.settled is True
    assert out.agreed == (B, C)
    assert [(o.rpc_url, o.state) for o in out.observations] == [
        (A, "unreadable"), (B, "confirmed"), (C, "confirmed"),
    ]
    assert "timed out" in out.observations[0].detail
    assert out.observations[0].read_at == "2026-11-09T12:00:01.000Z"


def test_the_json_record_carries_each_node_its_state_and_read_time(monkeypatch):
    nodes(monkeypatch, {A: {"error": "Block not found"}, B: send_block(confirmed="false")})
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B], clock=ticking_clock(1, 2))
    record = json.loads(out.to_json())
    assert record["settled"] is False
    assert record["observations"] == [o.to_dict() for o in out.observations]
    assert record["observations"][0] == {
        "rpc_url": A, "state": "not-seen", "read_at": "2026-11-09T12:00:01.000Z", "detail": "",
    }
    assert record["observations"][1]["state"] == "unconfirmed"


def test_not_corroborated_names_the_node_that_answered_and_the_one_that_did_not(monkeypatch):
    nodes(monkeypatch, {A: OSError("refused"), B: send_block()})
    with pytest.raises(NotCorroborated) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B], clock=ticking_clock(1, 2))
    assert [(o.rpc_url, o.state) for o in caught.value.observations] == [
        (A, "unreadable"), (B, "confirmed"),
    ]


def test_read_at_without_a_clock_is_utc_iso_8601(monkeypatch):
    from datetime import datetime

    nodes(monkeypatch, {A: send_block(confirmed="false"), B: send_block(confirmed="false")})
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    for seen in out.observations:
        assert seen.read_at.endswith("Z")
        assert datetime.fromisoformat(seen.read_at.replace("Z", "+00:00")).utcoffset().total_seconds() == 0


# --- the state vocabulary is pinned in the record itself ------------------------------


def test_the_json_record_pins_the_node_state_vocabulary_and_its_schema(monkeypatch):
    """A stored receipt must say what its states mean without the code beside it:
    above all that "not-seen" is an answer and "unreadable" is the lack of one."""
    nodes(monkeypatch, {A: {"error": "Block not found"}, B: send_block(confirmed="false")})
    record = json.loads(verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B]).to_json())
    assert record["schema"] == nano_quorum.RECEIPT_SCHEMA
    assert record["node_states"] == nano_quorum.NODE_STATES
    assert list(record["node_states"]) == [
        "confirmed", "unconfirmed", "not-seen", "unreadable", "mismatch",
    ]
    assert "answered" in record["node_states"]["not-seen"]
    assert "no answer" in record["node_states"]["unreadable"]
    assert record["node_states"]["not-seen"] != record["node_states"]["unreadable"]


def test_the_json_record_carries_the_hash_of_its_node_state_table(monkeypatch):
    """The record pins its own vocabulary by hash, and a stranger can recompute
    it from the record alone with the documented canonical form."""
    import hashlib

    nodes(monkeypatch, {A: {"error": "Block not found"}, B: send_block(confirmed="false")})
    text = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B]).to_json()
    record = json.loads(text)
    canonical = json.dumps(
        record["node_states"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert record["node_states_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert record["node_states_sha256"] == nano_quorum.NODE_STATES_SHA256
    assert nano_quorum.node_states_sha256(record["node_states"]) == record["node_states_sha256"]
    assert nano_quorum.verify_receipt_vocabulary(text) is True
    assert nano_quorum.verify_receipt_vocabulary(record) is True


def test_editing_one_meaning_in_a_stored_record_is_detected(monkeypatch):
    """Rewording a single state in a stored record, with or without re-hashing
    it, no longer matches the vocabulary the schema string names."""
    nodes(monkeypatch, {A: {"error": "Block not found"}, B: send_block(confirmed="false")})
    record = json.loads(verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B]).to_json())

    edited = json.loads(json.dumps(record))
    edited["node_states"]["not-seen"] = "the node gave no answer"
    assert nano_quorum.verify_receipt_vocabulary(edited) is False

    rehashed = json.loads(json.dumps(edited))
    rehashed["node_states_sha256"] = nano_quorum.node_states_sha256(rehashed["node_states"])
    assert nano_quorum.verify_receipt_vocabulary(rehashed) is False

    missing = json.loads(json.dumps(record))
    del missing["node_states_sha256"]
    assert nano_quorum.verify_receipt_vocabulary(missing) is False

    other_schema = json.loads(json.dumps(record))
    other_schema["schema"] = "nano_quorum.corroboration/1"
    assert nano_quorum.verify_receipt_vocabulary(other_schema) is False


def test_a_vocabulary_that_disagrees_with_the_pinned_hash_is_refused():
    """Changing NODE_STATES without bumping the schema must fail loudly, and the
    pinned constant is the hash of the shipped table."""
    assert nano_quorum.node_states_sha256(nano_quorum.NODE_STATES) == nano_quorum.NODE_STATES_SHA256
    nano_quorum._check_vocabulary_pin(nano_quorum.NODE_STATES, nano_quorum.NODE_STATES_SHA256)
    changed = dict(nano_quorum.NODE_STATES)
    changed["unreadable"] = "the node said nothing"
    with pytest.raises(AssertionError, match="RECEIPT_SCHEMA"):
        nano_quorum._check_vocabulary_pin(changed, nano_quorum.NODE_STATES_SHA256)


def test_an_unreadable_entry_carries_its_error_and_a_not_seen_entry_does_not(monkeypatch):
    """A node that errored is a different fact from a node that answered empty,
    and the record shows it: the errored one names its error, the other has none."""
    nodes(monkeypatch, {
        A: OSError("timed out"),
        B: {"error": "Block not found"},
        C: send_block(confirmed="false"),
    })
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C], agree=2)
    assert out.receipt.settled is False
    unreadable, not_seen, unconfirmed = json.loads(out.to_json())["observations"]
    assert unreadable["state"] == "unreadable"
    assert unreadable["error"] == {"kind": "OSError", "message": "timed out"}
    assert not_seen["state"] == "not-seen"
    assert "error" not in not_seen
    assert unconfirmed["state"] == "unconfirmed"
    assert "error" not in unconfirmed


def test_every_state_written_is_in_the_pinned_vocabulary(monkeypatch):
    """Whatever path ends the call - settled, unsettled, NotCorroborated,
    Disagreement, Mismatch - each observation's state is a NODE_STATES key."""
    seen = []

    nodes(monkeypatch, {A: OSError("refused"), B: send_block(), C: send_block()})
    seen += verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C]).observations

    nodes(monkeypatch, {A: {"error": "Block not found"}, B: send_block(confirmed="false")})
    seen += verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B]).observations

    nodes(monkeypatch, {A: OSError("refused"), B: send_block()})
    with pytest.raises(NotCorroborated) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    seen += caught.value.observations

    nodes(monkeypatch, {A: send_block(height=7), B: send_block(height=8)})
    with pytest.raises(Disagreement) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    seen += caught.value.observations

    nodes(monkeypatch, {A: send_block(amount=ONE_XNO - 1)})
    with pytest.raises(Mismatch) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])
    seen += caught.value.observations

    states = {o.state for o in seen}
    assert states == set(nano_quorum.NODE_STATES)
    for o in seen:
        assert (o.state == "unreadable") == ("error" in o.to_dict())


def test_an_observation_outside_the_vocabulary_or_an_errorless_unreadable_is_refused():
    at = "2026-11-09T12:00:00.000Z"
    with pytest.raises(ValueError, match="unknown node state"):
        nano_quorum.Observation(A, "empty", at)
    with pytest.raises(ValueError, match="must say what the error was"):
        nano_quorum.Observation(A, "unreadable", at)
    with pytest.raises(ValueError, match="carries no error"):
        nano_quorum.Observation(A, "not-seen", at, error_kind="OSError")


# --- a confirmed answer about a DIFFERENT block is a refusal, not agreement -----------


def state_send(previous="11" * 32, payer_key=bytes(range(1, 33)), height=7, block_account=None):
    """A real node's block_info reply for a confirmed state send of ONE_XNO to HOLDER.

    Its contents are complete, so `nano_five_fields.block_hash_of` recomputes the
    block's hash from them. Returns (reply, the hash its contents hash to).
    """
    from nano_five_fields import _address_of_key, block_hash_of

    payer = _address_of_key(payer_key)
    contents = {
        "type": "state",
        "account": payer,
        "previous": previous,
        "representative": payer,
        "balance": str(5 * ONE_XNO),
        "link": nano_settlement_verify.public_key_from_address(HOLDER).hex().upper(),
        "link_as_account": HOLDER,
    }
    reply = {
        "block_account": block_account or payer,
        "amount": str(ONE_XNO),
        "height": str(height),
        "confirmed": "true",
        "subtype": "send",
        "contents": contents,
    }
    return reply, block_hash_of(contents)


def test_two_nodes_confirming_full_contents_of_the_requested_block_settle_it(monkeypatch):
    """The control for the laws below: honest nodes with complete contents still agree."""
    reply, asked = state_send()
    nodes(monkeypatch, {A: reply, B: reply})
    out = verify_corroborated(asked, ONE_XNO, HOLDER, [A, B])
    assert out.receipt.settled is True
    assert out.agreed == (A, B)


def test_a_confirmed_answer_whose_contents_hash_to_another_block_is_a_disagreement(monkeypatch):
    """dynamo's block-hash-mismatch test. A and B both answer `confirmed`, with the
    same amount, payee and height, but B's contents are another block (a different
    `previous`), so they hash to something other than the hash asked for. Amount,
    payee and height alone would count that as a second witness and settle; it
    must refuse (case 3) instead, and say which hashes it saw."""
    right, asked = state_send(previous="11" * 32)
    wrong, other = state_send(previous="22" * 32)
    assert other != asked
    nodes(monkeypatch, {A: right, B: wrong})
    with pytest.raises(Disagreement) as caught:
        verify_corroborated(asked, ONE_XNO, HOLDER, [A, B], clock=ticking_clock(1, 2))
    assert caught.value.answers[B] == f"contents that hash to {other}"
    assert asked in caught.value.answers["requested"]
    assert [(o.rpc_url, o.state) for o in caught.value.observations] == [
        (A, "confirmed"), (B, "mismatch"),
    ]
    assert other in caught.value.observations[1].detail


def test_two_nodes_agreeing_about_another_block_do_not_settle_the_one_asked_for(monkeypatch):
    """Both nodes describe the same block - just not the requested one. Their
    agreement is about the wrong block, so the first such answer refuses and the
    second node is not asked."""
    wrong, other = state_send(previous="22" * 32)
    asked = "33" * 32
    called = nodes(monkeypatch, {A: wrong, B: wrong})
    with pytest.raises(Disagreement):
        verify_corroborated(asked, ONE_XNO, HOLDER, [A, B])
    assert called == [A]


def test_the_requested_hash_is_compared_whatever_its_letter_case(monkeypatch):
    reply, asked = state_send()
    nodes(monkeypatch, {A: reply, B: reply})
    assert verify_corroborated(asked.lower(), ONE_XNO, HOLDER, [A, B]).receipt.settled is True


def test_two_confirmed_sends_on_different_payer_chains_disagree(monkeypatch):
    """Same amount, payee and height, but the nodes name different chains the block
    sits on: two different blocks, so not one payment seen twice."""
    from nano_five_fields import _address_of_key

    one = send_block()
    one["block_account"] = _address_of_key(bytes(range(1, 33)))
    two = send_block()
    two["block_account"] = _address_of_key(bytes(range(2, 34)))
    nodes(monkeypatch, {A: one, B: two})
    with pytest.raises(Disagreement):
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B])


def test_both_spellings_of_one_payer_chain_still_agree(monkeypatch):
    from nano_five_fields import _address_of_key

    payer = _address_of_key(bytes(range(1, 33)))
    one = send_block()
    one["block_account"] = payer
    two = send_block()
    two["block_account"] = "xrb_" + payer[len("nano_"):]
    nodes(monkeypatch, {A: one, B: two})
    assert verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B]).receipt.settled is True


# --- two "wait" incidents a postmortem can tell apart ------------------------------------


D = "http://127.0.0.1:7074"


def test_three_nodes_behind_and_two_nodes_unreachable_are_different_records(monkeypatch):
    """Both incidents leave the payment unsettled ("wait"), and both receipts are
    the same empty Receipt. What tells them apart is the per-node state, which
    each record carries in `observations` and in to_json: three nodes that
    ANSWERED and were behind, against two that gave no answer at all."""
    nodes(monkeypatch, {
        A: {"error": "Block not found"},
        B: send_block(confirmed="false"),
        C: {"error": "Block not found"},
    })
    behind = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C], agree=3,
                                 clock=ticking_clock(1, 2, 3))
    nodes(monkeypatch, {
        A: OSError("connection refused"),
        B: OSError("timed out"),
        C: send_block(confirmed="false"),
        D: send_block(confirmed="false"),
    })
    unreachable = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C, D],
                                      clock=ticking_clock(1, 2, 3, 4))

    assert behind.receipt == unreachable.receipt
    assert behind.receipt.settled is False
    assert behind != unreachable
    assert [o.state for o in behind.observations] == ["not-seen", "unconfirmed", "not-seen"]
    assert [o.state for o in unreachable.observations] == [
        "unreadable", "unreadable", "unconfirmed", "unconfirmed",
    ]
    assert behind.failed == ()
    assert unreachable.failed == (A, B)

    behind_json = json.loads(behind.to_json())
    unreachable_json = json.loads(unreachable.to_json())
    assert [o["state"] for o in behind_json["observations"]] == ["not-seen", "unconfirmed", "not-seen"]
    assert all("error" not in o for o in behind_json["observations"])
    assert [o["state"] for o in unreachable_json["observations"]] == [
        "unreadable", "unreadable", "unconfirmed", "unconfirmed",
    ]
    assert [o["error"] for o in unreachable_json["observations"][:2]] == [
        {"kind": "OSError", "message": "connection refused"},
        {"kind": "OSError", "message": "timed out"},
    ]


def test_two_unreachable_of_three_is_not_corroborated_and_still_names_each_node(monkeypatch):
    """When the two unreachable nodes leave too few answers, the outcome is
    NotCorroborated rather than an unsettled receipt, and it still says which
    node was unreachable and which answered."""
    nodes(monkeypatch, {A: OSError("refused"), B: OSError("timed out"),
                        C: send_block(confirmed="false")})
    with pytest.raises(NotCorroborated) as caught:
        verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B, C], clock=ticking_clock(1, 2, 3))
    assert [(o.rpc_url, o.state) for o in caught.value.observations] == [
        (A, "unreadable"), (B, "unreadable"), (C, "unconfirmed"),
    ]


# --- equality ignores when an answer was read; the record does not ---------------------


def test_two_identical_calls_a_second_apart_are_equal_and_hash_alike(monkeypatch):
    nodes(monkeypatch, {A: {"error": "Block not found"}, B: send_block(confirmed="false")})
    first = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B], clock=ticking_clock(1, 2))
    second = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B], clock=ticking_clock(3, 4))
    assert first == second
    assert hash(first) == hash(second)
    assert len({first, second}) == 1
    assert first.to_json() != second.to_json()  # the record still says when each was read


def test_calls_whose_nodes_said_different_things_are_not_equal(monkeypatch):
    nodes(monkeypatch, {A: {"error": "Block not found"}, B: send_block(confirmed="false")})
    first = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B], clock=ticking_clock(1, 2))
    nodes(monkeypatch, {A: send_block(confirmed="false"), B: send_block(confirmed="false")})
    second = verify_corroborated(HASH, ONE_XNO, PAYEE, [A, B], clock=ticking_clock(1, 2))
    assert first != second
    assert first != "not a Corroboration"


# --- credentials in an endpoint URL are not written into the record ------------------


def test_credentials_in_an_endpoint_url_are_redacted_in_the_json_record(monkeypatch):
    keyed = "http://agent:s3cret@127.0.0.1:7071"
    nodes(monkeypatch, {keyed: send_block(), B: send_block()})
    out = verify_corroborated(HASH, ONE_XNO, PAYEE, [keyed, B])
    assert out.agreed == (keyed, B)  # in memory, the URL is kept exactly as given
    text = out.to_json()
    assert "s3cret" not in text and "agent" not in text
    record = json.loads(text)
    assert record["agreed"] == ["http://***@127.0.0.1:7071", B]
    assert record["observations"][0]["rpc_url"] == "http://***@127.0.0.1:7071"


def test_redact_url_leaves_a_plain_url_alone_and_handles_an_at_in_the_password():
    assert nano_quorum.redact_url(A) == A
    assert nano_quorum.redact_url("https://u:p@ss@node.example/rpc?x=1") == "https://***@node.example/rpc?x=1"
    assert nano_quorum.redact_url("https://node.example/a@b") == "https://node.example/a@b"
