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
# What verify_cli.py prints with exit 2: the receipt of a block that is not confirmed yet.
NOT_YET = '{"settled": false, "amount_raw": 0, "height": 0, "account": ""}'


def receipt(args, account=None, **extra):
    """verify_cli.py's exit-0 receipt for the call `args` (on time, with --not-after)."""
    out = {"settled": True, "amount_raw": int(args[1]), "height": 42,
           "account": args[2] if account is None else account}
    if "--not-after" in args:
        out.update(seen_at=1700000000, not_after=1700000000, time_source="node local_timestamp")
    out.update(extra)
    return json.dumps(out)


def fake(codes):
    calls = []

    def run(args):
        calls.append(args)
        code = codes[len(calls) - 1]
        if code == 3:
            return code, mismatch(args[2])
        if code == 2:
            return code, NOT_YET
        if code == 0:
            return code, receipt(args)
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
    # A check that says yes to anything: the probe settles and so does the real check.
    run, calls = fake([0, 0])
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    # The settled probe is followed by exactly one real check (the payee), which settles
    # too and so does not explain the probe: nothing approved.
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 2
    assert calls[1][2] == PAYEE and lines[-1] == "REAL-CHECK-DOES-NOT-EXPLAIN-PROBE (exit 0)"


def test_unreadable_node_on_probe_is_not_a_refusal():
    run, calls = fake([4])
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 4 and "PROBE-REFUSED" not in lines and "APPROVE" not in lines


def test_real_check_unreadable_node_passes_through_but_not_yet_is_7():
    run, _ = fake([3, 4])
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 4 and lines[0] == "PROBE-REFUSED" and "APPROVE" not in lines
    # The probe's payee mismatch says the block is confirmed: "not yet" contradicts it.
    run, _ = fake([3, 2])
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines


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
                           if code == 3 else json.loads(receipt(positional)) if code == 0
                           else {"settled": False})
                if len(urls) > 1:
                    verdict["rpc_index"] = index
                return code, json.dumps(verdict)
        return 4, '{"verdict": "node_unreachable"}'
    return run, calls


def test_failover_cannot_hand_the_real_check_to_another_node():
    # The yes-to-anything node is down for the probe, so the honest node refuses it;
    # it is back for the real check. The honest node then answers 3, contradicting its
    # own probe (which said the block pays PAYEE): 7, and never the YES node.
    nodes = {
        YES: lambda account, n: 4 if n == 1 else 0,
        HONEST: lambda account, n: 3,
    }
    run, calls = failover_nodes(nodes)
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, YES + "," + HONEST, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines
    assert calls[1][3] == HONEST


def test_failover_list_from_the_environment_is_pinned_too(monkeypatch):
    monkeypatch.setenv("NANO_RPC_URL", YES + "," + HONEST)
    nodes = {
        YES: lambda account, n: 4 if n == 1 else 0,
        HONEST: lambda account, n: 3,
    }
    run, calls = failover_nodes(nodes)
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and calls[1][3] == HONEST


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
    run, calls = probe_then(lambda probe: mismatch(probe, got=xrb_burn))
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
        return 0, receipt(args)
    return run, calls


def test_probe_not_found_is_terminal_exit_3_not_wait():
    # A made-up hash: the probe hits "Block not found" (verify_cli.py exit 3, "do not
    # serve"). Exit 2 would tell a retrying caller to poll that hash forever; it is 3,
    # and the real check (which would say settled here) is never asked.
    run, calls = scripted(lambda probe: (3, '{"verdict": "no_such_block"}'))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 3 and "APPROVE" not in lines and "PROBE-REFUSED" not in lines and len(calls) == 1


def paid_to(recipient):
    """A node for a confirmed send of RAW to RECIPIENT, answering like verify_cli.py."""
    calls = []

    def run(args):
        calls.append(args)
        if probe_gate._account_body(args[2]) == probe_gate._account_body(recipient):
            return 0, receipt(args)
        return 3, mismatch(args[2], got=recipient)
    return run, calls


def test_payment_sent_to_the_burn_address_checked_for_a_seller_exits_3_not_7():
    burn = probe_gate.PROBE_ACCOUNTS[0]
    run, calls = paid_to(burn)
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 3 and "APPROVE" not in lines and "PROBE-REFUSED" not in lines
    assert lines[-1] == "WRONG-RECIPIENT: nothing approved"
    assert calls[0][2] == burn and calls[1][2] == PAYEE and len(calls) == 2


def test_payment_to_the_second_probe_account_checked_for_the_burn_address_exits_3():
    burn, other = probe_gate.PROBE_ACCOUNTS
    run, calls = paid_to(other)
    code, lines = probe_gate.gate(BLOCK, RAW, burn, run=run)
    assert code == 3 and "APPROVE" not in lines and calls[0][2] == other


def test_settled_probe_then_mismatch_naming_another_recipient_is_7():
    # The node says the block pays the probe account, then that it pays someone else:
    # the two answers contradict each other, so it is not a wrong-recipient refusal.
    calls = []

    def run(args):
        calls.append(args)
        if len(calls) == 1:
            return 0, '{"settled": true}'
        return 3, mismatch(args[2], got="nano_3someoneelse")
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines


def test_settled_probe_on_a_list_without_rpc_index_asks_nothing_more():
    run, calls = paid_to(probe_gate.PROBE_ACCOUNTS[0])
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, YES + "," + HONEST, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 1


def test_settled_probe_real_check_is_pinned_to_the_probe_node():
    burn = probe_gate.PROBE_ACCOUNTS[0]
    nodes = {
        YES: lambda account, n: 4 if n == 1 else 0,
        HONEST: lambda account, n: 0 if account == burn else 3,
    }
    run, calls = failover_nodes(nodes)
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, YES + "," + HONEST, run=run)
    # failover_nodes reports got=PAYEE on a mismatch, so it does not explain the probe: 7,
    # and the real check went only to the node that answered the probe.
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines
    assert calls[1][3] == HONEST


def test_probe_invalid_amount_is_not_a_refusal():
    run, calls = scripted(lambda probe: (3, '{"verdict": "invalid_amount", "expect_raw": "x"}'))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 1


def test_probe_amount_mismatch_is_not_a_payee_refusal():
    # The probe stopped on the amount; a real check that then says settled disagrees
    # with it on the same node: nothing approved, exit 7.
    run, calls = scripted(lambda probe: (3, mismatch(RAW, got="1")))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines
    assert "PROBE-REFUSED" not in lines and calls[1][2] == PAYEE


def underpaid(got_amount=str(int(RAW) - 1), operation_mismatch=False):
    """A node for a send of too little to the right payee: verify stops on the amount
    (or the operation) before the payee, on the probe and on the real check alike."""
    calls = []

    def run(args):
        calls.append(args)
        if operation_mismatch:
            return 3, mismatch("send", got="receive")
        return 3, mismatch(str(int(args[1])), got=got_amount)
    return run, calls


def test_honest_underpayment_to_the_right_payee_exits_3_not_7():
    run, calls = underpaid()
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 3 and "APPROVE" not in lines and "PROBE-REFUSED" not in lines
    assert len(calls) == 2 and calls[0][2] != PAYEE and calls[1][2] == PAYEE


def test_overpayment_and_not_a_send_exit_3_too():
    for run, calls in (underpaid(got_amount=str(int(RAW) + 1)), underpaid(operation_mismatch=True)):
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
        assert code == 3 and "APPROVE" not in lines and len(calls) == 2


def test_underpayment_real_check_is_pinned_to_the_probe_node():
    calls = []

    def run(args):
        calls.append(args)
        return 3, mismatch(RAW, got="1", rpc_index=1)
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, YES + "," + HONEST, run=run)
    assert code == 3 and calls[1][3] == HONEST and "APPROVE" not in lines


def test_underpayment_probe_on_a_list_without_rpc_index_approves_nothing():
    run, calls = underpaid()
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, YES + "," + HONEST, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 1


def test_amount_probe_then_real_check_with_another_mismatch_is_7():
    calls = []

    def run(args):
        calls.append(args)
        if len(calls) == 1:
            return 3, mismatch(RAW, got="1")
        return 3, mismatch(PAYEE, got=probe_gate.PROBE_ACCOUNTS[0])
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines


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


def test_underpayment_through_the_real_verify_cli_exits_3(monkeypatch, capsys):
    # End to end through verify_cli.main and the real verify: a confirmed send of one raw
    # too little to the right payee. The probe stops on the amount, the real check too.
    vspec = importlib.util.spec_from_file_location("verify_cli_probe_gate_e2e", SKILL / "verify_cli.py")
    verify_cli = importlib.util.module_from_spec(vspec)
    vspec.loader.exec_module(verify_cli)
    reply = {
        "block_account": "nano_3payer", "amount": str(int(RAW) - 1), "confirmed": "true",
        "height": "42", "subtype": "send",
        "contents": {"type": "state", "account": "nano_3payer", "link_as_account": PAYEE},
    }
    monkeypatch.setattr(verify_cli._library, "post_json", lambda url, payload: reply)
    monkeypatch.delenv("NANO_RPC_URL", raising=False)

    def run(args):
        code = verify_cli.main(["verify_cli.py"] + list(args))
        return code, capsys.readouterr().out

    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, "http://127.0.0.1:7076", run=run)
    assert code == 3 and "APPROVE" not in lines
    # Controls on the same node: paid in full to the payee approves; paid in full to
    # someone else is refused by the real check (exit 3).
    reply["amount"] = RAW
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, "http://127.0.0.1:7076", run=run)
    assert code == 0 and lines[0] == "PROBE-REFUSED" and lines[-1] == "APPROVE"
    reply["contents"]["link_as_account"] = probe_gate.PROBE_ACCOUNTS[1]
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, "http://127.0.0.1:7076", run=run)
    assert code == 3 and "APPROVE" not in lines


def test_burn_address_payment_through_the_real_verify_cli_exits_3(monkeypatch, capsys):
    # End to end: a confirmed send of the full amount to the burn address, checked for
    # a seller. The probe (the burn address) settles; the real check refuses the payee.
    vspec = importlib.util.spec_from_file_location("verify_cli_probe_gate_burn", SKILL / "verify_cli.py")
    verify_cli = importlib.util.module_from_spec(vspec)
    vspec.loader.exec_module(verify_cli)
    burn = probe_gate.PROBE_ACCOUNTS[0]
    reply = {
        "block_account": "nano_3payer", "amount": RAW, "confirmed": "true",
        "height": "42", "subtype": "send",
        "contents": {"type": "state", "account": "nano_3payer", "link_as_account": burn},
    }
    monkeypatch.setattr(verify_cli._library, "post_json", lambda url, payload: reply)
    monkeypatch.delenv("NANO_RPC_URL", raising=False)

    def run(args):
        code = verify_cli.main(["verify_cli.py"] + list(args))
        return code, capsys.readouterr().out

    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, "http://127.0.0.1:7076", run=run)
    assert code == 3 and "APPROVE" not in lines and lines[-1] == "WRONG-RECIPIENT: nothing approved"
    # The burn address checking its own payment is refused by nothing: approved.
    code, lines = probe_gate.gate(BLOCK, RAW, burn, "http://127.0.0.1:7076", run=run)
    assert code == 0 and lines[-1] == "APPROVE"


THIRD = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"


def probe_then(probe_reply, real=lambda args: (0, receipt(args))):
    """Probe answers (3, probe_reply(probe_account)); the real check answers `real`."""
    calls = []

    def run(args):
        calls.append(args)
        if len(calls) == 1:
            return 3, probe_reply(args[2])
        return real(args) if callable(real) else real
    return run, calls


def test_probe_mismatch_reporting_the_probe_account_as_recipient_is_not_a_refusal():
    # A contradictory verdict: "expected the probe account, got the probe account".
    run, calls = probe_then(lambda probe: mismatch(probe, got=probe))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines
    assert "PROBE-REFUSED" not in lines and len(calls) == 1


def test_probe_mismatch_in_another_spelling_of_the_probe_account_is_not_a_refusal():
    run, calls = probe_then(lambda probe: mismatch(probe, got="xrb_" + probe[len("nano_"):]))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 1


def test_probe_mismatch_without_a_reported_recipient_is_not_a_refusal():
    for got in (None, "", 5, "not-an-account"):
        run, calls = probe_then(lambda probe: json.dumps(
            {"verdict": "mismatch", "expected": probe, "got": got}))
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 1
    run, calls = probe_then(lambda probe: json.dumps({"verdict": "mismatch", "expected": probe}))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 1


def test_probe_naming_a_third_recipient_then_settled_real_check_approves_nothing():
    # The probe says the block pays THIRD, the real check says it pays PAYEE: inverted.
    run, calls = probe_then(lambda probe: mismatch(probe, got=THIRD))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines
    assert "PROBE-REFUSED" not in lines and len(calls) == 2 and calls[1][2] == PAYEE


def test_probe_naming_a_third_recipient_confirmed_by_the_real_check_is_wrong_recipient():
    run, calls = probe_then(lambda probe: mismatch(probe, got=THIRD),
                            real=lambda args: (3, mismatch(args[2], got=THIRD)))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 3 and "APPROVE" not in lines and "PROBE-REFUSED" not in lines
    assert lines[-1] == "WRONG-RECIPIENT: nothing approved" and len(calls) == 2


def test_probe_and_real_check_naming_different_third_recipients_is_7():
    run, calls = probe_then(lambda probe: mismatch(probe, got=THIRD),
                            real=lambda args: (3, mismatch(args[2], got=probe_gate.PROBE_ACCOUNTS[1])))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines


def test_probe_naming_a_third_recipient_pins_the_real_check():
    run, calls = probe_then(lambda probe: mismatch(probe, got=THIRD, rpc_index=1),
                            real=lambda args: (3, mismatch(args[2], got=THIRD)))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, YES + "," + HONEST, run=run)
    assert code == 3 and calls[1][3] == HONEST and "APPROVE" not in lines
    run, calls = probe_then(lambda probe: mismatch(probe, got=THIRD))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, YES + "," + HONEST, run=run)
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines and len(calls) == 1


def _real_cli(monkeypatch, capsys, reply, name):
    """A run() through verify_cli.main and the real verify, answered by REPLY."""
    vspec = importlib.util.spec_from_file_location(name, SKILL / "verify_cli.py")
    verify_cli = importlib.util.module_from_spec(vspec)
    vspec.loader.exec_module(verify_cli)
    monkeypatch.setattr(verify_cli._library, "post_json", lambda url, payload: reply)
    monkeypatch.delenv("NANO_RPC_URL", raising=False)

    def run(args):
        code = verify_cli.main(["verify_cli.py"] + list(args))
        return code, capsys.readouterr().out
    return run


def test_late_or_untimed_burn_address_payment_through_the_real_verify_cli_exits_3(monkeypatch, capsys):
    # A payment to the burn address checked for a seller, with --not-after: the probe (the
    # burn address) matches on amount and payee and then fails on time - late (5) or no
    # timestamp (6). It still pays the probe account, so it is a wrong recipient (3), not 7.
    burn = probe_gate.PROBE_ACCOUNTS[0]
    reply = {
        "block_account": "nano_3payer", "amount": RAW, "confirmed": "true",
        "height": "42", "subtype": "send", "local_timestamp": "1790000000",
        "contents": {"type": "state", "account": "nano_3payer", "link_as_account": burn},
    }
    run = _real_cli(monkeypatch, capsys, reply, "verify_cli_probe_gate_late_burn")
    for timestamp, probe_code in (("1790000000", 5), ("0", 6)):
        reply["local_timestamp"] = timestamp
        assert run([BLOCK, RAW, burn, "http://127.0.0.1:7076", "--not-after", "1700000000"])[0] == probe_code
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, "http://127.0.0.1:7076", run=run,
                                      not_after="1700000000")
        assert code == 3 and "APPROVE" not in lines and lines[-1] == "WRONG-RECIPIENT: nothing approved"


def test_late_probe_without_the_probe_account_as_recipient_is_7():
    # Exit 5 or 6 on the probe counts as "pays the probe account" only from a parsed receipt
    # whose account is the probe account; the real check would confirm a wrong recipient.
    for receipt in ('{"verdict": "late", "account": "%s"}' % PAYEE, '{"verdict": "late"}',
                    '{"verdict": "unknown_time", "account": "%s"}' % THIRD, "",
                    '{"verdict": "mismatch", "account": "%s"}' % probe_gate.PROBE_ACCOUNTS[0]):
        calls = []

        def run(args):
            calls.append(args)
            if len(calls) == 1:
                return 5, receipt
            return 3, mismatch(args[2], got=probe_gate.PROBE_ACCOUNTS[0])
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run, not_after="1700000000")
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines, receipt


def test_exit_2_without_a_not_yet_receipt_is_7_not_pending():
    # Python exits 2 when it cannot open verify_cli.py; that is a broken check, not a block
    # that is not confirmed yet, so a caller that retries on 2 must not be told to wait.
    for out in ("", "python3: can't open file", '{"settled": true}', '{"verdict": "mismatch"}'):
        run, calls = scripted(lambda probe: (2, out))
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines, out
        calls = []

        def real_two(args):
            calls.append(args)
            return (3, mismatch(args[2])) if len(calls) == 1 else (2, out)
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=real_two)
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines, out
    # A real not-yet receipt still reads as pending on the probe; on the real check, after
    # a probe that said confirmed, it contradicts the probe.
    assert probe_gate.gate(BLOCK, RAW, PAYEE, run=fake([2])[0])[0] == 2
    assert probe_gate.gate(BLOCK, RAW, PAYEE, run=fake([3, 2])[0])[0] == probe_gate.EXIT_PROBE_PASSED


def test_missing_verify_cli_exits_7_not_pending(monkeypatch, tmp_path):
    # End to end through run_cli: the interpreter cannot open the script and exits 2.
    monkeypatch.setattr(probe_gate, "CLI", str(tmp_path / "verify_cli.py"))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, "https://127.0.0.1:9")
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines
    assert not any("PENDING" in line for line in lines)


# The real check after a probe refused on the payee, or naming a third account: only a
# settled receipt for ACCOUNT and EXPECT_RAW approves, and an answer that contradicts the
# probe (or a crash) exits 7.
PAYEE_PROBE = lambda probe: mismatch(probe)  # noqa: E731 - confirmed, pays RAW to PAYEE
THIRD_PROBE = lambda probe: mismatch(probe, got=THIRD)  # noqa: E731


def _real(code, out):
    return lambda args: (code, out(args) if callable(out) else out)


def test_exit_0_without_a_settled_receipt_for_the_payee_approves_nothing():
    for out in ("", "garbage", "[]", "null", '{"settled": true}',
                lambda a: receipt(a, settled=False),
                lambda a: receipt(a, settled="true"),
                lambda a: receipt(a, account=a[0]),
                lambda a: receipt(a, account=probe_gate.PROBE_ACCOUNTS[0]),
                lambda a: receipt(a, account=THIRD),
                lambda a: receipt(a, amount_raw=int(RAW) - 1),
                lambda a: receipt(a, amount_raw=RAW),
                lambda a: receipt(a, verdict="mismatch"),
                lambda a: json.dumps({k: v for k, v in json.loads(receipt(a)).items()
                                      if k != "amount_raw"})):
        run, calls = probe_then(PAYEE_PROBE, real=_real(0, out))
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines, out
        assert len(calls) == 2


def test_exit_0_with_not_after_needs_an_on_time_receipt():
    for out in (lambda a: receipt(a, seen_at=1700000001),
                lambda a: receipt(a, seen_at=None),
                lambda a: json.dumps({k: v for k, v in json.loads(receipt(a)).items()
                                      if k not in ("seen_at", "not_after")})):
        run, _ = probe_then(PAYEE_PROBE, real=_real(0, out))
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run, not_after="1700000000")
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines
    run, _ = probe_then(PAYEE_PROBE)
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run, not_after="1700000000")
    assert code == 0 and lines[-1] == "APPROVE"


def test_settled_receipt_in_xrb_spelling_of_the_payee_approves():
    run, _ = probe_then(PAYEE_PROBE, real=_real(0, lambda a: receipt(a, account="xrb_" + PAYEE[5:])))
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
    assert code == 0 and lines[-1] == "APPROVE"


def test_real_check_contradicting_a_payee_probe_is_7():
    burn = probe_gate.PROBE_ACCOUNTS[0]
    late = lambda account, **kw: json.dumps(dict(  # noqa: E731
        {"verdict": "late", "seen_at": 1700000001, "not_after": 1700000000,
         "amount_raw": int(RAW), "account": account}, **kw))
    for real in ((2, NOT_YET), (3, '{"verdict": "no_such_block"}'),
                 (3, mismatch(PAYEE, got=burn)), (3, mismatch(PAYEE, got=THIRD)),
                 (3, mismatch(RAW, got=str(int(RAW) - 1))), (3, mismatch("send", got="receive")),
                 (5, late(burn)), (5, late(THIRD)), (5, late(PAYEE, amount_raw=1)),
                 (5, late(PAYEE, verdict="unknown_time")), (6, late(PAYEE)),
                 (5, ""), (6, "garbage"), (64, ""), (1, "Traceback (most recent call last):")):
        run, calls = probe_then(PAYEE_PROBE, real=real)
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run, not_after="1700000000")
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines, real
        assert lines[-1].startswith("REAL-CHECK-DISAGREES-WITH-PROBE"), real


def test_late_or_untimed_payment_to_the_payee_after_a_payee_probe_passes_through():
    for code_in, verdict in ((5, "late"), (6, "unknown_time")):
        out = json.dumps({"verdict": verdict, "seen_at": None, "not_after": 1700000000,
                          "amount_raw": int(RAW), "account": PAYEE})
        run, _ = probe_then(PAYEE_PROBE, real=(code_in, out))
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run, not_after="1700000000")
        assert code == code_in and "APPROVE" not in lines


def test_real_check_contradicting_a_third_account_probe_is_7():
    late = json.dumps({"verdict": "late", "seen_at": 1700000001, "not_after": 1700000000,
                       "amount_raw": int(RAW), "account": THIRD})
    for real in ((5, late), (6, late.replace('"late"', '"unknown_time"')), (2, NOT_YET),
                 (64, ""), (1, "Traceback (most recent call last):")):
        run, calls = probe_then(THIRD_PROBE, real=real)
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run, not_after="1700000000")
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines, real
    run, _ = probe_then(THIRD_PROBE, real=(4, '{"verdict": "node_unreachable"}'))
    assert probe_gate.gate(BLOCK, RAW, PAYEE, run=run)[0] == 4


def test_crash_in_the_real_check_exits_7_not_1():
    for probe in (PAYEE_PROBE, THIRD_PROBE):
        run, _ = probe_then(probe, real=(1, "Traceback (most recent call last):\n  boom"))
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines


def _scripted(answers):
    calls = []

    def run(args):
        calls.append(args)
        return answers[len(calls) - 1]
    return run, calls


def test_settled_probe_whose_receipt_does_not_name_the_probe_account_is_7():
    probe = probe_gate.probe_account(PAYEE)
    real = (3, mismatch(PAYEE, got=probe))
    for out in (receipt([BLOCK, RAW, probe], account=PAYEE),
                receipt([BLOCK, RAW, probe], account=THIRD),
                receipt([BLOCK, RAW, probe], account=""),
                '{"settled": true, "amount_raw": %s, "height": 1}' % RAW, ""):
        run, _ = _scripted([(0, out), real])
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines, out
    run, _ = _scripted([(0, receipt([BLOCK, RAW, probe])), real])
    assert probe_gate.gate(BLOCK, RAW, PAYEE, run=run)[0] == 3


def test_not_after_receipt_is_compared_to_the_callers_deadline_not_its_own():
    late = receipt([BLOCK, RAW, PAYEE], seen_at=500, not_after=10 ** 12)
    run, _ = _scripted([(3, mismatch(probe_gate.probe_account(PAYEE))), (0, late)])
    code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run, not_after="100")
    assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines
    on_time = receipt([BLOCK, RAW, PAYEE], seen_at=100, not_after=1)
    run, _ = _scripted([(3, mismatch(probe_gate.probe_account(PAYEE))), (0, on_time)])
    assert probe_gate.gate(BLOCK, RAW, PAYEE, run=run, not_after="100")[0] == 0


def test_deeply_nested_stdout_exits_7_not_1():
    deep = "[" * 100000 + "]" * 100000
    for code_in in (0, 2, 3, 5, 6):
        run, _ = _scripted([(code_in, deep), (0, deep)])
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run)
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines, code_in
    for real_code in (0, 3, 5, 6):
        run, _ = _scripted([(3, mismatch(probe_gate.probe_account(PAYEE))), (real_code, deep)])
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run, not_after="100")
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines, real_code
    rpcs = "https://a.example,https://b.example"
    run, _ = _scripted([(3, mismatch(probe_gate.probe_account(PAYEE), rpc_index=0)), (0, deep)])
    assert probe_gate.gate(BLOCK, RAW, PAYEE, rpcs, run=run)[0] == probe_gate.EXIT_PROBE_PASSED


def test_not_after_receipt_with_seen_at_zero_or_negative_is_7():
    # seen_at <= 0 is "at or before" any deadline, but it is no time at all: approve nothing.
    for seen_at in (0, -1, -1700000000):
        out = receipt([BLOCK, RAW, PAYEE, "--not-after", "1700000000"], seen_at=seen_at)
        run, _ = _scripted([(3, mismatch(probe_gate.probe_account(PAYEE))), (0, out)])
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, run=run, not_after="1700000000")
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines, seen_at
    # The same on a settled probe: its receipt is not one for the probe account on time.
    probe = probe_gate.probe_account(PAYEE)
    out = receipt([BLOCK, RAW, probe, "--not-after", "1700000000"], seen_at=0)
    run, _ = _scripted([(0, out), (3, mismatch(PAYEE, got=probe))])
    assert probe_gate.gate(BLOCK, RAW, PAYEE, run=run, not_after="1700000000")[0] \
        == probe_gate.EXIT_PROBE_PASSED


def test_non_utf8_stdout_from_verify_cli_exits_7_not_1(monkeypatch, tmp_path):
    # End to end through run_cli: a child whose stdout is not UTF-8 is a broken check.
    probe = probe_gate.probe_account(PAYEE)
    cli = tmp_path / "verify_cli.py"
    cli.write_text(
        "import json, sys\n"
        "if sys.argv[3] == %r and sys.argv[4] == 'probe-bad':\n"
        "    sys.stdout.buffer.write(b'\\xff\\xfe{\"settled\": true}'); sys.exit(3)\n"
        "if sys.argv[3] == %r:\n"
        "    print(json.dumps({'verdict': 'mismatch', 'expected': sys.argv[3], 'got': %r}))\n"
        "    sys.exit(3)\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe\\x80'); sys.exit(0)\n" % (probe, probe, PAYEE))
    monkeypatch.setattr(probe_gate, "CLI", str(cli))
    assert probe_gate.run_cli([BLOCK, RAW, PAYEE, "x"])[0] == 1
    for rpc in ("probe-bad", "https://127.0.0.1:9"):
        code, lines = probe_gate.gate(BLOCK, RAW, PAYEE, rpc)
        assert code == probe_gate.EXIT_PROBE_PASSED and "APPROVE" not in lines, rpc
