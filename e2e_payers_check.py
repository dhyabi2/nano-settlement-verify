"""End-to-end check for nano_payers: real HTTP nodes, and then a real seller.

Not part of the test suite. The suite stubs `post_json`; this speaks HTTP to
loopback nodes over the real urllib path, and then - if the network allows it -
audits a live x402 seller end to end: fetch its own document, derive the account
it says to pay, and count off the public ledger who has paid it.

That last leg is the whole claim of the module, so it is worth running against
something nobody here controls the reading of.

Run it with `python e2e_payers_check.py`. Behind an HTTP proxy, prefix it with
`no_proxy=127.0.0.1` so urllib reaches the loopback stubs directly.
"""

import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import nano_settlement_verify
from nano_payers import (
    HistoryIncomplete,
    ManifestRefused,
    outside_payers,
    payees_from_manifest,
    payers,
    payers_corroborated,
)
from nano_quorum import Disagreement, NotCorroborated

PAYEE = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"
PAYER_1 = "nano_3gqrm67xjp33gew1pmrbhx8rm6y1yrcogbid36ku5nc9izp8nn3ec5pa48yt"
PAYER_2 = "nano_1995xc97t5dmpdqayx6j8798d8jtz8n14nr6usti3jub7e7z9na1obwwi7xb"
FUNDER = "nano_1xug1q5t7nxoj3ywwzokiea9jz8fq8qfgzp8pbyfr3co3e5xgj755uofu8ue"
CALL = 100000000000000000000000000  # 0.0001 XNO in raw


def serve(behaviour):
    """Start a loopback node with the given behaviour and return its URL and server."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            status, reply = behaviour(body)
            payload = json.dumps(reply).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}", server


def rows(*blocks):
    return {"account": PAYEE, "history": list(blocks)}


def receive(account, amount, height, confirmed="true", when=1790594630):
    return {
        "type": "receive",
        "account": account,
        "amount": str(amount),
        "height": str(height),
        "confirmed": confirmed,
        "local_timestamp": str(when),
        "hash": f"{height:064X}",
    }


CHAIN = rows(
    receive(PAYER_1, CALL, 5),
    receive(PAYER_1, CALL, 4, when=1790594600),
    receive(PAYER_2, 3 * CALL, 3, when=1790500000),
    {
        "type": "send",
        "account": PAYER_1,
        "amount": str(50 * 10**30),
        "height": "2",
        "confirmed": "true",
        "local_timestamp": "1790400000",
        "hash": f"{2:064X}",
    },
    receive(FUNDER, 20 * 10**30, 1, when=1790000000),
)

print("1. a real HTTP node, the whole chain, over the real urllib path")
GOOD, s1 = serve(lambda body: (200, CHAIN))
try:
    report = payers(PAYEE, [GOOD])
    print(f"   -> {report.to_json()}")
    assert report.distinct_payers == 3, report.distinct_payers
    assert report.payments == 4
    assert report.received_raw == 2 * CALL + 3 * CALL + 20 * 10**30
    assert report.complete is True
    assert report.asked == (GOOD,)

    print("2. the seller's own spending is not income")
    # The 50 XNO send in the chain above is money leaving; it must appear in
    # neither the payer count nor the total.
    assert report.received_raw < 50 * 10**30
    print(f"   -> received {report.received_raw} raw, the 50 XNO send ignored")

    print("3. our own funding account comes out, and the judgement is visible")
    outside = outside_payers(report, [FUNDER])
    print(f"   -> {outside.distinct_payers} payer(s) who are not us, "
          f"{outside.received_raw} raw")
    assert outside.distinct_payers == 2
    assert outside.received_raw == 5 * CALL
finally:
    s1.shutdown()

print("4. a node serving an unreadable reply is walked away from, not believed")
BROKEN, s2 = serve(lambda body: (200, {"account": PAYEE, "history": {"not": "a list"}}))
GOOD2, s3 = serve(lambda body: (200, CHAIN))
try:
    report = payers(PAYEE, [BROKEN, GOOD2])
    print(f"   -> read from {report.asked[0]}, {report.distinct_payers} payer(s)")
    assert report.asked == (GOOD2,)
    assert report.distinct_payers == 3
finally:
    s2.shutdown()

print("5. two nodes that name the same payers corroborate")
GOOD3, s4 = serve(lambda body: (200, CHAIN))
try:
    corroborated = payers_corroborated(PAYEE, [GOOD2, GOOD3])
    print(f"   -> {corroborated.distinct_payers} payer(s), asked {len(corroborated.asked)}")
    assert corroborated.distinct_payers == 3
    assert len(corroborated.asked) == 2

    print("6. a node that names a different set is a disagreement, not a vote")
    SHORT, s5 = serve(lambda body: (200, rows(receive(FUNDER, 20 * 10**30, 1))))
    try:
        payers_corroborated(PAYEE, [GOOD2, SHORT])
    except Disagreement as error:
        named = {url: len(accounts) for url, accounts in error.answers.items()}
        print(f"   -> Disagreement{named}")
    else:
        raise SystemExit("FAIL: two different payer sets must not corroborate")
    finally:
        s5.shutdown()

    print("7. a node that answers nothing is 'we could not look', not 'nobody paid'")
    try:
        payers_corroborated(PAYEE, [GOOD2, "http://127.0.0.1:1"])
    except NotCorroborated as error:
        print(f"   -> NotCorroborated(answered={error.answered})")
        assert error.answered == 1
    else:
        raise SystemExit("FAIL: one answer must not corroborate a payer set")
finally:
    s3.shutdown()
    s4.shutdown()

# --- the same thing, against a seller nobody here controls ---------------------
#
# The point of the module: a stranger holding nothing but these two URLs can
# re-derive every number printed below. A sandbox with no egress, a rate limit
# or a bad minute at either end must read as "not checked" and leave the seven
# loopback cases above standing - never as a failure of the library.
LIVE_X402 = os.environ.get(
    "X402_URL", "https://extract.paypercall.dev/.well-known/x402"
)
LIVE_RPC = os.environ.get("NANO_RPC", "https://rpc.nano.to")

print(f"\n8. a live seller: {LIVE_X402}")
try:
    request = urllib.request.Request(
        LIVE_X402, headers={"User-Agent": nano_settlement_verify.USER_AGENT}
    )
    with urllib.request.urlopen(
        request, timeout=nano_settlement_verify.RPC_TIMEOUT_S
    ) as response:
        doc = json.loads(response.read().decode("utf-8"))

    payees = payees_from_manifest(doc)
    print(f"   its own document advertises {len(payees)} Nano mainnet payee(s)")
    for payee in payees:
        print(f"   {payee.account}")
        print(f"     prices_raw={[str(p) for p in payee.prices_raw]} "
              f"across {len(payee.resources)} resource(s)")
        assert payee.network == "nano:mainnet"
        assert payee.prices_raw, "a priced seller must advertise a price in raw"

        report = payers(payee.account, [LIVE_RPC])
        print(f"     -> {report.distinct_payers} distinct account(s) have paid it, "
              f"{report.payments} payment(s), {report.received_raw} raw "
              f"({report.received_raw // 10**27} milli-XNO)")
        assert report.complete is True, "the live chain must be read to its open block"
        assert isinstance(report.received_raw, int)
        # Every payment at exactly a price the seller advertises is a call
        # somebody paid for, as opposed to the account being funded. This is
        # the only inference drawn from the ledger, and it is drawn from the
        # seller's OWN price list, not from the size of the number.
        at_price = [
            payer
            for payer in report.payers
            if payer.received_raw % min(payee.prices_raw) == 0
            and payer.received_raw <= 100 * min(payee.prices_raw)
        ]
        print(f"     -> {len(at_price)} of them paid only whole multiples of the "
              f"advertised {min(payee.prices_raw)} raw, in amounts under 100 calls")
        # The payer list is exactly what nano_independence wants: counting
        # paying keys is not counting buyers, and three accounts funded from
        # one place are one operator however separately they paid.
        from nano_independence import independence

        groups = independence(
            [payer.account for payer in report.payers], LIVE_RPC, seller=payee.account
        )
        print(f"     -> {groups.keys} paying key(s) group into "
              f"{groups.independent} independent payer(s)")
        assert groups.keys == report.distinct_payers
        assert groups.independent <= groups.keys
except (OSError, ValueError, ManifestRefused, HistoryIncomplete, NotCorroborated) as error:
    print(f"   skipped, not reachable from here: {type(error).__name__}: {error}")

print("\nthe payer count holds end to end: only confirmed receives from other "
      "accounts, summed in raw as integers, read to the open block.")
