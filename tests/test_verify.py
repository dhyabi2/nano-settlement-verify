"""The four acceptance tests, plus the error paths around them.

No test touches the network: the node reply is stubbed, and the no_sockets
fixture makes any real urlopen call fail the test that made it.
"""

import json

import pytest

import nano_settlement_verify
from nano_settlement_verify import Mismatch, NotFound, Receipt, verify

HASH = "B2EC1E2A1F2C3D4E5F60718293A4B5C6D7E8F9012345678901234567890ABCDE"
ACCOUNT = "nano_3abc"
URL = "http://127.0.0.1:7076"


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    """Any test that reaches the network fails instead of going out."""

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a socket")

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


@pytest.fixture
def node(monkeypatch):
    """Stub the node: hand it a reply, get back the requests it was asked."""
    calls = []

    def stub(reply):
        def post_json(rpc_url, payload):
            calls.append((rpc_url, payload))
            return reply

        monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)
        return calls

    return stub


def block_info(amount="1000000000000000000000000", confirmed="true", account=ACCOUNT, height="42"):
    """A node's block_info reply, in the shape the real node sends it."""
    return {"block_account": account, "amount": amount, "confirmed": confirmed, "height": height}


# --- the four acceptance tests -------------------------------------------------


def test_confirmed_block_settles(node):
    node(block_info(amount=str(10**24)))

    receipt = verify(HASH, 10**24, ACCOUNT, URL)

    assert receipt == Receipt(settled=True, amount_raw=10**24, height=42, account=ACCOUNT)


def test_unconfirmed_block_is_not_settled(node):
    node(block_info(amount=str(10**24), confirmed="false"))

    receipt = verify(HASH, 10**24, ACCOUNT, URL)

    assert receipt == Receipt(settled=False, amount_raw=0, height=0, account="")


def test_missing_block_raises_not_found(node):
    node({"error": "Block not found"})

    with pytest.raises(NotFound) as caught:
        verify(HASH, 1, ACCOUNT, URL)

    assert caught.value.block_hash == HASH
    assert HASH in str(caught.value)


def test_wrong_amount_raises_mismatch(node):
    node(block_info(amount=str(10**23)))

    with pytest.raises(Mismatch) as caught:
        verify(HASH, 10**24, ACCOUNT, URL)

    assert caught.value.got == 10**23
    assert caught.value.expected == 10**24


# --- error paths ---------------------------------------------------------------


def test_wrong_account_raises_mismatch(node):
    node(block_info(amount=str(10**24), account="nano_3zzz"))

    with pytest.raises(Mismatch) as caught:
        verify(HASH, 10**24, ACCOUNT, URL)

    assert caught.value.got == "nano_3zzz"
    assert caught.value.expected == ACCOUNT


def test_amount_is_checked_before_the_account(node):
    """Both are wrong: the amount is the one reported, per the workflow's order."""
    node(block_info(amount=str(10**23), account="nano_3zzz"))

    with pytest.raises(Mismatch) as caught:
        verify(HASH, 10**24, ACCOUNT, URL)

    assert caught.value.got == 10**23


def test_any_node_error_raises_not_found(node):
    node({"error": "Bad block hash"})

    with pytest.raises(NotFound):
        verify(HASH, 10**24, ACCOUNT, URL)


def test_an_error_reply_is_not_read_for_an_amount(node):
    """A node that sends an error and nothing else must not trip a KeyError."""
    node({"error": "Block not found"})

    with pytest.raises(NotFound):
        verify(HASH, 10**24, ACCOUNT, URL)


def test_an_unconfirmed_reply_never_looks_at_the_amount(node):
    """Unconfirmed short-circuits: a reply with no amount at all still returns."""
    node({"confirmed": "false"})

    assert verify(HASH, 10**24, ACCOUNT, URL) == Receipt(False, 0, 0, "")


def test_a_reply_with_no_confirmed_field_raises_key_error(node):
    """A malformed reply is not quietly treated as settled or as unsettled."""
    node({"amount": "1", "block_account": ACCOUNT, "height": "1"})

    with pytest.raises(KeyError):
        verify(HASH, 10**24, ACCOUNT, URL)


def test_a_non_integer_amount_raises_value_error(node):
    node(block_info(amount="1.5"))

    with pytest.raises(ValueError):
        verify(HASH, 10**24, ACCOUNT, URL)


# --- raw is integer arithmetic, never float ------------------------------------


def test_one_xno_keeps_every_digit(node):
    one_xno = 10**30
    node(block_info(amount=str(one_xno), height="7"))

    receipt = verify(HASH, one_xno, ACCOUNT, URL)

    assert receipt.amount_raw == one_xno
    assert str(receipt.amount_raw) == "1" + "0" * 30


def test_a_float_would_have_lost_the_low_digits(node):
    """Guards the rule the workflow states: parse raw with int(), never float()."""
    awkward = 10**30 + 1
    node(block_info(amount=str(awkward)))

    receipt = verify(HASH, awkward, ACCOUNT, URL)

    assert receipt.amount_raw == awkward
    assert int(float(awkward)) != awkward  # the mistake this test exists to forbid


# --- the receipt ---------------------------------------------------------------


def test_receipt_to_json_matches_the_documented_shape():
    receipt = Receipt(settled=True, amount_raw=10**24, height=42, account=ACCOUNT)

    assert receipt.to_json() == (
        '{"settled": true, "amount_raw": 1000000000000000000000000, '
        '"height": 42, "account": "nano_3abc"}'
    )


def test_receipt_json_carries_raw_as_an_unquoted_integer():
    receipt = Receipt(settled=True, amount_raw=10**30 + 1, height=1, account=ACCOUNT)

    loaded = json.loads(receipt.to_json())

    assert loaded["amount_raw"] == 10**30 + 1
    assert isinstance(loaded["amount_raw"], int)


def test_an_unsettled_receipt_serialises_too():
    assert json.loads(Receipt(False, 0, 0, "").to_json()) == {
        "settled": False,
        "amount_raw": 0,
        "height": 0,
        "account": "",
    }


def test_height_comes_back_as_an_integer(node):
    node(block_info(amount=str(10**24), height="123456"))

    assert verify(HASH, 10**24, ACCOUNT, URL).height == 123456


# --- the request put to the node ----------------------------------------------


def test_the_node_is_asked_for_block_info(node):
    calls = node(block_info(amount=str(10**24)))

    verify(HASH, 10**24, ACCOUNT, URL)

    assert calls == [(URL, {"action": "block_info", "json_block": "true", "hash": HASH})]


def test_verify_asks_the_node_exactly_once(node):
    calls = node(block_info(amount=str(10**24)))

    verify(HASH, 10**24, ACCOUNT, URL)

    assert len(calls) == 1


def test_post_json_posts_the_payload_as_json(monkeypatch):
    """post_json's own request shape, with urlopen captured rather than called."""
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"confirmed": "true"}'

    def fake_urlopen(request, *args, **kwargs):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["body"] = request.data
        seen["content_type"] = request.get_header("Content-type")
        return FakeResponse()

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", fake_urlopen)

    reply = nano_settlement_verify.post_json(URL, {"action": "block_info", "hash": HASH})

    assert reply == {"confirmed": "true"}
    assert seen["url"] == URL
    assert seen["method"] == "POST"
    assert seen["content_type"] == "application/json"
    assert json.loads(seen["body"]) == {"action": "block_info", "hash": HASH}


def test_the_no_sockets_guard_really_bites():
    """Proves the fixture above would catch a test that went to the network."""
    with pytest.raises(AssertionError, match="tried to open a socket"):
        nano_settlement_verify.post_json(URL, {"action": "block_info"})
