#!/usr/bin/env python3
"""Approve a Nano payment only if a check built to be refused was refused first, in the same run.

usage: python3 probe_gate.py BLOCK_HASH EXPECT_RAW ACCOUNT [RPC_URL]

A payment check that refused once, in a demo, can quietly stop refusing (a stub node, a cached
"yes", a wrapper that swallows the exit code) and nobody notices, because a working payment
looks the same either way. This keeps the refusal in the path: before the real check it runs
verify_cli.py on the SAME block and amount with a recipient the block was not sent to, and it
approves only when that probe came back `mismatch` (exit 3) AND the real check came back
settled (exit 0). It is a thin wrapper: every rule of verify_cli.py (node URL checks, exit
codes, error text) applies to both calls unchanged.

prints  PROBE-REFUSED, then the real check's JSON verdict, then APPROVE
exit 0  approved
     7  the probe was NOT refused: the check is not doing its job, nothing is approved
     4  the node could not be read on either call (nothing was checked)
     other  the real check's own exit code (2 not confirmed, 3 mismatch, ...); nothing approved
"""
import os
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


def probe_account(account):
    return next(a for a in PROBE_ACCOUNTS if a != account)


def run_cli(args):
    p = subprocess.run([sys.executable, CLI] + list(args), capture_output=True, text=True)
    return p.returncode, p.stdout


def gate(block, expect_raw, account, rpc=None, run=run_cli):
    """Return (exit_code, lines_to_print)."""
    extra = [rpc] if rpc else []
    code, out = run([block, expect_raw, probe_account(account)] + extra)
    if code == 4:
        return 4, [out.strip(), "PROBE-UNREAD (node unreachable; nothing was checked)"]
    if code != 3:
        return EXIT_PROBE_PASSED, [out.strip(), "PROBE-NOT-REFUSED (exit %d): nothing approved" % code]
    lines = ["PROBE-REFUSED"]
    code, out = run([block, expect_raw, account] + extra)
    lines.append(out.strip())
    if code == 0:
        lines.append("APPROVE")
    return code, lines


def main(argv):
    if len(argv) not in (3, 4):
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 64
    code, lines = gate(*argv)
    for line in lines:
        if line:
            print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
