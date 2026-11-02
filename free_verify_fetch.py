"""Fetch a block from free public nodes, then judge it with free_verify. Also a small keyless server.

Only this file touches the network. Reads only: it sends block_info and nothing else, holds no key,
and has no code path that sends, signs or spends.

    python3 free_verify_fetch.py --quote quote.json [--node URL[,URL...]]
    python3 free_verify_fetch.py --serve 8080          # GET /verify?pay_to=&amount_raw=&block=

Exit codes follow free_verify: 0 settled, 2 unknown, 3 mismatch, 4 bad input.
A node that cannot be reached is skipped; if none answers the verdict is unknown, never unpaid.
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import free_verify

DEFAULT_NODES = ["https://rpc.nano.to", "https://node.somenano.com/proxy",
                 "https://nanoslo.0x.no/proxy"]
USER_AGENT = "free-verify/1.0 (+https://github.com/dhyabi2/nano-settlement-verify)"
TIMEOUT_S = 15
RATE_PER_MINUTE = 30


def fetch_block(block_hash, nodes):
    """First parseable reply from the nodes, in order. Returns (reply, node) or ({"error": ...}, None)."""
    last = "no node answered"
    for node in nodes:
        body = json.dumps({"action": "block_info", "json_block": "true", "hash": block_hash}).encode()
        request = urllib.request.Request(node, data=body, headers={
            "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:
                data = json.loads(reply.read(1_000_000))
        except (OSError, ValueError) as error:
            last = "%s: %s" % (node, type(error).__name__)
            continue
        if isinstance(data, dict):
            return data, node
    return {"error": last}, None


def verify_with_nodes(quote, nodes=None, fetch=fetch_block):
    free_verify._validate(quote)
    if not quote["block_hash"]:
        return free_verify.check_settlement(quote, {})
    reply, node = fetch(quote["block_hash"].upper(), nodes or DEFAULT_NODES)
    verdict = free_verify.check_settlement(quote, reply)
    verdict["node"] = node
    return verdict


class _Limiter:
    def __init__(self, per_minute):
        self.per_minute, self.hits = per_minute, {}

    def allow(self, who, now=None):
        now = time.time() if now is None else now
        recent = [t for t in self.hits.get(who, []) if now - t < 60]
        if len(recent) >= self.per_minute:
            self.hits[who] = recent
            return False
        recent.append(now)
        self.hits[who] = recent
        return True


def make_handler(nodes, fetch=fetch_block, limiter=None):
    limiter = limiter or _Limiter(RATE_PER_MINUTE)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, obj):
            body = json.dumps(obj, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = urllib.parse.urlsplit(self.path)
            if parts.path != "/verify":
                return self._send(404, {"error": "GET /verify?pay_to=nano_...&amount_raw=<digits>&block=<64 hex>"})
            if not limiter.allow(self.client_address[0]):
                return self._send(429, {"error": "rate limit: %d checks a minute per address" % limiter.per_minute})
            q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
            quote = {"quote_id": q.get("quote_id", "-"), "pay_to": q.get("pay_to"),
                     "amount_raw": q.get("amount_raw"), "block_hash": q.get("block")}
            try:
                verdict = verify_with_nodes(quote, nodes, fetch)
            except (ValueError, TypeError) as error:
                return self._send(400, {"error": str(error)})
            self._send(200, verdict)

        def log_message(self, *args):
            pass

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(prog="free_verify_fetch")
    parser.add_argument("--quote")
    parser.add_argument("--node", default=",".join(DEFAULT_NODES))
    parser.add_argument("--serve", type=int, metavar="PORT")
    args = parser.parse_args(argv)
    nodes = [n.strip() for n in args.node.split(",") if n.strip()]
    if args.serve:
        server = ThreadingHTTPServer(("127.0.0.1", args.serve), make_handler(nodes))
        print("serving on 127.0.0.1:%d (put your own TLS in front)" % args.serve, file=sys.stderr)
        server.serve_forever()
        return 0
    if not args.quote:
        parser.error("--quote or --serve is required")
    with open(args.quote) as handle:
        try:
            verdict = verify_with_nodes(json.load(handle), nodes)
        except ValueError as error:
            print(json.dumps({"error": str(error)}))
            return 4
    print(json.dumps(verdict, sort_keys=True))
    return free_verify.EXIT[verdict["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
