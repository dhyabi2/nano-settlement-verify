"""Laws for nano_terms: pay against terms both sides hold, not against a claim.

The terms are addressed by the sha256 of their own bytes, so each side checks a
cited hash locally with no registry. Acceptance is decided by a machine check
written into the terms before anything is paid, and settlement is checked
against the payee and amount the terms pinned - never against what the seller
says afterwards. No test touches the network.
"""

import hashlib
import json

import pytest

import nano_settlement_verify
import nano_terms
from nano_settlement_verify import Mismatch
from nano_terms import NotAccepted, TermsRefused, accept, load_terms, settle, terms_sha256

HASH = "B2EC1E2A1F2C3D4E5F60718293A4B5C6D7E8F9012345678901234567890ABCDE"
# Real, checksum-valid Nano addresses, not placeholders. `load_terms` refuses a
# payee whose checksum does not match, because XNO sent to a mistyped address
# does not arrive and cannot be recalled - so a fixture payee has to be payable.
# `test_the_fixture_addresses_are_payable` below holds them to that, so a later
# edit cannot quietly reintroduce a placeholder and make this file's refusals
# pass for the wrong reason.
PAYEE = "nano_3wxkjcz9bby6bo8uw4ow8dtmhsq8ffj7th9kxfq16ribhhkgxam4kbbf6ww3"
BUYER = "nano_3q4ki69hmaat7gts8kdufcspb31nm6thg4ryiomicgj8dxr4z8qcp4yh9s1z"
ANOTHER_ACCOUNT = "nano_3pmhddz8g4gxhcqwxw68paibs89wb1o7bqgc6twyfkz1a37puf88pa9stsp8"
URL = "http://127.0.0.1:7076"
DELIVERABLE = b'{"answer": 42, "source": "https://example.org/a"}'


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a socket")

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


def node(monkeypatch, amount="1000000000000000000000000", paid=PAYEE, confirmed="true"):
    calls = []

    def post_json(rpc_url, payload):
        calls.append(payload)
        return {
            "block_account": BUYER,
            "amount": amount,
            "confirmed": confirmed,
            "height": "7",
            "subtype": "send",
            "contents": {"type": "state", "account": BUYER, "link_as_account": paid},
        }

    monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)
    return calls


def terms_bytes(**override):
    terms = {
        "version": "1",
        "payee": PAYEE,
        "amount_raw": "1000000000000000000000000",
        "task": "answer one question with a source",
        "acceptance": {"sha256": hashlib.sha256(DELIVERABLE).hexdigest()},
    }
    terms.update(override)
    return json.dumps(terms, sort_keys=True).encode("utf-8")


# --- the hash is of the bytes, and it is checked locally ---------------------------

def test_hash_is_sha256_of_the_exact_bytes():
    raw = terms_bytes()
    assert terms_sha256(raw) == hashlib.sha256(raw).hexdigest()


def test_cited_hash_that_does_not_match_the_bytes_is_refused():
    raw = terms_bytes()
    with pytest.raises(TermsRefused) as err:
        load_terms(raw, "0" * 64)
    assert err.value.code == "hash_mismatch"


def test_one_changed_byte_changes_the_hash_and_is_refused():
    raw = terms_bytes()
    cited = terms_sha256(raw)
    tampered = raw.replace(b'"1000000000000000000000000"', b'"1000000000000000000000001"')
    with pytest.raises(TermsRefused):
        load_terms(tampered, cited)


def test_cited_hash_is_compared_case_insensitively():
    raw = terms_bytes()
    assert load_terms(raw, terms_sha256(raw).upper()).payee == PAYEE


# --- the schema is pinned: nothing extra, nothing missing, nothing loose ---------------

@pytest.mark.parametrize("field", ["version", "payee", "amount_raw", "task", "acceptance"])
def test_a_missing_field_is_refused(field):
    terms = json.loads(terms_bytes())
    del terms[field]
    raw = json.dumps(terms).encode()
    with pytest.raises(TermsRefused):
        load_terms(raw, terms_sha256(raw))


def test_an_unknown_field_is_refused():
    raw = terms_bytes(refund_if_late="yes")
    with pytest.raises(TermsRefused) as err:
        load_terms(raw, terms_sha256(raw))
    assert err.value.code == "unknown_field"


@pytest.mark.parametrize("amount", [1000000, "1e24", "-5", "0", "", " 12"])
def test_amount_must_be_a_positive_decimal_string_of_raw(amount):
    raw = terms_bytes(amount_raw=amount)
    with pytest.raises(TermsRefused):
        load_terms(raw, terms_sha256(raw))


def test_amount_keeps_every_raw_digit():
    raw = terms_bytes(amount_raw="1000000000000000000000000000001")
    assert load_terms(raw, terms_sha256(raw)).amount_raw == 10**30 + 1


@pytest.mark.parametrize("payee", ["", "0xabc", "nano", 7])
def test_payee_must_be_a_nano_address(payee):
    raw = terms_bytes(payee=payee)
    with pytest.raises(TermsRefused):
        load_terms(raw, terms_sha256(raw))


def test_the_fixture_addresses_are_payable():
    """Every address this file pins is one XNO could actually be sent to.

    The payee checks below are only meaningful if the accepted payee is itself
    payable; a placeholder would make them pass for the wrong reason.
    """
    for name, address in (("PAYEE", PAYEE), ("BUYER", BUYER),
                          ("ANOTHER_ACCOUNT", ANOTHER_ACCOUNT)):
        assert nano_settlement_verify.is_valid_account(address), name


@pytest.mark.parametrize(
    "payee",
    [
        # The prefix was the whole check, so each of these was accepted as the
        # pinned payee of terms both sides hash - and then paid, irreversibly.
        "nano_1a",
        "nano_3abc",
        "nano_1",
        # right length, right alphabet, one character off: the case a checksum
        # exists for, and the one a human eye does not catch.
        PAYEE[:-1] + ("4" if PAYEE[-1] != "4" else "5"),
        # right length, but a character Nano's base32 alphabet does not have
        # (`0`, `2`, `l` and `v` are left out because they misread by hand).
        "nano_0" + PAYEE[6:],
        # the 60 characters of a valid address, truncated by one
        PAYEE[:-1],
        # and one character too many
        PAYEE + "1",
    ],
)
def test_a_payee_that_cannot_be_paid_is_refused(payee):
    raw = terms_bytes(payee=payee)
    with pytest.raises(TermsRefused) as caught:
        load_terms(raw, terms_sha256(raw))
    assert caught.value.code == "invalid_payee", caught.value.code


def test_both_spellings_of_one_payee_are_accepted():
    """A node answers `nano_`; older tooling stores `xrb_`. They are one
    account, and the checksum check must not refuse the legacy spelling."""
    legacy = "xrb_" + PAYEE[len("nano_"):]
    raw = terms_bytes(payee=legacy)
    assert load_terms(raw, terms_sha256(raw)).payee == legacy


def test_an_unknown_version_is_refused():
    raw = terms_bytes(version="2")
    with pytest.raises(TermsRefused):
        load_terms(raw, terms_sha256(raw))


@pytest.mark.parametrize(
    "acceptance",
    [{}, {"judge": "me"}, {"sha256": "abc"}, {"sha256": "g" * 64}, {"sha256": "a" * 64, "json_keys": ["x"]},
     {"json_keys": []}, {"json_keys": "answer"}, {"json_keys": [1]}, "anything"],
)
def test_acceptance_must_be_one_machine_check(acceptance):
    raw = terms_bytes(acceptance=acceptance)
    with pytest.raises(TermsRefused):
        load_terms(raw, terms_sha256(raw))


def test_terms_that_are_not_json_are_refused():
    raw = b"pay me later"
    with pytest.raises(TermsRefused):
        load_terms(raw, terms_sha256(raw))


# --- acceptance is decided by the check written before payment ------------------------

def test_the_agreed_bytes_are_accepted():
    raw = terms_bytes()
    result = accept(load_terms(raw, terms_sha256(raw)), DELIVERABLE)
    assert result.deliverable_sha256 == hashlib.sha256(DELIVERABLE).hexdigest()
    assert result.terms_sha256 == terms_sha256(raw)


def test_other_bytes_are_not_accepted():
    raw = terms_bytes()
    with pytest.raises(NotAccepted):
        accept(load_terms(raw, terms_sha256(raw)), DELIVERABLE + b" ")


def test_json_keys_accepts_an_object_carrying_every_key():
    raw = terms_bytes(acceptance={"json_keys": ["answer", "source"]})
    assert accept(load_terms(raw, terms_sha256(raw)), DELIVERABLE)


@pytest.mark.parametrize(
    "deliverable",
    [b'{"answer": 42}', b'{"answer": 42, "source": null}', b'{"answer": 42, "source": ""}', b"[1, 2]", b"not json"],
)
def test_json_keys_refuses_a_deliverable_missing_a_key(deliverable):
    raw = terms_bytes(acceptance={"json_keys": ["answer", "source"]})
    with pytest.raises(NotAccepted):
        accept(load_terms(raw, terms_sha256(raw)), deliverable)


def test_accept_never_touches_the_network(monkeypatch):
    calls = node(monkeypatch)
    raw = terms_bytes()
    accept(load_terms(raw, terms_sha256(raw)), DELIVERABLE)
    assert calls == []


# --- settlement is checked against the terms, not against the seller ------------------

def test_settle_checks_the_pinned_payee_and_amount(monkeypatch):
    calls = node(monkeypatch)
    raw = terms_bytes()
    terms = load_terms(raw, terms_sha256(raw))
    done = accept(terms, DELIVERABLE)
    deal = settle(terms, done, HASH, URL)
    assert deal.settled is True
    assert calls == [{"action": "block_info", "json_block": "true", "hash": HASH}]
    record = json.loads(deal.to_json())
    assert record == {
        "terms_sha256": terms_sha256(raw),
        "deliverable_sha256": hashlib.sha256(DELIVERABLE).hexdigest(),
        "block_hash": HASH,
        "settled": True,
        "amount_raw": 10**24,
        "payee": PAYEE,
    }


def test_a_payment_to_another_account_does_not_settle_the_terms(monkeypatch):
    node(monkeypatch, paid=ANOTHER_ACCOUNT)
    raw = terms_bytes()
    terms = load_terms(raw, terms_sha256(raw))
    with pytest.raises(Mismatch):
        settle(terms, accept(terms, DELIVERABLE), HASH, URL)


def test_a_short_payment_does_not_settle_the_terms(monkeypatch):
    node(monkeypatch, amount="999999999999999999999999")
    raw = terms_bytes()
    terms = load_terms(raw, terms_sha256(raw))
    with pytest.raises(Mismatch):
        settle(terms, accept(terms, DELIVERABLE), HASH, URL)


def test_an_unconfirmed_payment_is_not_yet_settled(monkeypatch):
    node(monkeypatch, confirmed="false")
    raw = terms_bytes()
    terms = load_terms(raw, terms_sha256(raw))
    assert settle(terms, accept(terms, DELIVERABLE), HASH, URL).settled is False


def test_settle_refuses_an_acceptance_of_other_terms(monkeypatch):
    calls = node(monkeypatch)
    raw_a = terms_bytes()
    raw_b = terms_bytes(task="a different job")
    terms_a = load_terms(raw_a, terms_sha256(raw_a))
    terms_b = load_terms(raw_b, terms_sha256(raw_b))
    with pytest.raises(TermsRefused) as err:
        settle(terms_a, accept(terms_b, DELIVERABLE), HASH, URL)
    assert err.value.code == "acceptance_of_other_terms"
    assert calls == []


def test_the_module_never_signs_or_sends():
    source = open(nano_terms.__file__, encoding="utf-8").read()
    for word in ("process", "send_block", "seed", "private", "sign("):
        assert word not in source


def test_a_duplicated_key_is_refused():
    # Parsers disagree on which duplicate wins, so identical bytes could be read
    # as two different amounts by the two sides.
    raw = terms_bytes()[:-1] + b', "amount_raw": "1"}'
    with pytest.raises(TermsRefused) as err:
        load_terms(raw, terms_sha256(raw))
    assert err.value.code == "duplicate_field"
