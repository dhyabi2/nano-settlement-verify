"""End-to-end check for nano_quorum: several real HTTP endpoints, the real urllib path.

Not part of the test suite. The suite stubs `post_json`; this speaks HTTP to
three loopback nodes that each behave differently, and then - if the network
allows it - repeats the decisive case against a real public Nano node, so the
agreement rule is exercised on a block that actually settled on the live ledger
rather than on a fixture.

Run it with `python e2e_quorum_check.py`. Behind an HTTP proxy, prefix it with
`no_proxy=127.0.0.1` so urllib reaches the loopback stubs directly.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import nano_settlement_verify
from nano_quorum import (
    Disagreement,
    NotCorroborated,
    balance_corroborated,
    post_json_failover,
    verify_corroborated,
)
from nano_settlement_verify import Mismatch

SELLER = "nano_3abc"
PAYER = "nano_3payer"
PAID = 10**24


def send(amount=PAID, height="42", confirmed="true"):
    return {
        "block_account": PAYER,
        "amount": str(amount),
        "confirmed": confirmed,
        "height": height,
        "subtype": "send",
        "contents": {"type": "state", "account": PAYER, "link_as_account": SELLER},
    }


def serve(behaviour):
    """Start a loopback node with the given behaviour and return its URL and server."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            status, payload = behaviour(body)
            raw = json.dumps(payload).encode() if isinstance(payload, (dict, list)) else payload
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}", server


def honest(body):
    if body["action"] == "account_info":
        return 200, {"confirmed_balance": str(PAID), "confirmed_receivable": "0"}
    return 200, send()


def lagging(body):
    """Has not seen the block, and has not confirmed the balance either."""
    if body["action"] == "account_info":
        return 200, {"error": "Account not found", "balance": "0"}
    return 200, {"error": "Block not found"}


def liar(body):
    """Confirms the same hash with a different height. Both cannot be true."""
    if body["action"] == "account_info":
        return 200, {"confirmed_balance": str(PAID * 2), "confirmed_receivable": "0"}
    return 200, send(height="999")


def broken(body):
    """A proxy's maintenance page: HTTP 200, and not a node reply."""
    return 200, b"<html><body>503 upstream</body></html>"


HONEST_A, s1 = serve(honest)
HONEST_B, s2 = serve(honest)
LAGGING, s3 = serve(lagging)
LIAR, s4 = serve(liar)
BROKEN, s5 = serve(broken)
DEAD = "http://127.0.0.1:1"  # nothing listens here

print("endpoints:")
for name, url in [
    ("honest A", HONEST_A), ("honest B", HONEST_B), ("lagging", LAGGING),
    ("liar", LIAR), ("broken", BROKEN), ("dead", DEAD),
]:
    print(f"  {name:9} {url}")

out = verify_corroborated("AAA", PAID, SELLER, [HONEST_A, HONEST_B])
print("\n1. two honest nodes agree")
print(f"   -> {out.to_json()}")
assert out.receipt.settled is True and out.agreed == (HONEST_A, HONEST_B)

out = verify_corroborated("AAA", PAID, SELLER, [DEAD, BROKEN, HONEST_A, HONEST_B])
print("\n2. a dead endpoint and a maintenance page do not stop two live ones")
print(f"   -> settled={out.receipt.settled} agreed={len(out.agreed)} failed={len(out.failed)}")
assert out.receipt.settled is True and len(out.failed) == 2

out = verify_corroborated("AAA", PAID, SELLER, [HONEST_A, LAGGING])
print("\n3. one confirming, one that has not seen the block: not yet, not no")
print(f"   -> settled={out.receipt.settled} answered={len(out.answered)}")
assert out.receipt.settled is False and len(out.answered) == 2

try:
    verify_corroborated("AAA", PAID, SELLER, [HONEST_A, LIAR])
except Disagreement as error:
    print("\n4. two nodes confirming one hash at different heights refuse")
    print(f"   -> Disagreement: {error}")
else:
    raise SystemExit("FAIL: a contradiction must not settle")

try:
    verify_corroborated("AAA", PAID, SELLER, [DEAD, BROKEN])
except NotCorroborated as error:
    print("\n5. nothing readable: 'we could not look', NOT 'you were not paid'")
    print(f"   -> NotCorroborated: {error}")
    assert error.answered == 0
else:
    raise SystemExit("FAIL: silence must not read as an answer")

try:
    verify_corroborated("AAA", PAID, "nano_3stranger", [HONEST_A, HONEST_B])
except Mismatch as error:
    print("\n6. one node saying it paid a stranger refuses at once")
    print(f"   -> Mismatch(got={error.got!r}, expected={error.expected!r})")
else:
    raise SystemExit("FAIL: a stranger must be refused")

try:
    verify_corroborated("AAA", PAID, SELLER, [HONEST_A, HONEST_A + "/"])
except ValueError as error:
    print("\n7. one endpoint written twice is one witness, and is refused")
    print(f"   -> ValueError: {error}")
else:
    raise SystemExit("FAIL: a duplicate endpoint must not pass as corroboration")

print("\n8. balances")
figure = balance_corroborated(SELLER, [HONEST_A, HONEST_B])
print(f"   two honest nodes      -> {figure}")
assert figure == (PAID, 0)
try:
    balance_corroborated(SELLER, [HONEST_A, LIAR])
except Disagreement as error:
    print(f"   one node overstating  -> Disagreement: {error}")
else:
    raise SystemExit("FAIL: differing balances must not settle")

reply, which = post_json_failover([DEAD, HONEST_A], {"action": "account_info"})
print("\n9. failover takes the first endpoint that answers")
print(f"   -> {which} said {reply}")
assert which == HONEST_A

for server in (s1, s2, s3, s4, s5):
    server.shutdown()

# --- the same rule, against the live ledger -----------------------------------
#
# A real confirmed send on the Nano mainnet. The second "endpoint" is a loopback
# relay in front of the same public node: a genuinely distinct URL and a second
# real network hop, which is what this environment allows. Two independent
# public RPCs would be the real test, and this is honest about not being that.
LIVE_RPC = os.environ.get("NANO_RPC", "https://rpc.nano.to")
LIVE_HASH = "6B7AF8D4F9B922F1AEF96CE7B543E11C26EE7178573401813437FDBAC814965F"
LIVE_RAW = 100000000000000000000000000
LIVE_PAYEE = "nano_1natrium1o3z5519ifou7xii8crpxpk8y65qmkih8e8bpsjri651oza8imdd"


def relay(body):
    return 200, nano_settlement_verify.post_json(LIVE_RPC, body)


print(f"\n10. against the live ledger via {LIVE_RPC}")
RELAY, s6 = serve(relay)
try:
    # Everything in here needs the public node. A sandbox with no egress, a
    # rate limit or a bad minute at the node must read as "not checked" and
    # leave the nine loopback cases above standing - never as a failure of the
    # library. NotCorroborated is in this list for the same reason: it is what
    # a throttled endpoint looks like from the inside.
    out = verify_corroborated(LIVE_HASH, LIVE_RAW, LIVE_PAYEE, [LIVE_RPC, RELAY])
    print(f"    -> {out.to_json()}")
    assert out.receipt.settled is True
    assert out.receipt.amount_raw == LIVE_RAW
    assert out.receipt.account == LIVE_PAYEE

    try:
        verify_corroborated(LIVE_HASH, LIVE_RAW + 1, LIVE_PAYEE, [LIVE_RPC, RELAY])
    except Mismatch as error:
        print(f"    one raw short -> Mismatch(got={error.got!r}, expected={error.expected!r})")
    else:
        raise SystemExit("FAIL: a one-raw-short expectation must be refused on the live ledger")

    balance = balance_corroborated(LIVE_PAYEE, [LIVE_RPC, RELAY])
    print(f"    live confirmed (balance_raw, receivable_raw) -> {balance}")
    assert isinstance(balance[0], int) and balance[0] > 0
except (OSError, ValueError, NotCorroborated) as error:
    print(f"    skipped, the node is not reachable from here: {type(error).__name__}: {error}")
finally:
    s6.shutdown()

print("\nagreement, contradiction, silence, duplicate endpoints and balances all hold end to end.")
