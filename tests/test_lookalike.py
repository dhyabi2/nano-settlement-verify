"""Address-poisoning lookalikes are refused: the payee is compared in full.

A poisoning attacker grinds an address that shares its target's first and last
characters, because that is what wallets show and people check. The verifier
must compare the whole account, so a lookalike - even one with a perfectly
valid checksum - is a stranger.

LOOKALIKE below was ground offline (about 2**30 blake2b tries) from PAYEE's
public key with bytes 8..15 changed until the low 30 bits of the checksum,
i.e. the last 6 address characters, matched again. No test touches the network.
"""

import pytest

import nano_settlement_verify
from nano_settlement_verify import Mismatch, is_valid_account, public_key_from_address, verify

HASH = "B2EC1E2A1F2C3D4E5F60718293A4B5C6D7E8F9012345678901234567890ABCDE"
URL = "http://127.0.0.1:7076"
RAW = 10**24

PAYEE = "nano_3b8csjscuwrpkfphb6dncn5hth9zcpgt5igjnpyj7r6o3wn47hx5rbfr9nub"
LOOKALIKE = "nano_3b8csjscuwrpkfr4d9zk86birchzcpgt5igjnpyj7r6o3wn47hx54mfr9nub"


def _body(address):
    return address.split("_", 1)[1]


def _flip_checksum(address):
    """Same key characters, last checksum character changed: invalid checksum."""
    last = address[-1]
    return address[:-1] + ("1" if last != "1" else "3")


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a socket")

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


@pytest.fixture
def node(monkeypatch):
    def stub(paid_to, legacy=False):
        if legacy:
            contents = {"type": "send", "destination": paid_to}
            reply = {"amount": str(RAW), "confirmed": "true", "height": "7", "contents": contents}
        else:
            contents = {"type": "state", "account": "nano_payer", "link_as_account": paid_to}
            reply = {"block_account": "nano_payer", "amount": str(RAW), "confirmed": "true",
                     "height": "7", "subtype": "send", "contents": contents}
        monkeypatch.setattr(nano_settlement_verify, "post_json", lambda url, payload: reply)

    return stub


def test_the_lookalike_is_a_real_poisoning_candidate():
    """The premise: valid checksum, same first 10 and last 6, different key."""
    assert is_valid_account(PAYEE)
    assert is_valid_account(LOOKALIKE)
    assert _body(LOOKALIKE)[:10] == _body(PAYEE)[:10]
    assert _body(LOOKALIKE)[-6:] == _body(PAYEE)[-6:]
    assert public_key_from_address(LOOKALIKE) != public_key_from_address(PAYEE)


@pytest.mark.parametrize("legacy", [False, True], ids=["state", "pre-state"])
@pytest.mark.parametrize("prefix", ["nano_", "xrb_"])
def test_a_send_to_the_checksum_valid_lookalike_is_refused(node, legacy, prefix):
    node(prefix + _body(LOOKALIKE), legacy=legacy)

    with pytest.raises(Mismatch) as caught:
        verify(HASH, RAW, PAYEE, URL)

    assert caught.value.got == prefix + _body(LOOKALIKE)
    assert caught.value.expected == PAYEE


def test_expecting_the_lookalike_refuses_a_send_to_the_payee(node):
    node(PAYEE)

    with pytest.raises(Mismatch):
        verify(HASH, RAW, LOOKALIKE, URL)


def test_a_lookalike_with_a_broken_checksum_is_refused(node):
    """Same first 10 and last 6 is not enough even without a valid checksum."""
    fake = "nano_" + _body(PAYEE)[:10] + "1" * 44 + _body(PAYEE)[-6:]
    assert not is_valid_account(fake)
    node(fake)

    with pytest.raises(Mismatch):
        verify(HASH, RAW, PAYEE, URL)


def test_case_is_not_folded(node):
    """Nano addresses are lowercase base32; an upper-cased spelling is not
    silently treated as the account (fail closed)."""
    node(PAYEE.upper())

    with pytest.raises(Mismatch):
        verify(HASH, RAW, PAYEE, URL)
    assert not is_valid_account(PAYEE.upper())


@pytest.mark.parametrize("paid_prefix", ["nano_", "xrb_"])
@pytest.mark.parametrize("expected_prefix", ["nano_", "xrb_"])
def test_nano_and_xrb_spellings_of_the_same_key_are_one_account(node, paid_prefix, expected_prefix):
    node(paid_prefix + _body(PAYEE))

    receipt = verify(HASH, RAW, expected_prefix + _body(PAYEE), URL)

    assert receipt.settled is True
    assert public_key_from_address(paid_prefix + _body(PAYEE)) == public_key_from_address(
        expected_prefix + _body(PAYEE)
    )


def test_an_invalid_checksum_address_is_rejected_as_input():
    """The input validator every module uses (nano_claim, nano_payers) refuses it."""
    bad = _flip_checksum(PAYEE)
    assert not is_valid_account(bad)
    assert public_key_from_address(bad) is None
    assert not is_valid_account("xrb_" + _body(bad))


def test_an_invalid_checksum_expectation_never_settles_a_real_payment(node):
    """verify() refuses (Mismatch) rather than settling, when the expected
    account is a one-character checksum typo of the account actually paid."""
    node(PAYEE)

    with pytest.raises(Mismatch):
        verify(HASH, RAW, _flip_checksum(PAYEE), URL)
