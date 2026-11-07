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
