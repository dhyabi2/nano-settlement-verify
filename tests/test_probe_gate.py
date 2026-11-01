"""probe_gate approves only when the wrong-recipient probe was refused first, in the same run."""

import importlib.util
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "skills" / "nano-settlement-verify"
spec = importlib.util.spec_from_file_location("probe_gate", SKILL / "probe_gate.py")
probe_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe_gate)

BLOCK = "B" * 64
RAW = "500000000000000000000000000"
PAYEE = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"


def fake(codes):
    calls = []

    def run(args):
        calls.append(args)
        return codes[len(calls) - 1], '{"n": %d}' % len(calls)
    return run, calls


def test_refused_probe_then_settled_approves():
    run, calls = fake([3, 0])
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 0 and lines[0] == "PROBE-REFUSED" and lines[-1] == "APPROVE"
    assert calls[0][2] != PAYEE and calls[1][2] == PAYEE


def test_probe_that_passes_approves_nothing():
    run, calls = fake([0, 0])
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 1


def test_unreadable_node_on_probe_is_not_a_refusal():
    run, calls = fake([4])
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 4 and "PROBE-REFUSED" not in lines and "APPROVE" not in lines


def test_real_check_failure_passes_its_code_through():
    run, _ = fake([3, 2])
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 2 and lines[0] == "PROBE-REFUSED" and "APPROVE" not in lines


def test_probe_recipient_never_equals_the_payee():
    for a in probe_gate.PROBE_ACCOUNTS + (PAYEE,):
        assert probe_gate.probe_account(a) != a


def test_rpc_url_reaches_both_calls():
    run, calls = fake([3, 0])
    probe_gate.gate(BLOCK, RAW, PAYEE, "https://rpc.nano.to", run=run)
    assert all(c[-1] == "https://rpc.nano.to" for c in calls)
