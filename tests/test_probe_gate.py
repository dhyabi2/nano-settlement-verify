"""probe_gate approves only when the wrong-recipient probe was refused first, in the same run."""

import importlib.util
import json
import os
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
        code = codes[len(calls) - 1]
        if code == 3:
            return code, mismatch(args[2])
        return code, '{"n": %d}' % len(calls)
    return run, calls


def mismatch(expected, got=PAYEE, **extra):
    return json.dumps(dict({"verdict": "mismatch", "expected": expected, "got": got}, **extra))


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


HONEST = "https://honest.example"
YES = "https://yes.example"


def failover_nodes(nodes):
    """A run() that answers like verify_cli.py over an RPC_URL list.

    Each node is a function (account, call_number) -> exit code; 4 fails over to the
    next node, any other answer is final and carries rpc_index when the list is longer
    than one, as verify_cli.py prints it.
    """
    calls = []

    def run(args):
        calls.append(args)
        positional = [a for a in args if not a.startswith("--")]
        url_arg = positional[3] if len(positional) > 3 else os.environ.get("NANO_RPC_URL", HONEST)
        urls = url_arg.split(",")
        for index, url in enumerate(urls):
            code = nodes[url](positional[2], len(calls))
            if code != 4:
                verdict = ({"verdict": "mismatch", "expected": positional[2], "got": PAYEE}
                           if code == 3 else {"settled": code == 0})
                if len(urls) > 1:
                    verdict["rpc_index"] = index
                return code, json.dumps(verdict)
        return 4, '{"verdict": "node_unreachable"}'
    return run, calls


def test_failover_cannot_hand_the_real_check_to_another_node():
    # The yes-to-anything node is down for the probe, so the honest node refuses it;
    # it is back for the real check. The block paid someone else: honest says 3.
    nodes = {
        YES: lambda account, n: 4 if n == 1 else 0,
        HONEST: lambda account, n: 3,
    }
    run, calls = failover_nodes(nodes)
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, YES + "," + HONEST, run=run)
    assert code == 3 and "APPROVE" not in lines
    assert calls[1][3] == HONEST


def test_failover_list_from_the_environment_is_pinned_too(monkeypatch):
    monkeypatch.setenv("NANO_RPC_URL", YES + "," + HONEST)
    nodes = {
        YES: lambda account, n: 4 if n == 1 else 0,
        HONEST: lambda account, n: 3,
    }
    run, calls = failover_nodes(nodes)
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 3 and "APPROVE" not in lines and calls[1][3] == HONEST


def test_list_probe_without_rpc_index_approves_nothing():
    run, calls = fake([3, 0])
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, YES + "," + HONEST, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 1


def test_real_check_answered_from_a_list_approves_nothing():
    calls = []

    def run(args):
        calls.append(args)
        return (3, mismatch(args[2], rpc_index=1)) if len(calls) == 1 else (0, '{"settled": true, "rpc_index": 0}')
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, YES + "," + HONEST, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines


def test_honest_list_still_approves_on_the_pinned_node():
    nodes = {
        YES: lambda account, n: 4,
        HONEST: lambda account, n: 0 if account == PAYEE else 3,
    }
    run, calls = failover_nodes(nodes)
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, YES + "," + HONEST, run=run)
    assert code == 0 and lines[-1] == "APPROVE" and calls[1][3] == HONEST


def test_unconfirmed_block_on_the_probe_is_exit_2_not_7():
    run, calls = fake([2])
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 2 and "APPROVE" not in lines and len(calls) == 1


def test_xrb_spelling_of_a_probe_account_still_gets_the_other_one():
    burn = probe_gate.PROBE_ACCOUNTS[0]
    xrb_burn = "xrb_" + burn[len("nano_"):]
    probe = probe_gate.probe_account(xrb_burn)
    assert probe == probe_gate.PROBE_ACCOUNTS[1]
    run, calls = fake([3, 0])
    code, _ = probe_gate.gate(BLOCK, RAW, xrb_burn, run=run)
    assert code == 0 and calls[0][2] == probe_gate.PROBE_ACCOUNTS[1]


def test_not_after_reaches_both_calls_with_an_rpc_url():
    run, calls = fake([3, 0])
    code, _ = probe_gate.gate(BLOCK, RAW, PAYEE, "https://rpc.nano.to", run=run,
                              not_after="2026-10-09T18:00:00Z")
    assert code == 0
    assert all(c[-2:] == ["--not-after", "2026-10-09T18:00:00Z"] for c in calls)
    assert all("https://rpc.nano.to" in c for c in calls)


def test_main_takes_not_after_with_rpc_url(monkeypatch):
    seen = {}

    def gate(*args, not_after=None):
        seen["args"], seen["not_after"] = args, not_after
        return 0, []
    monkeypatch.setattr(probe_gate, "gate", gate)
    assert probe_gate.main([BLOCK, RAW, PAYEE, "https://rpc.nano.to", "--not-after", "1700000000"]) == 0
    assert seen == {"args": (BLOCK, RAW, PAYEE, "https://rpc.nano.to"), "not_after": "1700000000"}


def scripted(probe_reply):
    """Probe answers (3, probe_reply(probe_account)); the real check would say settled."""
    calls = []

    def run(args):
        calls.append(args)
        if len(calls) == 1:
            return probe_reply(args[2])
        return 0, '{"settled": true}'
    return run, calls


def test_probe_not_found_is_not_a_refusal_and_exits_2():
    # A check that no longer compares the payee: the probe hits "Block not found"
    # (exit 3) and the real check says settled for a send to someone else.
    run, calls = scripted(lambda probe: (3, '{"verdict": "no_such_block"}'))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 2 and "APPROVE" not in lines and "PROBE-REFUSED" not in lines and len(calls) == 1


def test_probe_invalid_amount_is_not_a_refusal():
    run, calls = scripted(lambda probe: (3, '{"verdict": "invalid_amount", "expect_raw": "x"}'))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 1


def test_probe_amount_mismatch_is_not_a_payee_refusal():
    run, calls = scripted(lambda probe: (3, mismatch(RAW, got="1")))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 1


def test_probe_payee_mismatch_naming_another_account_is_not_a_refusal():
    run, calls = scripted(lambda probe: (3, mismatch(PAYEE, got=probe)))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 1


def test_probe_exit_3_without_json_is_not_a_refusal():
    for out in ("", "not json", "[]", '{"verdict": "mismatch"}', '{"verdict": "mismatch", "expected": 5}'):
        run, calls = scripted(lambda probe: (3, out))
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 1


def test_probe_payee_mismatch_in_xrb_spelling_is_a_refusal():
    run, calls = scripted(lambda probe: (3, mismatch("xrb_" + probe[len("nano_"):])))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 0 and lines[0] == "PROBE-REFUSED" and lines[-1] == "APPROVE"


def test_probe_usage_error_passes_through_as_64():
    run, calls = scripted(lambda probe: (64, '{"verdict": "invalid_not_after", "not_after": "x"}'))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run, not_after="x")
    assert code == 64 and "APPROVE" not in lines and len(calls) == 1
    assert not any("PROBE-NOT-REFUSED" in line for line in lines)


def test_real_cli_usage_error_on_probe_exits_64():
    # End to end through verify_cli.py: a bad --not-after is a usage error before any node is asked.
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, "https://127.0.0.1:9", not_after="not-a-time")
    assert code == 64 and "APPROVE" not in lines


def test_skill_source_line_names_every_file_it_ships():
    text = (SKILL / "SKILL.md").read_text()
    source = text.split("## Source", 1)[1].split("##", 1)[0]
    assert "One file" not in source
    for name in ("nano_settlement_verify.py", "verify_cli.py", "probe_gate.py"):
        assert name in source and (SKILL / name).exists()
