"""Verify a Nano (XNO) settlement receipt.

Given the hash of a send block, ask a Nano node whether that block is confirmed,
whether it moved the amount that was quoted, and whether it paid the account the
seller expected. What comes back is a receipt a seller can keep.

Amounts are raw integers everywhere: 1 XNO is 10**30 raw, and a float loses the
low digits, so raw is parsed with int() and never with float().
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

__all__ = ["Receipt", "NotFound", "Mismatch", "verify", "post_json"]

# Seconds to wait on the node before giving up. Verification sits on a seller's
# request path; an unbounded wait there is an outage, not patience.
RPC_TIMEOUT_S = 30

# The operations a Nano block can name. A state block's contents.type is "state",
# which says nothing about what the block did, so it is deliberately absent here:
# a reply that carries no subtype and a contents.type of "state" names no
# operation, and must fail closed rather than be read as a send.
_OPERATIONS = frozenset({"send", "receive", "open", "change", "epoch"})


class NotFound(Exception):
    """The node reports no block with this hash."""

    def __init__(self, block_hash: str) -> None:
        super().__init__(f"no such block: {block_hash}")
        self.block_hash = block_hash


class Mismatch(Exception):
    """The block is confirmed, but not for the amount or the account expected."""

    def __init__(self, got, expected) -> None:
        super().__init__(f"expected {expected!r}, got {got!r}")
        self.got = got
        self.expected = expected


@dataclass(frozen=True)
class Receipt:
    """What a seller can show to prove a payment settled."""

    settled: bool
    amount_raw: int
    height: int
    account: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "settled": self.settled,
                "amount_raw": self.amount_raw,
                "height": self.height,
                "account": self.account,
            }
        )


def post_json(rpc_url: str, payload: dict) -> dict:
    """POST payload as JSON to a Nano node's RPC endpoint and return its reply."""
    request = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # A seller calls this on its request path, so a node that accepts the
    # connection and then goes quiet must not hang the sale indefinitely.
    with urllib.request.urlopen(request, timeout=RPC_TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def _contents(reply: dict) -> dict:
    """The block's contents, or an empty mapping when the node sent none.

    A node that ignores json_block returns contents as an opaque string. There is
    no link to read out of that, so it must read as absent, not raise.
    """
    contents = reply.get("contents")
    return contents if isinstance(contents, dict) else {}


def _operation(reply: dict) -> str:
    """What the block did: "send", "receive", "open", "change" or "epoch".

    A state block names it in the top-level subtype; a pre-state block names it in
    contents.type. Anything else is unknown, and comes back as "".
    """
    subtype = str(reply.get("subtype") or "")
    if subtype:
        return subtype
    legacy = str(_contents(reply).get("type") or "")
    return legacy if legacy in _OPERATIONS else ""


def _paid_account(reply: dict) -> str:
    """The account the block paid: its link, not the chain it sits on.

    A state block carries it as contents.link_as_account, a pre-state send block
    as contents.destination. An absent link comes back as "".
    """
    contents = _contents(reply)
    return str(
        contents.get("link_as_account")
        or contents.get("destination")
        or reply.get("link_as_account")
        or reply.get("destination")
        or ""
    )


def verify(block_hash: str, expect_raw: int, account: str, rpc_url: str) -> Receipt:
    """Prove a Nano send block settled for expect_raw to account.

    Returns a settled Receipt when it did, and an unsettled one when the node
    has the block but has not confirmed it yet.

    Raises NotFound when the node has no such block, and Mismatch when the
    amount differs, when the block is not a send, or when it paid an account
    other than the one expected.

    Raises TypeError when expect_raw is not an int, because raw is an integer
    and a float expectation cannot be compared to one safely.
    """
    if isinstance(expect_raw, bool) or not isinstance(expect_raw, int):
        # A float expect_raw silently loses digits above 2**53, so 1 XNO written
        # as 1e30 would "match" an amount 19884624838656 raw short of it. Refuse
        # the comparison rather than settle on it.
        raise TypeError(f"expect_raw must be an int of raw, got {type(expect_raw).__name__}")
    reply = post_json(rpc_url, {"action": "block_info", "json_block": "true", "hash": block_hash})
    if "error" in reply:
        raise NotFound(block_hash)
    if reply["confirmed"] != "true":
        return Receipt(settled=False, amount_raw=0, height=0, account="")
    amount = int(reply["amount"])  # raw is an integer string; never parse it as a float
    if amount != expect_raw:
        raise Mismatch(amount, expect_raw)
    # Only a send pays anyone. A receive or an open block sits on the account that
    # was credited, so without this check the hash of any confirmed inbound block
    # of the seller's own chain would verify with nothing paid for this call.
    operation = _operation(reply)
    if operation != "send":
        raise Mismatch(operation, "send")
    # block_account is the account whose chain the block sits on - for a send, the
    # payer. The account paid is the block's link.
    paid = _paid_account(reply)
    if paid != account:
        raise Mismatch(paid, account)
    return Receipt(
        settled=True,
        amount_raw=amount,
        height=int(reply["height"]),
        account=paid,
    )
