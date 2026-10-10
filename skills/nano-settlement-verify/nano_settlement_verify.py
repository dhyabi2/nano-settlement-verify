"""Verify a Nano (XNO) settlement receipt.

Given the hash of a send block, ask a Nano node whether that block is confirmed,
whether it moved the amount that was quoted, and whether it paid the account the
seller expected. What comes back is a receipt a seller can keep.

Amounts are raw integers everywhere: 1 XNO is 10**30 raw, and a float loses the
low digits, so raw is parsed with int() and never with float().

Accounts are compared as accounts, not as the strings that spell them: one Nano
account has two spellings, the modern `nano_` form and the legacy `xrb_` form.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "Receipt",
    "NotFound",
    "Mismatch",
    "Late",
    "UnknownTime",
    "NotANodeReply",
    "verify",
    "parse_not_after",
    "TIME_SOURCE",
    "post_json",
    "is_valid_account",
    "public_key_from_address",
]

# Seconds to wait on the node before giving up. Verification sits on a seller's
# request path; an unbounded wait there is an outage, not patience.
RPC_TIMEOUT_S = 30

# Sent on every node call. Cloudflare-fronted RPCs, rpc.nano.to among them, refuse
# urllib's default `Python-urllib/3.x` with 403 "error code: 1010" - a refusal
# indistinguishable from a bad node - so the call names itself instead.
USER_AGENT = "nano-settlement-verify/1.0 (+https://github.com/dhyabi2/nano-settlement-verify)"

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


# Said in every receipt that carries a deadline, because it is the whole caveat.
TIME_SOURCE = (
    "local_timestamp: when this node first saw the block, on its own clock; "
    "not signed by the sender, and another node can differ by seconds - "
    "read it on two nodes if seconds matter"
)


class Late(Exception):
    """The block settled for the right amount and account, but was seen after not_after.

    The money did arrive - `receipt` is the settled receipt, so a refund can
    cite it - but not by the order's deadline, so the order is not paid on time.
    """

    def __init__(self, seen_at: int, not_after: int, receipt: "Receipt") -> None:
        super().__init__(f"seen at {seen_at}, after the deadline {not_after}")
        self.seen_at = seen_at
        self.not_after = not_after
        self.receipt = receipt


class UnknownTime(Exception):
    """The block settled, but the node reports no time it saw it (missing or 0).

    Very old blocks carry local_timestamp 0. With no time, on-time cannot be
    shown, so this is never read as on time.
    """

    def __init__(self, not_after: int, receipt: "Receipt") -> None:
        super().__init__(f"the node reports no local_timestamp; deadline {not_after} unchecked")
        self.not_after = not_after
        self.receipt = receipt


class NotANodeReply(ValueError):
    """The endpoint answered with JSON that is not a node's block_info reply.

    A public RPC behind a proxy answers 200 with its own JSON far more often
    than it answers with HTML: a status page, a rate-limit envelope, an auth
    complaint. None of them carry `confirmed`, so none of them says anything
    about the payment.

    This is a `ValueError` on purpose. It belongs with the transport failures,
    not with `NotFound` or `Mismatch`: it means "we could not look", not "the
    payment is bad". A seller catching `(OSError, ValueError)` around `verify` -
    which is what the README tells it to do, because this sits on its request
    path - holds the call and asks again, instead of taking an uncaught
    exception. It used to be a bare `KeyError`, which escaped that arm.

    It is never raised for a reply that IS a node reply: an `{"error": ...}`
    body is still `NotFound`, and `"confirmed": "false"` is still an unsettled
    receipt.
    """


def _required(reply: dict, field: str):
    """`reply[field]`, or `NotANodeReply` naming what was missing.

    Every field a node's block_info reply must carry is read through here, so a
    malformed reply is refused in the documented family rather than raising a
    bare KeyError out of a seller's request path.
    """
    try:
        return reply[field]
    except KeyError:
        raise NotANodeReply(
            "the endpoint's reply carries no %r, so it is not a node's "
            "block_info reply and says nothing about this block; keys present: %s"
            % (field, sorted(reply))
        ) from None


@dataclass(frozen=True)
class Receipt:
    """What a seller can show to prove a payment settled."""

    settled: bool
    amount_raw: int
    height: int
    account: str
    # Set only when verify() was given not_after; otherwise the receipt and its
    # JSON are exactly what they were before deadlines existed.
    seen_at: int | None = None
    not_after: int | None = None

    def to_json(self) -> str:
        out = {
            "settled": self.settled,
            "amount_raw": self.amount_raw,
            "height": self.height,
            "account": self.account,
        }
        if self.not_after is not None:
            out.update(seen_at=self.seen_at, not_after=self.not_after, time_source=TIME_SOURCE)
        return json.dumps(out)


def parse_not_after(text: str) -> int:
    """A deadline as unix seconds, from unix seconds or ISO-8601 with a UTC offset.

    `1790150110`, `2026-09-23T07:55:10Z` and `2026-09-23T07:55:10+00:00` are the
    same instant. A time with no offset is refused (ValueError): whose clock it
    means is a guess, and a guessed deadline decides real orders.
    """
    text = str(text).strip()
    if text.isdigit():
        return int(text)
    when = datetime.fromisoformat(text)  # ValueError on anything unreadable
    if when.tzinfo is None:
        raise ValueError(f"deadline {text!r} has no UTC offset; add Z or +00:00")
    return int(when.timestamp())


def _seen_at(reply: dict) -> int | None:
    """The node's local_timestamp as unix seconds, or None when it has none.

    Missing, empty, unreadable and 0 all come back as None: 0 is what a node
    reports for a block it has no record of seeing, not the epoch.
    """
    try:
        seen = int(str(reply.get("local_timestamp") or "0"))
    except ValueError:
        return None
    return seen if seen > 0 else None


def post_json(rpc_url: str, payload: dict) -> dict:
    """POST payload as JSON to a Nano node's RPC endpoint and return its reply."""
    request = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
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


# The two spellings of one Nano account. The address prefix was renamed from
# `xrb_` to `nano_`; the 60 characters after it are the same encoding of the same
# public key, and both forms are still in use and still accepted everywhere. So
# two addresses name the same account exactly when those 60 characters match.
_ACCOUNT_PREFIXES = ("nano_", "xrb_")


def _account_body(address: object) -> str | None:
    """The part of an address that identifies the account, or None if it has none.

    Everything after the `nano_`/`xrb_` prefix. Anything that is not a string
    carrying one of those prefixes is not a Nano address, and comes back as None
    so that it is refused rather than compared - an absent link reads as "", and
    "" must never match. A caller who passes None for `account` still gets the
    Mismatch it got before this function existed, not an AttributeError.
    """
    if not isinstance(address, str):
        return None
    for prefix in _ACCOUNT_PREFIXES:
        if address.startswith(prefix):
            return address[len(prefix):]
    return None


# A Nano address is its public key in base32 plus a 5-byte blake2b checksum of
# that key, so an address can be checked for being payable at all without
# asking anyone: 60 characters after the prefix, the first 52 carrying 4 zero
# padding bits and the 256-bit key, the last 8 carrying the checksum reversed.
# Nano's own base32 alphabet drops the characters that misread by hand.
_ACCOUNT_ALPHABET = "13456789abcdefghijkmnopqrstuwxyz"
_ACCOUNT_ALPHABET_INDEX = {c: i for i, c in enumerate(_ACCOUNT_ALPHABET)}
_ACCOUNT_BODY_LEN = 60


def _base32_to_int(chars: str) -> int | None:
    """The integer those base32 characters spell, or None if any is not one."""
    value = 0
    for char in chars:
        index = _ACCOUNT_ALPHABET_INDEX.get(char)
        if index is None:
            return None
        value = value * 32 + index
    return value


def public_key_from_address(address: object) -> bytes | None:
    """The 32-byte public key an address names, or None if it names none.

    None means the string cannot be a Nano account at all: a wrong prefix, a
    wrong length, a character outside Nano's base32 alphabet, non-zero padding
    bits, or a checksum that does not match the key it is attached to. It never
    means the account is empty or unknown to any node - this reads the address
    itself and asks nobody.
    """
    body = _account_body(address)
    if body is None or len(body) != _ACCOUNT_BODY_LEN:
        return None
    key_value = _base32_to_int(body[:52])
    check_value = _base32_to_int(body[52:])
    if key_value is None or check_value is None:
        return None
    if key_value >> 256:  # the 4 leading bits are padding and must be zero
        return None
    public_key = key_value.to_bytes(32, "big")
    expected = hashlib.blake2b(public_key, digest_size=5).digest()[::-1]
    if check_value.to_bytes(5, "big") != expected:
        return None
    return public_key


def is_valid_account(address: object) -> bool:
    """Whether this string is an address XNO can actually be sent to.

    The checksum is the whole point: a mistyped or truncated Nano address is
    overwhelmingly likely to fail it, and a Nano send is irreversible, so an
    address that fails this is money that leaves and never arrives.
    """
    return public_key_from_address(address) is not None


def _same_account(left: object, right: object) -> bool:
    """Whether two addresses name one account, however each one is spelled.

    A node always answers the modern `nano_` form, while a seller passes whatever
    they have stored, which for older tooling is the `xrb_` form of the very same
    account. Comparing the two strings refuses a payment that did arrive - and by
    then the buyer's money has irreversibly moved, so the refusal costs the sale
    and leaves the seller with no receipt for money they were paid.
    """
    body = _account_body(left)
    return body is not None and body == _account_body(right)


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


def verify(
    block_hash: str, expect_raw: int, account: str, rpc_url: str, not_after: int | None = None
) -> Receipt:
    """Prove a Nano send block settled for expect_raw to account.

    Returns a settled Receipt when it did, and an unsettled one when the node
    has the block but has not confirmed it yet.

    Raises NotFound when the node has no such block, and Mismatch when the
    amount differs, when the block names an operation other than "send", or when
    it paid an account other than the one expected. `account` may be given in either the `nano_` or
    the `xrb_` spelling; they are two names for one account. The receipt records
    the account as the node spelled it, which is the canonical `nano_` form.

    Raises TypeError when expect_raw is not an int, because raw is an integer
    and a float expectation cannot be compared to one safely.

    Raises NotANodeReply - a ValueError - when the endpoint answers with JSON
    that is not a node's block_info reply, which is what a proxy's status page
    or a rate-limit envelope looks like. That lands with the transport failures
    a seller is told to catch, rather than as a bare KeyError through its
    request path. A reply that carries `confirmed` and an amount but names no
    operation or no payee - a node that ignored json_block and sent `contents`
    as a string, or dropped `subtype` - is the same family: nothing was read, so
    nothing is claimed about the payment.

    With `not_after` (unix seconds; see parse_not_after), a settled block must
    also have been seen by this node at or before it - inclusive. Raises Late
    when it was seen after, and UnknownTime when the node reports no time
    (missing or 0). The time is the node's own local_timestamp: a Nano block
    carries no sender-signed time, so the deadline is checked against the node
    the caller chose, and another node can differ by seconds.
    """
    if isinstance(expect_raw, bool) or not isinstance(expect_raw, int):
        # A float expect_raw silently loses digits above 2**53, so 1 XNO written
        # as 1e30 would "match" an amount 19884624838656 raw short of it. Refuse
        # the comparison rather than settle on it.
        raise TypeError(f"expect_raw must be an int of raw, got {type(expect_raw).__name__}")
    reply = post_json(rpc_url, {"action": "block_info", "json_block": "true", "hash": block_hash})
    if "error" in reply:
        raise NotFound(block_hash)
    if _required(reply, "confirmed") != "true":
        return Receipt(settled=False, amount_raw=0, height=0, account="")
    # raw is an integer string; never parse it as a float
    amount = int(_required(reply, "amount"))
    if amount != expect_raw:
        raise Mismatch(amount, expect_raw)
    # Only a send pays anyone. A receive or an open block sits on the account that
    # was credited, so without this check the hash of any confirmed inbound block
    # of the seller's own chain would verify with nothing paid for this call.
    operation = _operation(reply)
    # "" is not an answer about the block, it is the absence of one: a reply with
    # no subtype and a contents.type of "state", or a contents the node sent as an
    # opaque string because it ignored json_block. Reporting that as
    # `Mismatch("", "send")` says "this block is not a send" - a verdict about the
    # money, and the one arm the README tells a seller to refuse on - when the
    # truth is "we could not look", which is what NotANodeReply is for. A real
    # non-send names itself ("receive", "open", "change", "epoch") and is still a
    # Mismatch below.
    if not operation:
        raise NotANodeReply(
            f"the reply for {block_hash} names no operation: no subtype, and "
            f"contents {'is not an object' if not isinstance(reply.get('contents'), dict) else 'names no type'}"
        )
    if operation != "send":
        raise Mismatch(operation, "send")
    # block_account is the account whose chain the block sits on - for a send, the
    # payer. The account paid is the block's link.
    paid = _paid_account(reply)
    # Same distinction: every send names its payee, in one of the four places
    # `_paid_account` looks, so "" means the reply carried none of them - not that
    # the block paid somebody else. `Mismatch("", account)` told a seller whose
    # buyer really had paid that it had been paid by nobody.
    if not paid:
        raise NotANodeReply(
            f"the reply for {block_hash} names no payee: no link_as_account and no "
            f"destination, in contents or at the top level"
        )
    if not _same_account(paid, account):
        raise Mismatch(paid, account)
    receipt = Receipt(
        settled=True,
        amount_raw=amount,
        height=int(_required(reply, "height")),
        account=paid,
    )
    if not_after is None:
        return receipt
    # The order's expiry. Checked last, so a wrong amount or payee is still a
    # Mismatch whatever its time. Inclusive: seen exactly at not_after is on time.
    seen_at = _seen_at(reply)
    receipt = Receipt(receipt.settled, receipt.amount_raw, receipt.height, receipt.account,
                      seen_at=seen_at, not_after=not_after)
    if seen_at is None:
        raise UnknownTime(not_after, receipt)
    if seen_at > not_after:
        raise Late(seen_at, not_after, receipt)
    return receipt
