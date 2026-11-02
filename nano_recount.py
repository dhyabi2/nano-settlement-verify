"""Stranger recount: one address in, a countable number out, on either rail. Keyless, read-only.

    python3 nano_recount.py --address nano_...|0x... [--network nano:mainnet|eip155:8453]
        [--node URL]... [--from-block N] [--exclude-self ADDR]... [--rule-stated-at ISO] [--json]

Counts distinct sending accounts that paid an address and their confirmed payments: no clustering, no
identity resolution (`method_is_an_inference` is always false here). A truncated read REFUSES (exit 3,
`range_incomplete`); it never prints a smaller number. Exit: 0 counted (zero is an answer), 2 bad
checksum, 3 node unreachable or range incomplete, 64 usage / unsupported rail.

Nano:  nano_payers.payers (account_history walked to the open block).
Base USDC (eip155:8453): eth_getLogs over the USDC contract, Transfer(to = address), paged in windows
the node accepts. The read is heavier than Nano's (a filtered log range over a shared contract, not the
account's own chain) and `--from-block` is required, because starting at block 0 is thousands of calls.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request

import nano_payers
import nano_quorum
import nano_settlement_verify
from nano_settlement_verify import is_valid_account

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DEFAULT_EVM_NODE = "https://base-rpc.publicnode.com"
SUPPORTED = ("nano:mainnet/XNO", "eip155:8453/USDC")
WINDOW = 10_000
MIN_WINDOW = 500


class Refusal(Exception):
    def __init__(self, code, exit_code, detail=""):
        super().__init__(f"{code}: {detail}")
        self.code, self.exit_code, self.detail = code, exit_code, detail


# --- keccak-256 (hashlib's sha3_256 is NIST SHA-3, a different padding) --------------------------
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
       0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
       0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
       0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _rol(x, n):
    return ((x << n) | (x >> (64 - n))) & _M if n else x


def keccak256(data: bytes) -> bytes:
    st = [[0] * 5 for _ in range(5)]
    rate = 136
    p = bytearray(data) + b"\x01"
    p += b"\x00" * (-len(p) % rate)
    p[-1] |= 0x80
    for off in range(0, len(p), rate):
        for i in range(rate // 8):
            st[i % 5][i // 5] ^= int.from_bytes(p[off + 8 * i: off + 8 * i + 8], "little")
        for rc in _RC:
            c = [st[x][0] ^ st[x][1] ^ st[x][2] ^ st[x][3] ^ st[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
            st = [[st[x][y] ^ d[x] for y in range(5)] for x in range(5)]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    b[y][(2 * x + 3 * y) % 5] = _rol(st[x][y], _ROT[x][y])
            st = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
            st[0][0] ^= rc
    out = b"".join(st[i % 5][i // 5].to_bytes(8, "little") for i in range(rate // 8))
    return out[:32]


TRANSFER_TOPIC = "0x" + keccak256(b"Transfer(address,address,uint256)").hex()


def eip55_valid(addr: str) -> bool:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        return False
    body = addr[2:]
    if body == body.lower() or body == body.upper():
        return True                      # not mixed-case: carries no checksum
    h = keccak256(body.lower().encode()).hex()
    return all((c.upper() if int(h[i], 16) >= 8 else c.lower()) == c for i, c in enumerate(body) if c.isalpha())


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": nano_settlement_verify.USER_AGENT})
    with urllib.request.urlopen(req, timeout=nano_settlement_verify.RPC_TIMEOUT_S) as r:
        return json.loads(r.read().decode())


def _evm_count(address, nodes, from_block, exclude, fetch):
    fetch = fetch or _post
    node = nodes[0]
    me = address.lower()
    skip = {e.lower() for e in exclude}

    def rpc(method, params):
        try:
            r = fetch(node, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except (OSError, ValueError) as e:   # HTTPError (429, 5xx), timeouts, non-JSON
            if method == "eth_getLogs":
                return {"error": f"{type(e).__name__}: {e}"}
            raise Refusal("node_unreachable", 3, f"{type(e).__name__}: {e}")
        if not isinstance(r, dict):
            raise Refusal("node_unreachable", 3, "reply is not JSON-RPC")
        return r

    head = rpc("eth_blockNumber", [])
    if "result" not in head:
        raise Refusal("node_unreachable", 3, str(head.get("error"))[:120])
    latest = int(head["result"], 16)
    topic_to = "0x" + "0" * 24 + me[2:]
    payers, total, stamps, covered = {}, 0, [], from_block
    self_sends, lo, size = 0, from_block, WINDOW
    while lo <= latest:
        hi = min(lo + size - 1, latest)
        r = rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi), "address": USDC_BASE,
                                 "topics": [TRANSFER_TOPIC, None, topic_to]}])
        if "error" in r or not isinstance(r.get("result"), list):
            if size > MIN_WINDOW:
                size //= 2                # the node refused this width: retry narrower
                continue
            raise Refusal("range_incomplete", 3, f"covered blocks {from_block}..{covered - 1}; "
                          f"node refused {lo}..{hi}: {str(r.get('error'))[:120]}")
        for lg in r["result"]:
            src = "0x" + lg["topics"][1][-40:]
            if src.lower() == me or src.lower() in skip:
                self_sends += 1
                continue
            payers.setdefault(src.lower(), 0)
            payers[src.lower()] += 1
            total += int(lg["data"], 16)
            stamps.append(int(lg["blockNumber"], 16))
        covered, lo = hi + 1, hi + 1
    return {"distinct_payers": len(payers), "payments": sum(payers.values()),
            "total_received": _fmt(total, 6), "first_payment_at": None, "last_payment_at": None,
            "first_block": min(stamps) if stamps else None, "last_block": max(stamps) if stamps else None,
            "excluded": {"self_sends": self_sends, "unconfirmed": 0, "below_dust": 0},
            "source": {"node": node, "key_in_path": False, "range": f"blocks {from_block}..{latest}"},
            "method": "every Transfer log of the USDC contract to this address in the range, counted by "
                      "distinct sending account; eth_getLogs returns only included transactions; no "
                      "clustering, no identity resolution"}


def _fmt(raw, decimals):
    s = str(raw).rjust(decimals + 1, "0")
    whole, frac = s[:-decimals], s[-decimals:].rstrip("0")
    return whole + ("." + frac if frac else "")


def _nano_count(address, nodes, exclude):
    try:
        rep = nano_payers.payers(address, nodes)
    except nano_payers.HistoryIncomplete as e:
        raise Refusal("range_incomplete", 3, str(e))
    except nano_quorum.NotCorroborated as e:
        raise Refusal("node_unreachable", 3, str(e))
    if exclude:
        rep = nano_payers.outside_payers(rep, exclude)
    firsts = [p.first_timestamp for p in rep.payers if p.first_timestamp]
    lasts = [p.last_timestamp for p in rep.payers if p.last_timestamp]
    return {"distinct_payers": rep.distinct_payers, "payments": rep.payments,
            "total_received": _fmt(rep.received_raw, 30),
            "first_payment_at": _iso(min(firsts)) if firsts else None,
            "last_payment_at": _iso(max(lasts)) if lasts else None,
            "excluded": {"self_sends": rep.self_payments, "unconfirmed": rep.unconfirmed_payments,
                         "below_dust": 0},
            "source": {"node": rep.asked[0] if rep.asked else nodes[0], "key_in_path": False,
                       "range": "full history"},
            "method": "every confirmed receive on the account, counted by distinct sending account; "
                      "no clustering, no identity resolution; times are when the payee pocketed"}


def recount(address, network=None, asset=None, nodes=None, from_block=None, exclude=(),
            rule_stated_at=None, fetch=None, now=None) -> dict:
    nano = isinstance(address, str) and address.startswith(("nano_", "xrb_"))
    evm = isinstance(address, str) and address.startswith("0x")
    if not (nano or evm):
        raise Refusal("address_checksum_invalid", 2, "not a nano_ or 0x address")
    network = network or ("nano:mainnet" if nano else "eip155:8453")
    asset = asset or ("XNO" if network.startswith("nano") else "USDC")
    if (nano and network != "nano:mainnet") or (evm and network != "eip155:8453"):
        raise Refusal("usage", 64, f"address shape and --network {network} disagree")
    if f"{network}/{asset}" not in SUPPORTED:
        raise Refusal("unsupported_rail", 64, "supported: " + ", ".join(SUPPORTED))
    if nano and not is_valid_account(address):
        raise Refusal("address_checksum_invalid", 2, address)
    if evm and not eip55_valid(address):
        raise Refusal("address_checksum_invalid", 2, address)
    for e in exclude:
        if (nano and not is_valid_account(e)) or (evm and not eip55_valid(e)):
            raise Refusal("address_checksum_invalid", 2, f"--exclude-self {e}")
    as_of = _iso(now if now is not None else time.time())
    if nano:
        core = _nano_count(address, nodes or ["https://rpc.nano.to"], list(exclude))
    else:
        if from_block is None:
            raise Refusal("usage", 64, "--from-block is required on eip155:8453")
        core = _evm_count(address, nodes or [DEFAULT_EVM_NODE], from_block, exclude, fetch)
    out = {"v": 1, "address": address, "network": network, "asset": asset,
           "method_is_an_inference": False, "complete": True, "as_of": as_of, **core}
    out["_rule"] = {"stated_at": rule_stated_at or as_of,
                    "text": "a payer is a distinct sending account that we do not control; "
                            "a payment is a confirmed receive bound to it"}
    warnings = []
    if rule_stated_at and out.get("first_payment_at") and rule_stated_at > out["first_payment_at"]:
        warnings.append("rule_stated_after_the_rows_it_covers")
    if warnings:
        out["warnings"] = warnings
    return out


def main(argv=None, fetch=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--address", required=True)
    p.add_argument("--network")
    p.add_argument("--asset")
    p.add_argument("--node", action="append")
    p.add_argument("--from-block", type=int)
    p.add_argument("--exclude-self", action="append", default=[])
    p.add_argument("--rule-stated-at")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        r = recount(a.address, a.network, a.asset, a.node, a.from_block, a.exclude_self,
                    a.rule_stated_at, fetch)
    except Refusal as e:
        print(json.dumps({"refused": e.code, "detail": e.detail, "complete": False}))
        return e.exit_code
    print(json.dumps(r, indent=None if a.json else 1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
