"""Laws for nano_payers: what a stranger may conclude about who has paid a seller.

No test touches the network: every endpoint is a stub, and the no_sockets
fixture fails any test that tries to open a socket.

The numbers this module produces are a claim about somebody else's business, so
most of these laws are about what it REFUSES to count - an unconfirmed receive,
the payee's own spending, a half-read chain, one account written two ways - and
about the amounts staying exact integers in raw.

Every address here is a real checksum-valid Nano address, because the module
checks checksums; a made-up `nano_1payee` is refused by design.
"""

import json

import pytest

import nano_payers
import nano_quorum
import nano_settlement_verify
from nano_payers import (
    HISTORY_PAGE,
    HistoryIncomplete,
    ManifestRefused,
    Payee,
    PayerReport,
    outside_payers,
    payees_from_manifest,
    payers,
    payers_corroborated,
)

A = "http://127.0.0.1:7071"
B = "http://127.0.0.1:7072"
C = "http://127.0.0.1:7073"

# The live payee of extract.paypercall.dev and three accounts that have paid it.
PAYEE = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"
PAYEE_XRB = "xrb_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"
PAYER_1 = "nano_3gqrm67xjp33gew1pmrbhx8rm6y1yrcogbid36ku5nc9izp8nn3ec5pa48yt"
PAYER_2 = "nano_1995xc97t5dmpdqayx6j8798d8jtz8n14nr6usti3jub7e7z9na1obwwi7xb"
FUNDER = "nano_1xug1q5t7nxoj3ywwzokiea9jz8fq8qfgzp8pbyfr3co3e5xgj755uofu8ue"
# One character of PAYEE changed: a well-formed address whose checksum fails.
PAYEE_BROKEN = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx8"

CALL_PRICE = 100000000000000000000000000  # 0.0001 XNO, in raw


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a socket")

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


def receive(account=PAYER_1, amount=CALL_PRICE, height=3, confirmed="true", when=1790594630,
            block_hash=None):
    """A history row for a receive, as `account_history` serves one."""
    return {
        "type": "receive",
        "account": account,
        "amount": str(amount),
        "height": str(height),
        "confirmed": confirmed,
        "local_timestamp": str(when),
        "hash": block_hash or f"{height:064X}",
    }


def send(height=2, amount=CALL_PRICE, block_hash=None):
    """A history row for a send OUT of the payee's account."""
    return {
        "type": "send",
        "account": PAYER_1,
        "amount": str(amount),
        "height": str(height),
        "confirmed": "true",
        "local_timestamp": "1790594630",
        "hash": block_hash or f"{height:064X}",
    }


def open_block(account=FUNDER, amount=CALL_PRICE, when=1790000000):
    """The receive at height 1 - the block that opened the account."""
    return receive(account=account, amount=amount, height=1, when=when)


def nodes(monkeypatch, replies):
    """Wire each endpoint to a reply, a callable taking the payload, or an exception.

    An endpoint not in the map is one the test did not expect to be asked, and
    asking it fails the test.
    """
    called = []

    def post_json(rpc_url, payload):
        called.append(rpc_url)
        if rpc_url not in replies:
            raise AssertionError(f"unexpected call to {rpc_url}")
        answer = replies[rpc_url]
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(payload)
        return answer

    monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)
    return called


def history(*blocks):
    return {"account": PAYEE, "history": list(blocks)}


# --------------------------------------------------------------------------
# payees_from_manifest - where the seller itself says to pay
# --------------------------------------------------------------------------


CATALOGUE = {
    "x402Version": 2,
    "resources": [
        {
            "url": "https://extract.paypercall.dev/api/v1/extract",
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "nano:mainnet",
                    "asset": "XNO",
                    "amount": str(CALL_PRICE),
                    "payTo": PAYEE,
                }
            ],
        },
        {
            "url": "https://extract.paypercall.dev/api/v1/screenshot",
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "nano:mainnet",
                    "asset": "XNO",
                    "amount": str(5 * CALL_PRICE),
                    "payTo": PAYEE,
                }
            ],
        },
    ],
}


def test_reads_a_catalogue_and_collapses_one_payee_across_its_resources():
    (payee,) = payees_from_manifest(CATALOGUE)
    assert payee.account == PAYEE
    assert payee.network == "nano:mainnet"
    # Both prices are kept, ascending: the cheapest is what one call costs, and
    # that is what makes a payment of exactly that amount recognisable later.
    assert payee.prices_raw == (CALL_PRICE, 5 * CALL_PRICE)
    assert len(payee.resources) == 2


def test_reads_a_single_402_challenge_too():
    challenge = {
        "x402Version": 2,
        "resource": "https://seller.example/thing",
        "accepts": CATALOGUE["resources"][0]["accepts"],
    }
    (payee,) = payees_from_manifest(challenge)
    assert payee.account == PAYEE


def test_one_account_in_two_spellings_is_one_payee():
    doc = {
        "resources": [
            {"url": "a", "accepts": [{"network": "nano:mainnet", "payTo": PAYEE,
                                      "amount": str(CALL_PRICE)}]},
            {"url": "b", "accepts": [{"network": "nano-mainnet", "payTo": PAYEE_XRB,
                                      "amount": str(CALL_PRICE)}]},
        ]
    }
    payees = payees_from_manifest(doc)
    assert len(payees) == 1, "nano_ and xrb_ spell one account; two payees doubles its reach"
    assert payees[0].resources == ("a", "b")


def test_both_mainnet_spellings_are_read():
    for network in ("nano:mainnet", "nano-mainnet"):
        doc = {"accepts": [{"network": network, "payTo": PAYEE, "amount": str(CALL_PRICE)}]}
        assert payees_from_manifest(doc)[0].account == PAYEE


def test_another_rail_beside_ours_is_skipped_not_refused():
    doc = {
        "accepts": [
            {"network": "base-sepolia", "asset": "USDC", "payTo": "0xabc",
             "maxAmountRequired": "1000"},
            {"network": "nano:mainnet", "payTo": PAYEE, "amount": str(CALL_PRICE)},
        ]
    }
    (payee,) = payees_from_manifest(doc)
    assert payee.account == PAYEE


def test_the_older_amount_field_name_is_read():
    doc = {"accepts": [{"network": "nano:mainnet", "payTo": PAYEE,
                        "maxAmountRequired": str(CALL_PRICE)}]}
    assert payees_from_manifest(doc)[0].prices_raw == (CALL_PRICE,)


def test_a_payee_whose_checksum_fails_is_refused():
    doc = {"accepts": [{"network": "nano:mainnet", "payTo": PAYEE_BROKEN,
                        "amount": str(CALL_PRICE)}]}
    with pytest.raises(ManifestRefused) as caught:
        payees_from_manifest(doc)
    assert caught.value.reason == "payee_checksum"


def test_a_bare_nano_network_is_refused_not_assumed_to_be_mainnet():
    for network in ("nano", "xno", "NANO"):
        doc = {"accepts": [{"network": network, "payTo": PAYEE, "amount": str(CALL_PRICE)}]}
        with pytest.raises(ManifestRefused) as caught:
            payees_from_manifest(doc)
        assert caught.value.reason == "network_unspecified"


def test_a_nano_test_network_is_a_different_answer_from_mainnet():
    doc = {"accepts": [{"network": "nano:testnet", "payTo": PAYEE, "amount": str(CALL_PRICE)}]}
    with pytest.raises(ManifestRefused) as caught:
        payees_from_manifest(doc)
    assert caught.value.reason == "not_mainnet"


@pytest.mark.parametrize("amount", ["1e26", "0.0001", "", " 100", "-100", "0x10"])
def test_a_price_that_is_not_raw_is_refused_never_coerced(amount):
    doc = {"accepts": [{"network": "nano:mainnet", "payTo": PAYEE, "amount": amount}]}
    with pytest.raises(ManifestRefused) as caught:
        payees_from_manifest(doc)
    assert caught.value.reason == "amount_not_raw"


def test_a_json_number_price_is_refused_because_it_cannot_carry_raw_exactly():
    # 1e26 as a JSON number round-trips through a float and comes back short of
    # the amount it was meant to be; the low digits of raw are real money.
    doc = {"accepts": [{"network": "nano:mainnet", "payTo": PAYEE, "amount": 1e26}]}
    with pytest.raises(ManifestRefused) as caught:
        payees_from_manifest(doc)
    assert caught.value.reason == "amount_not_raw"


def test_a_document_with_no_nano_entry_says_so():
    doc = {"accepts": [{"network": "base-sepolia", "asset": "USDC", "payTo": "0xabc"}]}
    with pytest.raises(ManifestRefused) as caught:
        payees_from_manifest(doc)
    assert caught.value.reason == "no_nano_payee"


@pytest.mark.parametrize("doc", [None, [], "x402", 7])
def test_something_that_is_not_an_x402_document_is_refused(doc):
    with pytest.raises(ManifestRefused) as caught:
        payees_from_manifest(doc)
    assert caught.value.reason == "not_an_x402_document"


def test_a_document_with_neither_resources_nor_accepts_is_refused():
    with pytest.raises(ManifestRefused) as caught:
        payees_from_manifest({"x402Version": 2, "seller": "someone"})
    assert caught.value.reason == "not_an_x402_document"


def test_a_payee_serialises_its_raw_prices_as_strings():
    (payee,) = payees_from_manifest(CATALOGUE)
    assert json.loads(payee.to_json())["prices_raw"] == [
        str(CALL_PRICE),
        str(5 * CALL_PRICE),
    ]


# --------------------------------------------------------------------------
# payers - who the ledger says has paid
# --------------------------------------------------------------------------


def test_counts_distinct_payers_and_sums_raw_exactly(monkeypatch):
    nodes(monkeypatch, {A: history(
        receive(PAYER_1, CALL_PRICE, height=4, when=1790594630),
        receive(PAYER_1, CALL_PRICE, height=3, when=1790594600),
        receive(PAYER_2, 3 * CALL_PRICE, height=2, when=1790500000),
        open_block(FUNDER, 20 * 10**30),
    )})
    report = payers(PAYEE, [A])
    assert report.distinct_payers == 3
    assert report.payments == 4
    assert report.received_raw == 2 * CALL_PRICE + 3 * CALL_PRICE + 20 * 10**30
    # Ordered by what they paid, so the biggest payer is not buried.
    assert report.payers[0].account == FUNDER
    by_account = {payer.account: payer for payer in report.payers}
    assert by_account[PAYER_1].payments == 2
    assert by_account[PAYER_1].received_raw == 2 * CALL_PRICE
    assert by_account[PAYER_1].first_timestamp == 1790594600
    assert by_account[PAYER_1].last_timestamp == 1790594630


def test_the_payees_own_sends_are_not_income(monkeypatch):
    nodes(monkeypatch, {A: history(
        send(height=3, amount=50 * 10**30),
        open_block(PAYER_1, CALL_PRICE),
    )})
    report = payers(PAYEE, [A])
    assert report.distinct_payers == 1
    assert report.received_raw == CALL_PRICE, "a send on this chain is money leaving"


def test_an_unconfirmed_receive_is_reported_and_never_counted(monkeypatch):
    nodes(monkeypatch, {A: history(
        receive(PAYER_2, 9 * CALL_PRICE, height=3, confirmed="false"),
        open_block(PAYER_1, CALL_PRICE),
    )})
    report = payers(PAYEE, [A])
    assert report.unconfirmed_payments == 1
    assert report.distinct_payers == 1
    assert report.received_raw == CALL_PRICE
    assert PAYER_2 not in [payer.account for payer in report.payers]


@pytest.mark.parametrize("confirmed", [None, "", "unknown", "TRUE", True])
def test_only_true_counts_as_confirmed(monkeypatch, confirmed):
    block = receive(PAYER_2, CALL_PRICE, height=3)
    if confirmed is None:
        del block["confirmed"]
    else:
        block["confirmed"] = confirmed
    nodes(monkeypatch, {A: history(block, open_block(PAYER_1, CALL_PRICE))})
    report = payers(PAYEE, [A])
    counted = confirmed in ("TRUE", True)
    assert report.distinct_payers == (2 if counted else 1)


def test_a_receive_from_the_payee_itself_is_not_a_customer(monkeypatch):
    nodes(monkeypatch, {A: history(
        receive(PAYEE, 7 * CALL_PRICE, height=3),
        open_block(PAYER_1, CALL_PRICE),
    )})
    report = payers(PAYEE, [A])
    assert report.self_payments == 1
    assert report.distinct_payers == 1
    assert report.received_raw == CALL_PRICE


def test_a_payer_in_both_spellings_is_one_payer(monkeypatch):
    other = "xrb_3gqrm67xjp33gew1pmrbhx8rm6y1yrcogbid36ku5nc9izp8nn3ec5pa48yt"
    nodes(monkeypatch, {A: history(
        receive(other, CALL_PRICE, height=2),
        open_block(PAYER_1, CALL_PRICE),
    )})
    report = payers(PAYEE, [A])
    assert report.distinct_payers == 1, "xrb_ and nano_ spell the same customer"
    assert report.payers[0].payments == 2


def test_a_self_receive_in_the_other_spelling_is_still_not_a_customer(monkeypatch):
    nodes(monkeypatch, {A: history(
        receive(PAYEE_XRB, 7 * CALL_PRICE, height=3),
        open_block(PAYER_1, CALL_PRICE),
    )})
    report = payers(PAYEE, [A])
    assert report.self_payments == 1
    assert report.distinct_payers == 1


def test_a_timestamp_of_zero_does_not_become_the_first_payment(monkeypatch):
    nodes(monkeypatch, {A: history(
        receive(PAYER_1, CALL_PRICE, height=3, when=0),
        receive(PAYER_1, CALL_PRICE, height=2, when=1790594600),
        open_block(PAYER_1, CALL_PRICE, when=1790000000),
    )})
    (payer,) = payers(PAYEE, [A]).payers
    assert payer.first_timestamp == 1790000000
    assert payer.last_timestamp == 1790594600


def test_an_unopened_account_has_no_payers_rather_than_a_broken_reply(monkeypatch):
    # A node answers an account it has never seen with history: "".
    nodes(monkeypatch, {A: {"account": PAYEE, "history": ""}})
    report = payers(PAYEE, [A])
    assert report.distinct_payers == 0
    assert report.received_raw == 0
    assert report.complete is True


def test_an_address_whose_checksum_fails_is_refused_before_any_node_is_asked(monkeypatch):
    called = nodes(monkeypatch, {})
    with pytest.raises(ValueError, match="checksum"):
        payers(PAYEE_BROKEN, [A])
    assert called == [], "nothing should be asked about an account that cannot exist"


def test_a_node_error_is_not_read_as_an_empty_chain(monkeypatch):
    nodes(monkeypatch, {A: {"error": "Bad account number"}})
    with pytest.raises(nano_quorum.NotCorroborated):
        payers(PAYEE, [A])


@pytest.mark.parametrize("amount", ["1.5", "1e26", "", "-1", None, 1.5, True])
def test_an_amount_that_is_not_an_integer_refuses_the_whole_report(monkeypatch, amount):
    block = receive(PAYER_1, height=2)
    block["amount"] = amount
    nodes(monkeypatch, {A: history(block, open_block())})
    # The alternative is dropping the row, which makes a total that looks
    # complete and is short by whatever that row was worth.
    with pytest.raises(nano_quorum.NotCorroborated):
        payers(PAYEE, [A])


def test_a_receive_from_an_unparseable_source_refuses_the_report(monkeypatch):
    nodes(monkeypatch, {A: history(receive("nano_notanaccount", height=2), open_block())})
    with pytest.raises(nano_quorum.NotCorroborated):
        payers(PAYEE, [A])


def test_a_reply_that_is_not_a_node_reply_is_refused(monkeypatch):
    nodes(monkeypatch, {A: {"account": PAYEE, "history": {"not": "a list"}}})
    with pytest.raises(nano_quorum.NotCorroborated):
        payers(PAYEE, [A])


def test_one_url_string_instead_of_a_list_is_a_type_error(monkeypatch):
    nodes(monkeypatch, {})
    with pytest.raises(TypeError):
        payers(PAYEE, A)


# --------------------------------------------------------------------------
# a chain longer than one page
# --------------------------------------------------------------------------


def long_chain(total):
    """`total` receives, each from a distinct-looking payer, newest first."""
    payers_cycle = [PAYER_1, PAYER_2, FUNDER]
    return [
        receive(
            payers_cycle[height % 3],
            CALL_PRICE,
            height=height,
            block_hash=f"{height:064X}",
        )
        for height in range(total, 0, -1)
    ]


def test_a_chain_longer_than_one_page_is_walked_to_the_open_block(monkeypatch):
    chain = long_chain(HISTORY_PAGE + 5)
    by_hash = {block["hash"]: index for index, block in enumerate(chain)}

    def serve(payload):
        head = payload.get("head")
        start = by_hash[head] if head else 0
        return {"account": PAYEE, "history": chain[start:start + HISTORY_PAGE]}

    nodes(monkeypatch, {A: serve})
    report = payers(PAYEE, [A])
    assert report.complete is True
    # The block a page starts from is served again; counting it twice would
    # inflate both the payment count and the money.
    assert report.blocks_read == len(chain)
    assert report.payments == len(chain)
    assert report.received_raw == len(chain) * CALL_PRICE


def test_a_half_read_chain_is_refused_rather_than_reported_short(monkeypatch):
    chain = long_chain(HISTORY_PAGE + 5)

    def serve(payload):
        # A node that serves one page and then forgets how to page.
        return {"account": PAYEE, "history": chain[:HISTORY_PAGE]}

    nodes(monkeypatch, {A: serve})
    with pytest.raises(HistoryIncomplete) as caught:
        payers(PAYEE, [A])
    assert caught.value.oldest_height == 6
    # The same read, taken knowingly, is allowed and says it is partial.
    report = payers(PAYEE, [A], allow_partial=True)
    assert report.complete is False
    assert report.payments == HISTORY_PAGE
    assert json.loads(report.to_json())["complete"] is False


def test_allow_partial_must_be_a_bool(monkeypatch):
    nodes(monkeypatch, {})
    with pytest.raises(TypeError):
        payers(PAYEE, [A], allow_partial="yes")


# --------------------------------------------------------------------------
# payers_corroborated - two nodes must name the same customers
# --------------------------------------------------------------------------


def test_two_endpoints_that_name_the_same_payers_corroborate(monkeypatch):
    same = history(receive(PAYER_1, CALL_PRICE, height=2), open_block(PAYER_2, CALL_PRICE))
    nodes(monkeypatch, {A: same, B: same})
    report = payers_corroborated(PAYEE, [A, B])
    assert report.distinct_payers == 2
    assert report.asked == (A, B)


def test_a_node_reporting_a_different_payer_set_is_a_disagreement(monkeypatch):
    nodes(monkeypatch, {
        A: history(receive(PAYER_1, CALL_PRICE, height=2), open_block(PAYER_2, CALL_PRICE)),
        B: history(open_block(PAYER_2, CALL_PRICE)),
    })
    with pytest.raises(nano_quorum.Disagreement) as caught:
        payers_corroborated(PAYEE, [A, B])
    # The accounts each endpoint named, not just how many: two nodes can agree
    # on the count and name different people.
    assert any(PAYER_1 in answer for answer in caught.value.answers.values())


def test_the_same_endpoint_written_twice_is_one_witness(monkeypatch):
    nodes(monkeypatch, {A: history(open_block(PAYER_1, CALL_PRICE))})
    with pytest.raises(ValueError, match="distinct endpoint"):
        payers_corroborated(PAYEE, [A, A + "/"])


def test_too_few_endpoints_answering_is_we_could_not_look(monkeypatch):
    nodes(monkeypatch, {
        A: history(open_block(PAYER_1, CALL_PRICE)),
        B: OSError("refused"),
    })
    with pytest.raises(nano_quorum.NotCorroborated) as caught:
        payers_corroborated(PAYEE, [A, B])
    assert caught.value.answered == 1
    assert B in caught.value.failures


def test_differing_timestamps_alone_are_not_a_disagreement(monkeypatch):
    # local_timestamp is when each node saw a block and legitimately differs.
    nodes(monkeypatch, {
        A: history(open_block(PAYER_1, CALL_PRICE, when=1790000000)),
        B: history(open_block(PAYER_1, CALL_PRICE, when=1790000999)),
    })
    assert payers_corroborated(PAYEE, [A, B]).distinct_payers == 1


# --------------------------------------------------------------------------
# outside_payers - the judgement that stays the caller's
# --------------------------------------------------------------------------


def report_with(monkeypatch):
    nodes(monkeypatch, {A: history(
        receive(PAYER_1, CALL_PRICE, height=3),
        receive(PAYER_2, 3 * CALL_PRICE, height=2),
        open_block(FUNDER, 20 * 10**30),
    )})
    return payers(PAYEE, [A])


def test_our_own_accounts_come_out_of_the_count(monkeypatch):
    report = report_with(monkeypatch)
    outside = outside_payers(report, [FUNDER])
    assert outside.distinct_payers == 2
    assert outside.received_raw == 4 * CALL_PRICE
    assert FUNDER not in [payer.account for payer in outside.payers]
    # What the ledger saw is not rewritten by our bookkeeping.
    assert outside.blocks_read == report.blocks_read


def test_one_of_our_accounts_in_the_other_spelling_still_comes_out(monkeypatch):
    report = report_with(monkeypatch)
    xrb = "xrb_1xug1q5t7nxoj3ywwzokiea9jz8fq8qfgzp8pbyfr3co3e5xgj755uofu8ue"
    assert outside_payers(report, [xrb]).distinct_payers == 2


def test_declaring_no_accounts_changes_nothing_rather_than_guessing(monkeypatch):
    report = report_with(monkeypatch)
    assert outside_payers(report, []).distinct_payers == report.distinct_payers


def test_an_account_we_cannot_name_is_not_one_we_can_exclude(monkeypatch):
    report = report_with(monkeypatch)
    with pytest.raises(ValueError, match="checksum"):
        outside_payers(report, [PAYEE_BROKEN])


def test_one_address_string_instead_of_a_list_is_a_type_error(monkeypatch):
    report = report_with(monkeypatch)
    with pytest.raises(TypeError):
        outside_payers(report, FUNDER)


# --------------------------------------------------------------------------
# the report as a stranger reads it
# --------------------------------------------------------------------------


def test_raw_is_serialised_as_a_string_so_no_reader_floats_it(monkeypatch):
    report = report_with(monkeypatch)
    body = json.loads(report.to_json())
    assert body["received_raw"] == str(report.received_raw)
    assert isinstance(body["payers"][0]["received_raw"], str)
    # 20 XNO in raw has 31 digits; a JSON number here would come back short.
    assert len(body["received_raw"]) == 32


def test_the_report_names_which_endpoint_it_came_from(monkeypatch):
    report = report_with(monkeypatch)
    assert report.asked == (A,)
    assert json.loads(report.to_json())["asked"] == [A]


# --------------------------------------------------------------------------
# failing over is about a USABLE answer, not an HTTP 200
# --------------------------------------------------------------------------


def test_an_endpoint_serving_an_unreadable_reply_is_walked_away_from(monkeypatch):
    called = nodes(monkeypatch, {
        A: {"account": PAYEE, "history": {"not": "a list"}},
        B: history(open_block(PAYER_1, CALL_PRICE)),
    })
    report = payers(PAYEE, [A, B])
    assert report.distinct_payers == 1
    assert report.asked == (B,), "the report must name the node it was actually read from"
    assert called == [A, B]


def test_an_endpoint_whose_amounts_cannot_be_summed_is_walked_away_from(monkeypatch):
    broken = receive(PAYER_1, height=2)
    broken["amount"] = "1.5"
    nodes(monkeypatch, {
        A: history(broken, open_block()),
        B: history(open_block(PAYER_1, CALL_PRICE)),
    })
    # Dropping the row would have reported a seller with one payment fewer and
    # nothing to show that anything was dropped.
    assert payers(PAYEE, [A, B]).asked == (B,)


def test_a_node_with_only_half_the_chain_is_walked_away_from(monkeypatch):
    chain = long_chain(HISTORY_PAGE + 5)
    by_hash = {block["hash"]: index for index, block in enumerate(chain)}

    def whole(payload):
        head = payload.get("head")
        start = by_hash[head] if head else 0
        return {"account": PAYEE, "history": chain[start:start + HISTORY_PAGE]}

    nodes(monkeypatch, {
        A: lambda payload: {"account": PAYEE, "history": chain[:HISTORY_PAGE]},
        B: whole,
    })
    report = payers(PAYEE, [A, B])
    assert report.complete is True
    assert report.blocks_read == len(chain)
    assert report.asked == (B,)


def test_when_every_node_is_short_the_answer_says_short_not_unreachable(monkeypatch):
    chain = long_chain(HISTORY_PAGE + 5)
    half = lambda payload: {"account": PAYEE, "history": chain[:HISTORY_PAGE]}
    nodes(monkeypatch, {A: half, B: half})
    # "I read part of the chain" is a different diagnosis from "nobody
    # answered", and only the first one tells the caller what to do about it.
    with pytest.raises(HistoryIncomplete):
        payers(PAYEE, [A, B])


def test_one_endpoint_written_twice_is_asked_once(monkeypatch):
    called = nodes(monkeypatch, {A: history(open_block(PAYER_1, CALL_PRICE))})
    payers(PAYEE, [A, A])
    assert called == [A], "asking a dead endpoint twice is pure latency"


def test_no_endpoints_at_all_is_refused(monkeypatch):
    nodes(monkeypatch, {})
    with pytest.raises(ValueError, match="no endpoints"):
        payers(PAYEE, [])


def test_a_bad_price_names_the_resource_it_is_on(monkeypatch):
    # A 24-resource catalogue refused for one typo'd price is only useful if the
    # refusal says which resource to go and look at.
    doc = {
        "resources": [
            {"url": "https://seller.example/good",
             "accepts": [{"network": "nano:mainnet", "payTo": PAYEE,
                          "amount": str(CALL_PRICE)}]},
            {"url": "https://seller.example/typo",
             "accepts": [{"network": "nano:mainnet", "payTo": PAYEE, "amount": "0.0001"}]},
        ]
    }
    with pytest.raises(ManifestRefused) as caught:
        payees_from_manifest(doc)
    assert caught.value.reason == "amount_not_raw"
    assert "https://seller.example/typo" in caught.value.detail


def test_a_bad_price_on_an_unnamed_resource_still_refuses_cleanly():
    with pytest.raises(ManifestRefused) as caught:
        payees_from_manifest(
            {"accepts": [{"network": "nano:mainnet", "payTo": PAYEE, "amount": "0.0001"}]}
        )
    assert caught.value.reason == "amount_not_raw"
    assert caught.value.detail.endswith("exponent")


# --------------------------------------------------------------------------
# repeat behaviour - a payer seen once and a payer seen weekly are not alike
# --------------------------------------------------------------------------


def repeat_chain(monkeypatch):
    """PAYER_1 pays three times, PAYER_2 once."""
    nodes(monkeypatch, {A: history(
        receive(PAYER_1, CALL_PRICE, height=4, when=1790594630),
        receive(PAYER_2, CALL_PRICE, height=3, when=1790594620),
        receive(PAYER_1, CALL_PRICE, height=2, when=1790594610),
        open_block(PAYER_1, CALL_PRICE, when=1790000000),
    )})
    return payers(PAYEE, [A])


def test_repeat_payers_and_repeat_rate_come_with_the_distinct_count(monkeypatch):
    report = repeat_chain(monkeypatch)
    assert report.distinct_payers == 2
    by_account = {payer.account: payer for payer in report.payers}
    assert by_account[PAYER_1].payments == 3
    assert by_account[PAYER_2].payments == 1
    assert report.repeat_payers == 1
    assert report.repeat_rate == 0.5
    body = json.loads(report.to_json())
    assert body["repeat_payers"] == 1
    assert body["repeat_rate"] == 0.5
    # The fields that were already there keep their meaning.
    assert body["distinct_payers"] == 2
    assert body["payments"] == 4


def test_removing_our_own_repeat_payer_recomputes_the_repeat_rate(monkeypatch):
    report = repeat_chain(monkeypatch)
    outside = outside_payers(report, [PAYER_1])
    assert outside.distinct_payers == 1
    assert outside.repeat_payers == 0
    assert outside.repeat_rate == 0
    assert json.loads(outside.to_json())["repeat_rate"] == 0


def test_no_payers_is_a_repeat_rate_of_zero_not_a_division_error(monkeypatch):
    nodes(monkeypatch, {A: {"account": PAYEE, "history": ""}})
    report = payers(PAYEE, [A])
    assert report.distinct_payers == 0
    assert report.repeat_payers == 0
    assert report.repeat_rate == 0
    assert json.loads(report.to_json())["repeat_rate"] == 0


def test_repeat_rate_is_rounded_to_four_places(monkeypatch):
    nodes(monkeypatch, {A: history(
        receive(PAYER_1, CALL_PRICE, height=3),
        receive(PAYER_2, CALL_PRICE, height=2),
        open_block(PAYER_1, CALL_PRICE),
        receive(FUNDER, CALL_PRICE, height=4),
    )})
    report = payers(PAYEE, [A])
    assert (report.distinct_payers, report.repeat_payers) == (3, 1)
    assert report.repeat_rate == 0.3333


def test_a_corroborated_report_carries_the_repeat_rate(monkeypatch):
    same = history(
        receive(PAYER_1, CALL_PRICE, height=3),
        receive(PAYER_1, CALL_PRICE, height=2),
        open_block(PAYER_2, CALL_PRICE),
    )
    nodes(monkeypatch, {A: same, B: same})
    report = payers_corroborated(PAYEE, [A, B])
    assert (report.distinct_payers, report.repeat_payers, report.repeat_rate) == (2, 1, 0.5)
