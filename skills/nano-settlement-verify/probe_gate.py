#!/usr/bin/env python3
"""Approve a Nano payment only if a check built to be refused was refused first, in the same run.

usage: python3 probe_gate.py BLOCK_HASH EXPECT_RAW ACCOUNT [RPC_URL] [--not-after WHEN]

A payment check that refused once, in a demo, can quietly stop refusing (a wrapper that
swallows the exit code, a check that no longer compares the payee, a node that answers
"confirmed" for anything) and nobody notices, because a working payment looks the same
either way. This keeps the refusal in the path: before the real check it runs verify_cli.py
on the SAME block and amount with a recipient the block was not sent to, and it approves
only when that probe came back `mismatch` (exit 3) AND the real check came back settled
(exit 0). It is a thin wrapper: every rule of verify_cli.py (node URL checks, exit codes,
error text, --not-after) applies to both calls unchanged.

Both calls are answered by ONE node. With an RPC_URL list (or a list in $NANO_RPC_URL) the
probe may fail over as verify_cli.py does; the real check is then sent only to the node that
answered the probe (its "rpc_index"), never to the rest of the list, so a node that refused
the probe cannot hand the real check to one that says yes to anything.

What it does not catch: only the recipient is varied, so a node that answers the true amount
and payee for this block (a stub written for this payment, or a reply about another block
that paid the same amount to the same payee) passes the probe and the check alike. It proves
the check still refuses a wrong payee on this node, not that the node is honest; agreement
of independent nodes is nano_quorum's job.

prints  PROBE-REFUSED, then the real check's JSON verdict, then APPROVE
exit 0  approved
     2  the block is not confirmed yet (on the probe or the real check); nothing approved
     7  the probe was NOT refused, or the two calls could not be pinned to one node:
        the check is not doing its job, nothing is approved
     4  the node could not be read (nothing was checked)
     other  the real check's own exit code (3 mismatch, 5 late, ...); nothing approved
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


def _rpc_index(out):
    try:
        verdict = json.loads(out)
    except ValueError:
        return None
    index = verdict.get("rpc_index") if isinstance(verdict, dict) else None
    return index if type(index) is int else None


def gate(block, expect_raw, account, rpc=None, run=run_cli, not_after=None):
    """Return (exit_code, lines_to_print)."""
    tail = ["--not-after", not_after] if not_after is not None else []
    nodes = _rpc_list(rpc)
    extra = [rpc] if rpc is not None else []
    code, out = run([block, expect_raw, probe_account(account)] + extra + tail)
    if code == 4:
        return 4, [out.strip(), "PROBE-UNREAD (node unreachable; nothing was checked)"]
    if code == 2:
        return 2, [out.strip(), "PROBE-PENDING (block not confirmed yet; nothing approved)"]
    if code != 3:
        return EXIT_PROBE_PASSED, [out.strip(), "PROBE-NOT-REFUSED (exit %d): nothing approved" % code]
    if nodes is not None and len(nodes) > 1:
        # Pin the real check to the node that refused the probe, passed explicitly
        # so neither the rest of the list nor $NANO_RPC_URL can answer it instead.
        index = _rpc_index(out)
        if index is None or not 0 <= index < len(nodes):
            return EXIT_PROBE_PASSED, [out.strip(),
                                       "PROBE-NODE-UNKNOWN (no rpc_index to pin to): nothing approved"]
        pinned = [nodes[index]]
    elif nodes is not None:
        pinned = nodes
    else:
        pinned = []
    lines = ["PROBE-REFUSED"]
    code, out = run([block, expect_raw, account] + pinned + tail)
    lines.append(out.strip())
    if code == 0 and _rpc_index(out) is not None:
        # A single pinned URL never carries rpc_index; one that does was not pinned.
        return EXIT_PROBE_PASSED, lines + ["REAL-CHECK-NOT-PINNED: nothing approved"]
    if code == 0:
        lines.append("APPROVE")
    return code, lines


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
