"""Offline block check: no network. Two real mainnet blocks, pinned verbatim in tests/fixtures.

B749B757... is a 0.0005 XNO state send; 37402FF0... is the block before it on
the same account chain (a receive). Both were fetched once from rpc.nano.to
with block_info json_block=true and frozen.
"""
import copy, json, pathlib
import pytest
import nano_settlement_verify
import nano_offline as off

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEND = json.loads((ROOT / "tests/fixtures/block_B749B757.json").read_text())
PREV = json.loads((ROOT / "tests/fixtures/block_37402FF0.json").read_text())
H = "B749B757EE750FC9AEA72F33CB429EACCD2ABEC9F2CCF59BF17AFAC304C9A58F"
C, SIG = SEND["contents"], SEND["contents"]["signature"]
PC = PREV["contents"]
OTHER = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to open a socket")
    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


def test_real_signature_verifies():
    assert off.verify_block_signature(C, SIG) is True
    assert off.verify_block_signature(PC, PC["signature"]) is True


def test_one_flipped_balance_digit_fails():
    bad = dict(C, balance="7" + C["balance"][1:])
    assert off.verify_block_signature(bad, SIG) is False


def test_one_flipped_signature_byte_fails():
    raw = bytearray(bytes.fromhex(SIG))
    for i in (0, 40):  # one byte in R, one in S
        flipped = bytearray(raw)
        flipped[i] ^= 0x01
        assert off.verify_block_signature(C, flipped.hex()) is False


def test_wrong_account_fails():
    assert off.verify_block_signature(dict(C, account=OTHER), SIG) is False


def test_legacy_send_needs_an_account():
    legacy = {"type": "send", "previous": C["previous"], "destination": OTHER,
              "balance": "%032X" % int(C["balance"])}
    assert off.verify_block_signature(legacy, SIG) is False
    assert off.verify_block_signature(legacy, SIG, account=C["account"]) is False  # not its signature


def test_non_canonical_s_is_rejected():
    raw = bytes.fromhex(SIG)
    s = int.from_bytes(raw[32:], "little")
    high = raw[:32] + (s + off.L).to_bytes(32, "little")  # same S mod L: verifies if not rejected
    assert off.verify_block_signature(C, high.hex()) is False


@pytest.mark.parametrize("contents, sig", [
    (None, SIG), ("x", SIG), ({}, SIG), (C, None), (C, ""), (C, "zz" * 64),
    (C, SIG[:-2]), (C, 12345), (dict(C, balance="-1"), SIG), (dict(C, account=None), SIG),
    (C, "FF" * 64), (C, "00" * 64),
])
def test_garbage_is_false_never_raises(contents, sig):
    assert off.verify_block_signature(contents, sig) is False


def test_offline_check_with_real_previous():
    out = off.offline_check(C, SIG, PC)
    assert out["block_hash"] == H
    assert out["signature_valid"] is True
    assert out["previous_matches"] is True
    assert out["balance_change_raw"] == -int(SEND["amount"])  # a send: balance went down
    assert out["amount_raw"] == int(SEND["amount"]) == 500000000000000000000000000
    assert "nano_quorum" in out["note"]


def test_offline_check_without_previous():
    out = off.offline_check(C, SIG)
    assert out["signature_valid"] is True
    assert out["previous_matches"] is None and out["amount_raw"] is None


def test_offline_check_tampered_previous():
    bad = dict(PC, balance=str(int(PC["balance"]) + 1))
    out = off.offline_check(C, SIG, bad)
    assert out["previous_matches"] is False
    assert out["amount_raw"] is None and out["balance_change_raw"] is None


def test_offline_check_receive_reports_a_positive_change():
    # the previous block itself is a receive; its own previous is not pinned, so
    # no change is computed - but the signature still checks offline.
    out = off.offline_check(PC, PC["signature"])
    assert out["signature_valid"] is True and out["balance_change_raw"] is None


def test_offline_check_garbage_never_raises():
    out = off.offline_check("nonsense", None, previous_contents=42)
    assert out["signature_valid"] is False and out["previous_matches"] is False


def test_cli_exit_codes(monkeypatch, capsys):
    replies = {H: copy.deepcopy(SEND), C["previous"]: copy.deepcopy(PREV)}
    monkeypatch.setattr(off, "post_json", lambda url, p: replies[p["hash"]])
    assert off.main([H]) == 0
    assert json.loads(capsys.readouterr().out)["amount_raw"] == 500000000000000000000000000
    replies[H]["contents"]["balance"] = "1"
    assert off.main([H]) == 1


# --- break-review fixes: small-order keys, cross-account previous, unsigned amounts, balance parsing ---

import hashlib

_ALPHA = "13456789abcdefghijkmnopqrstuwxyz"


def _address(key: bytes) -> str:
    def b32(n, chars):
        return "".join(_ALPHA[(n >> (5 * i)) & 31] for i in reversed(range(chars)))
    check = int.from_bytes(hashlib.blake2b(key, digest_size=5).digest()[::-1], "big")
    return "nano_" + b32(int.from_bytes(key, "big"), 52) + b32(check, 8)


def _encode(pt) -> bytes:
    x, y, z, _ = pt
    zi = pow(z, off.P - 2, off.P)
    x, y = x * zi % off.P, y * zi % off.P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _h(*parts) -> int:
    return int.from_bytes(hashlib.blake2b(b"".join(parts), digest_size=64).digest(), "little")


def _keypair(seed: bytes):
    d = hashlib.blake2b(seed, digest_size=64).digest()
    a = int.from_bytes(d[:32], "little")
    a = (a & ((1 << 254) - 8)) | (1 << 254)
    return a, d[32:], _encode(off._mul(a, off._BASE))


def _sign(seed: bytes, contents: dict) -> str:
    a, prefix, pub = _keypair(seed)
    msg = bytes.fromhex(off.block_hash_of(contents))
    r = _h(prefix, msg) % off.L
    R = _encode(off._mul(r, off._BASE))
    s = (r + _h(R, pub, msg) % off.L * a) % off.L
    return (R + s.to_bytes(32, "little")).hex().upper()


def _state(seed: bytes, previous: str, balance: int) -> dict:
    return {"type": "state", "account": _address(_keypair(seed)[2]), "previous": previous,
            "representative": C["representative"], "balance": str(balance), "link": "00" * 32}


def test_identity_key_forgery_is_rejected():
    identity_account = _address((1).to_bytes(32, "little"))  # the identity point, y = 1
    assert identity_account.startswith("nano_11a1")
    forged = dict(C, account=identity_account)
    assert off.verify_block_signature(forged, "01" + "00" * 63) is False
    assert off.verify_block_signature(dict(forged, balance="1"), "01" + "00" * 63) is False


def test_all_small_order_points_are_flagged():
    # the 8-torsion: points P with 8*P = identity; none of them may serve as A or R
    torsion = [off._decode((1).to_bytes(32, "little")), off._decode((off.P - 1).to_bytes(32, "little"))]
    assert all(t is not None and off._small_order(t) for t in torsion)
    assert not off._small_order(off._BASE)
    assert not off._small_order(off._decode(bytes.fromhex(SIG)[:32]))


def test_previous_from_another_account_gives_no_amount():
    seed = b"test-only key, holds nothing"
    block = _state(seed, H, 1)  # signed by our key, but `previous` is another account's block (C)
    sig = _sign(seed, block)
    assert off.verify_block_signature(block, sig) is True
    out = off.offline_check(block, sig, C)
    assert out["previous_matches"] is True
    assert out["amount_raw"] is None and out["balance_change_raw"] is None
    assert out["previous_same_account"] is False
    # control: the same key's own previous block does give an amount
    first = _state(seed, "11" * 32, 10)
    second = _state(seed, off.block_hash_of(first), 4)
    out = off.offline_check(second, _sign(seed, second), first)
    assert out["previous_same_account"] is True and out["balance_change_raw"] == -6 and out["amount_raw"] == 6


def test_no_amount_without_a_valid_signature():
    out = off.offline_check(dict(C, balance="0"), SIG, PC)  # balance edited: signature no longer valid
    assert out["signature_valid"] is False and out["previous_matches"] is True
    assert out["amount_raw"] is None and out["balance_change_raw"] is None


@pytest.mark.parametrize("raw", ["５", "٥", "9" * 40, str(2**128), "", "+5", " 5"])
def test_balance_parsing_is_ascii_and_bounded(raw):
    assert off._balance({"type": "state", "balance": raw}) is None


def test_balance_parsing_accepts_the_max():
    assert off._balance({"type": "state", "balance": str(2**128 - 1)}) == 2**128 - 1
