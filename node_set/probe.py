#!/usr/bin/env python3
"""Probe public Nano RPC nodes with one known block and write what was measured to NODES.json.

    python3 node_set/probe.py --out node_set/NODES.json [--candidate URL ...] [--timeout S] [--probed-from LABEL]

Read-only: one block_info call per candidate. Exit 0 when at least one node is usable, 3 when none is
(a measurement, not a failure to run), 5 when --out cannot be written (any old file is left untouched).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

PROBE_BLOCK = "B749B757EE750FC9AEA72F33CB429EACCD2ABEC9F2CCF59BF17AFAC304C9A58F"
PROBE_AMOUNT = "500000000000000000000000000"
UA = "Mozilla/5.0 (compatible; nano-settlement-verify-node-probe)"
DEFAULT_CANDIDATES = (
    "https://rpc.nano.to",
    "https://proxy.nanos.cc/proxy",
    "https://node.somenano.com/proxy",
    "https://rainstorm.city/api",
    "https://nanoslo.0x.no/proxy",
    "https://www.bitrequest.app:8020",
)


class HttpStatus(Exception):
    def __init__(self, code):
        self.code = code


def http_post(url, body, timeout):
    """The only network call. Returns (status, text); raises on connection failure."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(1 << 20).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def check(url, post=http_post, timeout=15):
    """-> (usable, why, http)"""
    body = {"action": "block_info", "json_block": "true", "hash": PROBE_BLOCK}
    try:
        status, text = post(url, body, timeout)
    except TimeoutError:
        return False, "timeout", None
    except OSError as e:
        if "timed out" in str(e):
            return False, "timeout", None
        return False, "connect_rejected", None
    if status != 200:
        return False, f"http_{status}", status
    try:
        d = json.loads(text)
    except ValueError:
        return False, "bad_json", status
    if not isinstance(d, dict):
        return False, "bad_json", status
    if "error" in d:
        return False, "rpc_error", status
    if d.get("confirmed") not in ("true", True):
        return False, "not_confirmed", status
    if d.get("amount") != PROBE_AMOUNT:
        return False, "wrong_amount", status
    return True, None, status


def probe(candidates, post=http_post, timeout=15, probed_from="unknown", now=None):
    seen, usable, unusable = set(), [], []
    for u in candidates:
        if u in seen:
            continue
        seen.add(u)
        ok, why, status = check(u, post, timeout)
        if ok:
            usable.append(u)
        else:
            unusable.append({"url": u, "why": why, "http": status})
    return {
        "v": "node-set-v1",
        "probed_at": now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "probe_block": PROBE_BLOCK,
        "probed_from": probed_from,
        "usable": usable,
        "unusable": unusable,
        "usable_count": len(usable),
        "two_node_check_possible": len(usable) >= 2,
    }


def main(argv=None, post=http_post) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", required=True)
    p.add_argument("--candidate", action="append")
    p.add_argument("--timeout", type=float, default=15)
    p.add_argument("--probed-from", default="unknown")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    result = probe(a.candidate or DEFAULT_CANDIDATES, post, a.timeout, a.probed_from)
    try:
        d = os.path.dirname(os.path.abspath(a.out))
        fd, tmp = tempfile.mkstemp(dir=d)
        with os.fdopen(fd, "w") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
        os.replace(tmp, a.out)
    except OSError as e:
        print(f"cannot write {a.out}: {e}", file=sys.stderr)
        return 5
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"usable {result['usable_count']}/{len(result['usable']) + len(result['unusable'])}: {result['usable']}")
    return 0 if result["usable"] else 3


if __name__ == "__main__":
    sys.exit(main())
