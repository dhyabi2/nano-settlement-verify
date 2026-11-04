#!/usr/bin/env python3
"""Approve a Nano payment only if a check built to be refused was refused first, in the same run.

usage: python3 probe_gate.py BLOCK_HASH EXPECT_RAW ACCOUNT [RPC_URL] [--not-after WHEN]

A payment check that refused once, in a demo, can quietly stop refusing (a wrapper that
swallows the exit code, a check that no longer compares the payee, a node that answers
"confirmed" for anything) and nobody notices, because a working payment looks the same
either way. This keeps the refusal in the path: before the real check it runs verify_cli.py
on the SAME block and amount with a recipient the block was not sent to, and it approves
only when that probe came back `mismatch` (exit 3) on the payee - its "expected" is the
probe account and its "got" is ACCOUNT - AND the real check came back settled (exit 0) with
a receipt that says so: "settled" true, "account" ACCOUNT and "amount_raw" EXPECT_RAW (and,
with --not-after, "seen_at" at or before "not_after"); an exit 0 with any other output exits
7. After such a probe the only other answers the real check may give are 4 (node unreadable)
and 5/6 (late/untimed) naming ACCOUNT and EXPECT_RAW; anything else (not yet, no such block,
a mismatch, a late verdict naming another account, a crash) contradicts the probe and exits 7. A
payee mismatch that names any other recipient is not a passing probe: one that names the
probe account itself (or no account) contradicts itself and exits 7; one that names a third
account is a payment to that account only if the real check, on the same node, refuses it as
paid to that same account (exit 3); exit 4 passes through, and any other answer exits 7. Any other exit 3 on the
probe is not a refusal of the payee: no_such_block exits 3 like verify_cli.py ("do not
serve"; asking again does not create the block), invalid_amount exits 7. A probe refused on a
field checked before the payee - the amount (an honest underpayment to the right payee) or
the operation (not a send) - never reached the payee; the real check is then run on the same
node and, if it refuses with the same mismatch, the gate exits 3 like any mismatch. It is a thin wrapper: every rule of verify_cli.py (node URL checks, exit codes,
error text, --not-after) applies to both calls unchanged.

A probe that came back settled is a probe that was not refused, with one honest cause: the
block really pays the probe account (a payment sent to the burn address, checked for a
seller). The real check is then run on the same node; if it refuses the block as a payee
mismatch naming the probe account as the recipient, that is a wrong-recipient payment and
the gate exits 3. Nothing in that path can approve: any other answer exits 7.

Both calls are answered by ONE node. With an RPC_URL list (or a list in $NANO_RPC_URL) the
probe may fail over as verify_cli.py does; the real check is then sent only to the node that
answered the probe (its "rpc_index"), never to the rest of the list, so a node that refused
the probe cannot hand the real check to one that says yes to anything.

What it does not catch: only the recipient is varied, so a node that answers the true amount
and payee for this block (a stub written for this payment, or a reply about another block
that paid the same amount to the same payee) passes the probe and the check alike. It proves
the check still refuses a wrong payee on this node, not that the node is honest; agreement
of independent nodes is nano_quorum's job.

The same holds when the probe pays the probe account but fails only on time: with
--not-after, a late (5) or untimed (6) probe whose verdict names the probe account as its
"account" is checked the same way, and exits 3 when the real check refuses it as paid to
the probe account; a 5 or 6 that names any other account, or none, exits 7.

prints  PROBE-REFUSED, then the real check's JSON verdict, then APPROVE
exit 0  approved
     2  the block is not confirmed yet (on the probe): only when verify_cli.py printed
        its not-confirmed receipt ("settled": false). Any other exit 2 (Python could not
        open verify_cli.py, say), and any exit 2 from the real check after the probe was
        answered, exits 7; nothing approved
     3  refused: wrong amount (too much or too little) or not a send, the same on the
        probe and the real check; a payment to the wrong recipient (also when it was sent
        to the probe account itself); or the probe's node has no such block. Terminal: do
        not serve, asking again gives the same answer; nothing approved
     7  the probe was NOT refused on the payee, or the two calls could not be pinned to one
        node, or the real check disagreed with the probe, or exited 0 without a settled
        receipt for ACCOUNT and EXPECT_RAW, or crashed (exit 1, or any code not listed
        here): the check is not doing its job, nothing is approved
     4  the node could not be read (nothing was checked)
     5  late, 6 unknown_time: the real check's own verdict after a probe refused on the
        payee, only when it names ACCOUNT and EXPECT_RAW; nothing approved
    64  usage error (from this script or from verify_cli.py on the probe)
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "verify_cli.py")
# Two valid accounts that are nobody's payee: the well-known burn address and a second
# well-known one, so the probe recipient always differs from the real one.
PROBE_ACCOUNTS = (
    "nano_1111111111111111111111111111111111111111111111111111hifc8npp",
    "nano_1natrium1o3z5519ifou7xii8crpxpk8y65qmkih8e8bpsjri651oza8imdd",
)
EXIT_PROBE_PASSED = 7
_PREFIXES = ("nano_", "xrb_")
# verify_cli.py splits RPC_URL on commas only when every piece starts with a scheme.
_STARTS_WITH_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


def _account_body(account):
    """The account without its nano_/xrb_ prefix: the two spellings name one account."""
    for prefix in _PREFIXES:
        if account.startswith(prefix):
            return account[len(prefix):]
    return account


def probe_account(account):
    body = _account_body(account)
    return next(a for a in PROBE_ACCOUNTS if _account_body(a) != body)


def run_cli(args):
    p = subprocess.run([sys.executable, CLI] + list(args), capture_output=True, text=True)
    return p.returncode, p.stdout


def _rpc_list(rpc):
    """The nodes verify_cli.py would ask for this RPC_URL, in order (None: its default)."""
    if rpc is None:
        rpc = (os.environ.get("NANO_RPC_URL") or "").strip() or None
        if rpc is None:
            return None
    rpc = rpc.strip()
    if "," in rpc:
        parts = [part.strip() for part in rpc.split(",")]
        if all(_STARTS_WITH_SCHEME.match(part) or not part for part in parts):
            return parts
    return [rpc]


def _verdict(out):
    """The parsed JSON object verify_cli.py printed, or None."""
    try:
        verdict = json.loads(out)
    except ValueError:
        return None
    return verdict if isinstance(verdict, dict) else None


def _not_yet(out):
    """True only for verify_cli.py's exit-2 receipt: a block not confirmed yet."""
    verdict = _verdict(out)
    return verdict is not None and "verdict" not in verdict and verdict.get("settled") is False


def _pays_probe_late(out, probe):
    """True when a probe's exit-5/6 verdict (late, unknown_time) names the probe account
    as the block's recipient: the payee matched the probe; only the time failed."""
    verdict = _verdict(out)
    if verdict is None or verdict.get("verdict") not in ("late", "unknown_time"):
        return False
    account = verdict.get("account")
    return (isinstance(account, str) and account.startswith(_PREFIXES)
            and _account_body(account) == _account_body(probe))


def _rpc_index(out):
    try:
        verdict = json.loads(out)
    except ValueError:
        return None
    index = verdict.get("rpc_index") if isinstance(verdict, dict) else None
    return index if type(index) is int else None


def _payee_refusal(out, probe, account):
    """What the probe's exit-3 verdict refused on: "payee" only for a mismatch whose
    expected account is the probe account and whose reported recipient (`got`) is ACCOUNT,
    "elsewhere" for one whose `got` is a third account (neither the probe nor ACCOUNT),
    "no_such_block", or None for anything else (a `got` equal to the probe account, or
    not an account at all, contradicts the mismatch)."""
    try:
        verdict = json.loads(out)
    except ValueError:
        return None
    if not isinstance(verdict, dict):
        return None
    if verdict.get("verdict") == "no_such_block":
        return "no_such_block"
    expected, got = verdict.get("expected"), verdict.get("got")
    if (verdict.get("verdict") == "mismatch" and isinstance(expected, str)
            and expected.startswith(_PREFIXES) and _account_body(expected) == _account_body(probe)):
        if not isinstance(got, str) or not got.startswith(_PREFIXES):
            return None
        if _account_body(got) == _account_body(expected):
            return None
        return "payee" if _account_body(got) == _account_body(account) else "elsewhere"
    return None


def _earlier_mismatch(out, expect_raw):
    """The probe's exit-3 verdict as (expected, got) when it is a mismatch on a field
    verify checks BEFORE the payee - the amount (expected is EXPECT_RAW) or the
    operation (expected is "send") - else None. Such a probe never reached the payee
    comparison, so it neither passed nor refused it."""
    try:
        verdict = json.loads(out)
    except ValueError:
        return None
    if not isinstance(verdict, dict) or verdict.get("verdict") != "mismatch":
        return None
    expected, got = verdict.get("expected"), verdict.get("got")
    if not isinstance(expected, str) or not isinstance(got, str):
        return None
    try:
        amount = str(int(expect_raw))
    except ValueError:
        return None
    if expected in (amount, "send") and got != expected:
        return expected, got
    return None


def _pinned(nodes, out):
    """The RPC_URL arguments for the real check, pinned to the node that answered the
    probe; None when a list's probe carried no usable rpc_index."""
    if nodes is not None and len(nodes) > 1:
        index = _rpc_index(out)
        if index is None or not 0 <= index < len(nodes):
            return None
        return [nodes[index]]
    return nodes if nodes is not None else []


def _paid_to(out, account, probe):
    """True when a real-check exit-3 verdict is a payee mismatch that expected ACCOUNT and
    got the probe account: the block pays the probe account, not the payee."""
    try:
        verdict = json.loads(out)
    except ValueError:
        return False
    if not isinstance(verdict, dict) or verdict.get("verdict") != "mismatch":
        return False
    expected, got = verdict.get("expected"), verdict.get("got")
    return (isinstance(expected, str) and isinstance(got, str)
            and expected.startswith(_PREFIXES) and got.startswith(_PREFIXES)
            and _account_body(expected) == _account_body(account)
            and _account_body(got) == _account_body(probe))


def _same_account(value, account):
    return (isinstance(value, str) and value.startswith(_PREFIXES)
            and _account_body(value) == _account_body(account))


def _same_amount(value, expect_raw):
    try:
        return type(value) is int and value == int(expect_raw)
    except ValueError:
        return False


def _settled_receipt(out, expect_raw, account, timed):
    """True only for verify_cli.py's exit-0 receipt of this payment: "settled" true,
    "amount_raw" EXPECT_RAW and "account" ACCOUNT, and, with --not-after, "seen_at"
    at or before "not_after"."""
    receipt = _verdict(out)
    if receipt is None or "verdict" in receipt or receipt.get("settled") is not True:
        return False
    if not (_same_account(receipt.get("account"), account)
            and _same_amount(receipt.get("amount_raw"), expect_raw)):
        return False
    if timed:
        seen_at, deadline = receipt.get("seen_at"), receipt.get("not_after")
        if type(seen_at) is not int or type(deadline) is not int or seen_at > deadline:
            return False
    return True


def _late_for(out, code, expect_raw, account):
    """True when an exit-5/6 verdict is late/unknown_time for EXPECT_RAW to ACCOUNT."""
    verdict = _verdict(out)
    return (verdict is not None
            and verdict.get("verdict") == {5: "late", 6: "unknown_time"}[code]
            and _same_account(verdict.get("account"), account)
            and _same_amount(verdict.get("amount_raw"), expect_raw))


def _probe_settled(block, expect_raw, account, probe, nodes, out, run, tail, probe_code=0):
    """The probe matched the payee: settled (exit 0), or late/untimed (5/6) naming the
    probe account. Exit 3 only when the real check, on the same node, refuses the block
    as paid to the probe account; never approve."""
    not_refused = [out.strip(), "PROBE-NOT-REFUSED (exit %d): nothing approved" % probe_code]
    pinned = _pinned(nodes, out)
    if pinned is None:
        return EXIT_PROBE_PASSED, not_refused
    code, real = run([block, expect_raw, account] + pinned + tail)
    if code == 3 and _paid_to(real, account, probe):
        state = ("PROBE-SETTLED" if probe_code == 0
                 else "PROBE-PAYEE-MATCHED-BUT-NOT-ON-TIME (exit %d)" % probe_code)
        return 3, [out.strip(), state + " (the block pays the probe account)", real.strip(),
                   "WRONG-RECIPIENT: nothing approved"]
    return EXIT_PROBE_PASSED, not_refused + [real.strip(),
                                             "REAL-CHECK-DOES-NOT-EXPLAIN-PROBE (exit %d)" % code]


def gate(block, expect_raw, account, rpc=None, run=run_cli, not_after=None):
    """Return (exit_code, lines_to_print)."""
    tail = ["--not-after", not_after] if not_after is not None else []
    nodes = _rpc_list(rpc)
    extra = [rpc] if rpc is not None else []
    probe = probe_account(account)
    code, out = run([block, expect_raw, probe] + extra + tail)
    if code == 4:
        return 4, [out.strip(), "PROBE-UNREAD (node unreachable; nothing was checked)"]
    if code == 2:
        if not _not_yet(out):
            return EXIT_PROBE_PASSED, [
                out.strip(), "PROBE-EXIT-2-WITHOUT-RECEIPT (not a not-confirmed verdict): nothing approved"]
        return 2, [out.strip(), "PROBE-PENDING (block not confirmed yet; nothing approved)"]
    if code == 64:
        return 64, [out.strip(), "PROBE-USAGE-ERROR (exit 64): nothing approved"]
    if code == 0 or (code in (5, 6) and _pays_probe_late(out, probe)):
        # Settled, or late/untimed with the probe account as recipient: either way the
        # probe matched the payee, so only a payment to the probe account explains it.
        return _probe_settled(block, expect_raw, account, probe, nodes, out, run, tail, code)
    if code != 3:
        return EXIT_PROBE_PASSED, [out.strip(), "PROBE-NOT-REFUSED (exit %d): nothing approved" % code]
    refusal = _payee_refusal(out, probe, account)
    if refusal == "no_such_block":
        # verify_cli.py's own exit 3, "do not serve": a made-up or wrong hash stays
        # missing, so a caller that retries on 2 must not be told to wait for it.
        return 3, [out.strip(), "PROBE-BLOCK-NOT-FOUND (no such block on this node; nothing approved)"]
    earlier = _earlier_mismatch(out, expect_raw) if refusal != "payee" else None
    if earlier is not None:
        # The block is refused before its payee is read: wrong amount (an honest
        # underpayment to the right payee) or not a send. That is the real check's
        # mismatch, exit 3, not a broken check - but only when the real check, on the
        # same node, refuses with the same mismatch. Anything else approves nothing.
        pinned = _pinned(nodes, out)
        if pinned is None:
            return EXIT_PROBE_PASSED, [out.strip(),
                                       "PROBE-NODE-UNKNOWN (no rpc_index to pin to): nothing approved"]
        probe_out = out.strip()
        code, out = run([block, expect_raw, account] + pinned + tail)
        lines = [probe_out, "PROBE-MISMATCH-BEFORE-PAYEE (expected %r, got %r)" % earlier, out.strip()]
        if code == 3 and _earlier_mismatch(out, expect_raw) == earlier:
            return 3, lines + ["MISMATCH: nothing approved"]
        return EXIT_PROBE_PASSED, lines + [
            "REAL-CHECK-DISAGREES-WITH-PROBE (exit %d): nothing approved" % code]
    if refusal == "elsewhere":
        # The probe names a third account as the recipient: not a passing probe. It is a
        # wrong-recipient payment (exit 3) only if the real check, on the same node, says
        # the block pays that same account; it can never approve.
        pinned = _pinned(nodes, out)
        if pinned is None:
            return EXIT_PROBE_PASSED, [out.strip(),
                                       "PROBE-NODE-UNKNOWN (no rpc_index to pin to): nothing approved"]
        got = json.loads(out)["got"]
        probe_out = out.strip()
        code, out = run([block, expect_raw, account] + pinned + tail)
        lines = [probe_out, "PROBE-PAID-ELSEWHERE (got %s)" % got, out.strip()]
        if code == 3 and _paid_to(out, account, got):
            return 3, lines + ["WRONG-RECIPIENT: nothing approved"]
        if code == 4:
            return 4, lines
        # Settled, not yet, late, untimed, another mismatch or a crash: none of them is
        # "this block pays that account", which the probe said.
        return EXIT_PROBE_PASSED, lines + [
            "REAL-CHECK-DISAGREES-WITH-PROBE (exit %d): nothing approved" % code]
    if refusal != "payee":
        # Exit 3 for any other reason (a wrong amount or operation, an amount that is
        # not raw, unreadable JSON) says nothing about whether the payee is compared.
        return EXIT_PROBE_PASSED, [out.strip(),
                                   "PROBE-NOT-REFUSED-ON-PAYEE (exit 3, not a payee mismatch): nothing approved"]
    # Pin the real check to the node that refused the probe, passed explicitly
    # so neither the rest of the list nor $NANO_RPC_URL can answer it instead.
    pinned = _pinned(nodes, out)
    if pinned is None:
        return EXIT_PROBE_PASSED, [out.strip(),
                                   "PROBE-NODE-UNKNOWN (no rpc_index to pin to): nothing approved"]
    lines = ["PROBE-REFUSED"]
    code, out = run([block, expect_raw, account] + pinned + tail)
    lines.append(out.strip())
    if code == 0 and _rpc_index(out) is not None:
        # A single pinned URL never carries rpc_index; one that does was not pinned.
        return EXIT_PROBE_PASSED, lines + ["REAL-CHECK-NOT-PINNED: nothing approved"]
    if code == 0:
        if not _settled_receipt(out, expect_raw, account, not_after is not None):
            # Exit 0 alone is not a settlement: the receipt must say settled, for
            # ACCOUNT and EXPECT_RAW (and on time, with --not-after).
            return EXIT_PROBE_PASSED, lines + [
                "REAL-CHECK-EXIT-0-WITHOUT-RECEIPT (not a settled receipt for this payment): nothing approved"]
        return 0, lines + ["APPROVE"]
    if code == 4 or (code in (5, 6) and _late_for(out, code, expect_raw, account)):
        # The node could not be read, or the payment to ACCOUNT failed only on time
        # (checked after the payee, so the probe never reached it).
        return code, lines
    # The probe said the block is confirmed and pays EXPECT_RAW to ACCOUNT; any other
    # answer from the same node (not yet, no such block, a mismatch, a late or untimed
    # verdict naming another account, a crash) contradicts it.
    return EXIT_PROBE_PASSED, lines + [
        "REAL-CHECK-DISAGREES-WITH-PROBE (exit %d): nothing approved" % code]


def _take_not_after(argv):
    rest, value, i = [], None, 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--not-after" and i + 1 < len(argv):
            value, i = argv[i + 1], i + 2
            continue
        if arg.startswith("--not-after="):
            value = arg.split("=", 1)[1]
        else:
            rest.append(arg)
        i += 1
    return rest, value


def main(argv):
    argv, not_after = _take_not_after(argv)
    if len(argv) not in (3, 4):
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 64
    code, lines = gate(*argv, not_after=not_after)
    for line in lines:
        if line:
            print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
