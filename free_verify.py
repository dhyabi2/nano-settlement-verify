"""Free verification path: was this Nano payment made? Three verdicts, no node, no money.

Network-free on purpose. The caller hands in a quote and a node's reply it already has; this
module returns settled, unknown or mismatch. Fetching lives in free_verify_fetch.py.

There is no "unpaid" verdict and there cannot be one. A read that finds nothing means "I do not know
yet", and turning that into "unpaid" is how a blind retry pays twice for a payment that was already
final (merktop, 2026-10-31). A caller that needs "unpaid" gets it from its own quote expiry.

A balance cannot answer "was this paid"; check_from_balance exists only to refuse.

Amounts are integer raw (1 XNO = 10**30 raw), compared as Decimal parsed from digit strings.
"""

import argparse
import decimal
import hashlib
import json
import re
import sys

VERDICTS = ("settled", "unknown", "mismatch")
EXIT = {"settled": 0, "unknown": 2, "mismatch": 3}
_ADDRESS = re.compile(r"^nano_[13][13456789abcdefghijkmnopqrstuwxyz]{59}$")
_HASH = re.compile(r"^[0-9A-Fa-f]{64}$")
_ARTIFACT = "block_info"


def _validate(quote):
    if not isinstance(quote, dict):
        raise ValueError("quote is missing quote_id")
    for key in ("quote_id", "pay_to", "amount_raw", "block_hash"):
        if key not in quote:
            raise ValueError("quote is missing %s" % key)
    amount = quote["amount_raw"]
    if isinstance(amount, bool) or not isinstance(amount, (str, int)):
        raise ValueError("amount_raw must be an integer string of raw; 1 XNO = 10**30 raw and a "
                         "float loses digits")
    amount = str(amount)
    if "." in amount or "e" in amount.lower():
        raise ValueError("amount_raw must be an integer string of raw; 1 XNO = 10**30 raw and a "
                         "float loses digits")
    if not amount.isascii() or not amount.isdigit():
        raise ValueError("amount_raw must be digits only, got %s" % amount)
    if not isinstance(quote["pay_to"], str) or not _ADDRESS.match(quote["pay_to"]):
        raise ValueError("pay_to is not a Nano address: %s" % (quote["pay_to"],))
    block = quote["block_hash"]
    if block is not None and (not isinstance(block, str) or not _HASH.match(block)):
        raise ValueError("block_hash must be 64 hex characters, got %s" % (block,))


def _out(verdict, block, why, amount=None):
    row = {"verdict": verdict, "block_hash": block, "cost_usd": 0.0,
           "artifact": _ARTIFACT, "why": why}
    if verdict == "settled":
        row["amount_raw"] = amount
    return row


def _locate(response, block):
    """The entry for block in either a block_info reply (flat) or a blocks_info reply."""
    blocks = response.get("blocks")
    if isinstance(blocks, dict):
        for key, entry in blocks.items():
            if str(key).upper() == block and isinstance(entry, dict):
                return entry
        return None
    if "contents" in response or "block_account" in response or "subtype" in response:
        return response
    return None


def _destination(entry):
    contents = entry.get("contents")
    if isinstance(contents, dict) and contents.get("link_as_account"):
        return contents["link_as_account"]
    return entry.get("link_as_account")


def check_settlement(quote, block_response):
    _validate(quote)
    if not isinstance(block_response, dict):
        raise ValueError("block_response must be the node's response object")
    block = quote["block_hash"].upper() if quote["block_hash"] else None
    if "error" in block_response:
        return _out("unknown", block, "node error: %s" % (block_response["error"],))
    if block is None:
        return _out("unknown", None, "the buyer named no block; no block, no verdict, never unpaid")
    entry = _locate(block_response, block)
    if entry is None:
        return _out("unknown", block,
                    "block not found in the response; not found is unknown, never unpaid")
    if entry.get("confirmed") not in ("true", True):
        return _out("unknown", block, "block %s is not confirmed yet" % block)
    subtype = entry.get("subtype")
    if subtype != "send":
        return _out("mismatch", block, "block %s is a %s, not a send" % (block, subtype))
    destination = _destination(entry)
    if destination != quote["pay_to"]:
        return _out("mismatch", block, "block pays %s, the quote named %s"
                    % (destination, quote["pay_to"]))
    try:
        paid = decimal.Decimal(str(entry.get("amount")))
        if paid != paid.to_integral_value() or "." in str(entry.get("amount")):
            raise decimal.InvalidOperation
    except decimal.InvalidOperation:
        return _out("unknown", block, "the node's amount is not an integer string of raw: %r"
                    % (entry.get("amount"),))
    wanted = decimal.Decimal(str(quote["amount_raw"]))
    if paid != wanted:
        return _out("mismatch", block, "block pays %s raw, the quote named %s raw"
                    % (int(paid), int(wanted)))
    return _out("settled", block, "block %s is confirmed, pays %s exactly %s raw"
                % (block, quote["pay_to"], int(wanted)), amount=str(int(wanted)))


def check_from_balance(*args, **kwargs):
    raise NotImplementedError(
        "a balance cannot answer 'was this paid'; name the settlement artifact. "
        "See merktop, 2026-10-31: a balance view answers what the payee has pocketed so far.")


def free_surfaces(path=None):
    """The zero-budget read paths, read from free_surfaces.json, each with its caveat."""
    path = path or __file__[:-len("free_verify.py")] + "free_surfaces.json"
    with open(path) as handle:
        rows = json.load(handle)
    out = []
    for row in rows:
        row = dict(row)
        if row.get("measured_at") is None:
            row["caveat"] = "not measured from a machine"
        elif not row.get("measured_from"):
            raise ValueError("%s claims a measurement with no measured_from" % row.get("name"))
        out.append(row)
    return out


def cost_report(verdicts):
    paid = sorted({v["artifact_paid"] for v in verdicts if v.get("artifact_paid")})
    cost = sum(v.get("cost_usd", 0.0) for v in verdicts)
    n = len(verdicts)
    return {"checks": n, "cost_usd": cost, "paid_surfaces_used": paid,
            "claim": "%d settlement checks, %s USD outgoing, no node run, no account required"
                     % (n, ("%.1f" % cost) if cost == int(cost) else cost)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="free_verify",
                                     description="offline: judge a node reply against a quote")
    parser.add_argument("--quote", required=True, help="quote.json")
    parser.add_argument("--block", required=True, help="the node's block_info/blocks_info reply")
    args = parser.parse_args(argv)
    with open(args.quote) as q, open(args.block) as b:
        try:
            verdict = check_settlement(json.load(q), json.load(b))
        except ValueError as error:
            print(json.dumps({"error": str(error)}))
            return 4
    print(json.dumps(verdict, sort_keys=True))
    return EXIT[verdict["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
