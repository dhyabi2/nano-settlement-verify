"""Two-node verdict: the five fields of TWO-NODE-CHECK.md, read from two independent public nodes.

This adds nothing to `nano_five_fields.five_field_verdict`; it is the command line for it, plus the
one thing the document promises: the curl recipe in TWO-NODE-CHECK.md and the request this module
sends are the same request (`REQUEST_FOR` below is what the document's curl line is tested against).

    python3 nano_two_node.py --block HASH --amount-raw RAW --invoice-id ID \\
        --invoice-issued-at 2026-10-10T15:04:58Z --payee nano_... \\
        [--operator-account nano_...]... [--node URL]... [--json]

Exit 0 settled, 2 unknown (something was not read: ignorance, never a refusal), 4 refused (a field was
read and is wrong: wrong amount, wrong payee, source is destination or on your list, invoice not
before the block) or a malformed block hash. Reads only: no key, no seed, no signing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import nano_five_fields as ff

DEFAULT_NODES = ("https://rpc.nano.to", "https://node.somenano.com/proxy")
SETTLED_MEANS = "I can start work. This is NOT buyer_accept."
_HEX64 = re.compile(r"[0-9A-Fa-f]{64}")


def REQUEST_FOR(block_hash: str) -> dict:
    """The exact JSON body sent to every node (and the one TWO-NODE-CHECK.md's curl sends)."""
    return {"action": "block_info", "json_block": "true", "hash": block_hash}


def two_node_verdict(block_hash, amount_raw, invoice_id, invoice_issued_at, payee,
                     operator_accounts=(), nodes=DEFAULT_NODES, fetch=None) -> dict:
    if not isinstance(block_hash, str) or not _HEX64.fullmatch(block_hash):
        return {"v": "two-node-verdict-v1", "verdict": "refused",
                "why": "block hash is not 64 hex characters; no node was asked",
                "settled_means": None}
    if isinstance(amount_raw, str) and amount_raw.isdigit():
        expect = int(amount_raw)           # integer string, never float
    else:
        expect = None                      # five_field_verdict marks the field missing
    reads = ff.read_nodes(block_hash, list(nodes), fetch=fetch)
    r = ff.five_field_verdict(block_hash, reads, expect_raw=expect, invoice_id=invoice_id,
                              issued_at=invoice_issued_at, payee=payee,
                              exclude=tuple(operator_accounts))
    out = {"v": "two-node-verdict-v1", "verdict": r["verdict"], "block_hash": r["block_hash"],
           "fields": r["fields"], "missing_fields": r["missing"],
           "operator_list_empty": len(tuple(operator_accounts)) == 0,
           "nodes_read": r["fields"]["nodes_read"].get("read", []),
           "settled_means": SETTLED_MEANS if r["verdict"] == "settled" else None}
    return out


EXIT = {"settled": 0, "unknown": 2, "refused": 4}


def main(argv=None, fetch=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--block", required=True)
    p.add_argument("--amount-raw", required=True, help="exact tagged amount quoted, integer raw")
    p.add_argument("--invoice-id", required=True)
    p.add_argument("--invoice-issued-at", required=True)
    p.add_argument("--payee", required=True)
    p.add_argument("--operator-account", action="append", default=[])
    p.add_argument("--node", action="append")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    v = two_node_verdict(a.block, a.amount_raw, a.invoice_id, a.invoice_issued_at, a.payee,
                         a.operator_account, a.node or DEFAULT_NODES, fetch=fetch)
    print(json.dumps(v, indent=None if a.json else 1))
    return EXIT[v["verdict"]]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
