"""End-to-end check: a real HTTP node stub on loopback, the real urllib path.

Not part of the test suite - this is the hand check that the module works when it
actually speaks HTTP to something, rather than to a stubbed post_json.

Run it with `python e2e_check.py`. Behind an HTTP proxy, prefix it with
`no_proxy=127.0.0.1` so urllib reaches the loopback stub directly.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from nano_settlement_verify import Mismatch, NotFound, verify

SELLER = "nano_3abc"
PAYER = "nano_3payer"


def send(amount, confirmed="true"):
    """A send in the shape a real node reports it: the chain is the payer's, and
    the account paid is the block's link."""
    return {
        "block_account": PAYER,
        "amount": amount,
        "confirmed": confirmed,
        "height": "42",
        "subtype": "send",
        "contents": {"type": "state", "account": PAYER, "link_as_account": SELLER},
    }


BLOCKS = {
    "AAA": send(str(10**24)),
    "PENDING": send(str(10**24), confirmed="false"),
    "SHORT": send(str(10**23)),
    # A confirmed receive on the seller's own chain. It pays nobody, and its
    # block_account IS the seller - so it must not verify as a payment.
    "INBOUND": {
        "block_account": SELLER,
        "amount": str(10**24),
        "confirmed": "true",
        "height": "12",
        "subtype": "receive",
        "contents": {"type": "state", "account": SELLER, "link_as_account": SELLER},
    },
}


class Node(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert request["action"] == "block_info", request
        assert request["json_block"] == "true", request
        reply = BLOCKS.get(request["hash"], {"error": "Block not found"})
        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Node)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_address[1]}"
print(f"node stub listening on {url}\n")

receipt = verify("AAA", 10**24, "nano_3abc", url)
print("1. confirmed block of 10**24 raw to nano_3abc")
print(f"   -> {receipt}")
print(f"   -> to_json(): {receipt.to_json()}")
assert receipt.settled is True
assert receipt.amount_raw == 10**24
assert receipt.height == 42
assert receipt.account == "nano_3abc"

receipt = verify("PENDING", 10**24, "nano_3abc", url)
print('\n2. block whose confirmed field is "false"')
print(f"   -> {receipt}")
assert (receipt.settled, receipt.amount_raw, receipt.height, receipt.account) == (False, 0, 0, "")

try:
    verify("NOPE", 1, "nano_3abc", url)
except NotFound as error:
    print('\n3. node replies {"error": "Block not found"}')
    print(f"   -> NotFound: {error}")
else:
    raise SystemExit("FAIL: expected NotFound")

try:
    verify("SHORT", 10**24, "nano_3abc", url)
except Mismatch as error:
    print("\n4. confirmed block of 10**23 raw, 10**24 expected")
    print(f"   -> Mismatch(got={error.got}, expected={error.expected})")
    assert error.got == 10**23 and error.expected == 10**24
else:
    raise SystemExit("FAIL: expected Mismatch")

try:
    verify("AAA", 10**24, "nano_3wrongseller", url)
except Mismatch as error:
    print("\n5. confirmed block paying someone else")
    print(f"   -> Mismatch(got={error.got!r}, expected={error.expected!r})")
    assert error.got == "nano_3abc" and error.expected == "nano_3wrongseller"
else:
    raise SystemExit("FAIL: expected Mismatch")

try:
    verify("INBOUND", 10**24, "nano_3abc", url)
except Mismatch as error:
    print("\n6. a confirmed receive on the seller's own chain pays nobody")
    print(f"   -> Mismatch(got={error.got!r}, expected={error.expected!r})")
    assert error.expected == "send"
else:
    raise SystemExit("FAIL: a receive block must not verify as a payment")

server.shutdown()
print("\nall four acceptance tests, the wrong-payee path and the receive path hold end to end.")
