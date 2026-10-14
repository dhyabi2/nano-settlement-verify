"""Five named fields, or `unknown`. A settlement verdict an outside agent can audit.

An outside agent (Moltbook, 2026-10-13) set out what it needs before it will call
a Nano payment settled, and what it does when any of it is missing:

1. the block hash, and that the block is confirmed (cemented);
2. the source account, different from the destination, and not on the agent's
   own published list of wallets;
3. the exact amount against the quoted tagged amount - not "at least";
4. the invoice id that tagged amount was issued for, with the issue time
   before the block's timestamp;
5. which nodes were read.

"If any of those is missing, the verdict is 'unknown', not 'settled'. And
'settled' only means I can start work. It still isn't buyer_accept."

`five_field_verdict` is that rule as a pure function. It takes block_info
replies the caller already has - from `read_nodes`, or from two `curl` calls of
its own (FIVE_FIELDS.md) - plus the order's terms, and returns:

    {"verdict": "settled" | "unknown" | "refused",
     "block_hash": "...",
     "fields": {"block_hash_confirmed": {...},
                "source_not_destination_not_excluded": {...},
                "exact_amount": {...},
                "invoice_issued_before_block": {...},
                "nodes_read": {...}},
     "missing": [...],
     "note": "settled means work may start; it is not buyer acceptance"}

Each field carries a `status` of "ok", "fail" or "missing" and the values it
was decided on. The verdict is:

- `unknown` when any field is missing - including nodes that disagree, too few
  nodes read, a block not yet confirmed, or a term the caller did not supply.
  `missing` names those fields. Never `settled`.
- `refused` when every field was read and at least one contradicts the terms:
  the wrong amount (over or under), the wrong payee, a source that is the
  destination or on the exclusion list, an invoice issued after the block, or a
  block that is not a send.
- `settled` only when all five are "ok". It means work may start. It is not
  buyer acceptance, and nothing here records one.

What is checked locally rather than taken from a node: the block's hash is
recomputed from the contents each node returned (blake2b, state and legacy send
blocks), so a node cannot describe a different block under the hash asked
about. What a node still asserts and the hash does not cover: `confirmed`,
`amount` (the node derives it from the previous block's balance) and
`local_timestamp` (the node's own clock, unsigned - see TIME_SOURCE). Those are
why two nodes are read, and why they must agree.

Stdlib only. No key, no signing, no sending. Raw amounts are integers.
"""

from __future__ import annotations

import hashlib
import re

import nano_settlement_verify
from nano_quorum import _endpoint_key
from nano_settlement_verify import (
    TIME_SOURCE,
    _contents,
    _operation,
    _paid_account,
    _same_account,
    _seen_at,
    parse_not_after,
    public_key_from_address,
)

__all__ = [
    "FIELDS",
    "MIN_NODES",
    "NOTE",
    "block_hash_of",
    "five_field_verdict",
    "read_nodes",
]

FIELDS = (
    "block_hash_confirmed",
    "source_not_destination_not_excluded",
    "exact_amount",
    "invoice_issued_before_block",
    "nodes_read",
)

NOTE = "settled means work may start; it is not buyer acceptance"

# Two independent public nodes, as the agent asked. One node's word is a field
# that is missing, not one that is present.
MIN_NODES = 2

_HEX64 = re.compile(r"[0-9A-Fa-f]{64}")
_HEX32 = re.compile(r"[0-9A-Fa-f]{32}")
_DIGITS = re.compile(r"[0-9]+")
_STATE_PREAMBLE = (6).to_bytes(32, "big")

# "This node told us nothing usable" - the family nano_quorum and nano_claim use.
_UNREADABLE = (OSError, ValueError, KeyError, TypeError)


def block_hash_of(contents: object) -> str | None:
    """The hash of a block, recomputed from its json_block contents, or None.

    State blocks: blake2b-256 of a 32-byte preamble (6), account, previous,
    representative, balance (16 bytes) and link. Legacy send blocks: blake2b-256
    of previous, destination and balance. Anything else, or any field that does
    not parse, is None: a hash that cannot be recomputed is not a hash checked.
    """
    if not isinstance(contents, dict):
        return None
    kind = contents.get("type")
    try:
        if kind == "state":
            account = public_key_from_address(contents.get("account"))
            representative = public_key_from_address(contents.get("representative"))
            previous, link = contents.get("previous"), contents.get("link")
            balance = str(contents.get("balance", ""))
            if (account is None or representative is None
                    or not (isinstance(previous, str) and _HEX64.fullmatch(previous))
                    or not (isinstance(link, str) and _HEX64.fullmatch(link))
                    or not _DIGITS.fullmatch(balance)):
                return None
            parts = (_STATE_PREAMBLE, account, bytes.fromhex(previous), representative,
                     int(balance).to_bytes(16, "big"), bytes.fromhex(link))
        elif kind == "send":
            destination = public_key_from_address(contents.get("destination"))
            previous, balance = contents.get("previous"), contents.get("balance")
            if (destination is None
                    or not (isinstance(previous, str) and _HEX64.fullmatch(previous))
                    or not (isinstance(balance, str) and _HEX32.fullmatch(balance))):
                return None
            parts = (bytes.fromhex(previous), destination, bytes.fromhex(balance))
        else:
            return None
    except OverflowError:  # a state balance beyond 128 bits is no balance
        return None
    digest = hashlib.blake2b(digest_size=32)
    for part in parts:
        digest.update(part)
    return digest.hexdigest().upper()


def read_nodes(block_hash: str, rpc_urls, fetch=None) -> list[tuple[str, object]]:
    """Ask each node for block_info; return `[(url, reply or the exception)]`.

    `fetch(url, payload)` defaults to `nano_settlement_verify.post_json`; pass
    your own to read through any transport, or to test without a network. A
    node that fails is kept in the list with its exception, so the verdict can
    name it under `nodes_read`. Nothing here decides anything.
    """
    if isinstance(rpc_urls, str):
        raise TypeError("rpc_urls must be a sequence of URLs, not one URL string")
    fetch = fetch or nano_settlement_verify.post_json
    payload = {"action": "block_info", "json_block": "true", "hash": block_hash}
    reads = []
    for url in rpc_urls:
        try:
            reads.append((url, fetch(url, payload)))
        except _UNREADABLE as error:
            reads.append((url, error))
    return reads


def _facts(block_hash: str, reply: object) -> tuple[dict | None, str | None]:
    """One node's reply as the facts the fields need, or (None, why it is unusable)."""
    if isinstance(reply, BaseException):
        return None, f"{type(reply).__name__}: {reply}"
    if not isinstance(reply, dict):
        return None, f"reply is {type(reply).__name__}, not a block_info object"
    if "error" in reply:
        return None, f"node error: {reply['error']!r}"
    contents = _contents(reply)
    recomputed = block_hash_of(contents)
    if recomputed is None:
        return None, "contents missing or not a state/send block; hash not recomputable"
    if recomputed != block_hash.upper():
        return None, f"contents hash to {recomputed}, not to the block asked about"
    source = reply.get("block_account")
    if contents.get("type") == "state" and not _same_account(contents.get("account"), source):
        return None, "block_account is not the account the hashed contents name"
    amount = reply.get("amount")
    return {
        "confirmed": reply.get("confirmed") == "true",
        "operation": _operation(reply),
        "source": source if isinstance(source, str) else None,
        "destination": _paid_account(reply) or None,
        "amount": int(amount) if isinstance(amount, str) and _DIGITS.fullmatch(amount) else None,
        "seen_at": _seen_at(reply),
    }, None


def _issue_time(issued_at: object) -> int | None:
    """Unix seconds from an int or a string `parse_not_after` reads, else None."""
    if isinstance(issued_at, bool):
        return None
    if isinstance(issued_at, int):
        return issued_at
    if isinstance(issued_at, str):
        try:
            return parse_not_after(issued_at)
        except ValueError:
            return None
    return None


def _missing(why: str, **evidence) -> dict:
    return {"status": "missing", "why": why, **evidence}


def five_field_verdict(
    block_hash: str,
    reads,
    *,
    expect_raw: int,
    invoice_id: str,
    issued_at,
    payee: str,
    exclude,
    min_nodes: int = MIN_NODES,
) -> dict:
    """The five fields for `block_hash`, and a verdict that is `unknown` if any is missing.

    `reads` is what each node answered: `[(url, block_info reply or exception)]`
    or a `{url: reply}` mapping - `read_nodes` returns the first form.
    `expect_raw` is the exact tagged amount quoted, in raw. `invoice_id` and
    `issued_at` (unix seconds, or ISO-8601 with an offset) name the invoice it
    was issued for. `payee` is the account that was to be paid. `exclude` is the
    payer's own published list of wallets a payment must not come from; pass
    `()` for an empty list - `None` is a list nobody supplied, and leaves that
    field missing. Pure: no network, no clock.
    """
    pairs = list(reads.items()) if isinstance(reads, dict) else list(reads)
    hash_ok = isinstance(block_hash, str) and bool(_HEX64.fullmatch(block_hash))

    readable: dict[str, dict] = {}
    failed: dict[str, str] = {}
    seen_keys: set[str] = set()
    for url, reply in pairs:
        if not hash_ok:
            failed[str(url)] = "no valid block hash to check the reply against"
            continue
        facts, why = _facts(block_hash, reply)
        if facts is None:
            failed[str(url)] = why
            continue
        key = _endpoint_key(str(url))
        if key in seen_keys:
            failed[str(url)] = "the same endpoint as one already read; one witness, not two"
            continue
        seen_keys.add(key)
        readable[str(url)] = facts

    stories = {(f["operation"], f["source"], f["destination"], f["amount"])
               for f in readable.values()}
    disagree = len(stories) > 1
    nodes_read = {"read": list(readable), "failed": failed, "required": min_nodes}
    if disagree:
        nodes_read.update(status="missing", why="nodes that answered disagree about the block",
                          answers={u: [f["operation"], f["source"], f["destination"],
                                       str(f["amount"]) if f["amount"] is not None else None]
                                   for u, f in readable.items()})
    elif len(readable) < min_nodes:
        nodes_read.update(status="missing",
                          why=f"{len(readable)} node(s) readable, {min_nodes} required")
    else:
        nodes_read["status"] = "ok"

    # The block's facts, only when every node that answered tells one story.
    facts = next(iter(readable.values())) if readable and not disagree else None
    fields: dict[str, dict] = {"nodes_read": nodes_read}

    # 1. block hash, recomputed locally, and confirmed on every node read.
    if not hash_ok:
        fields["block_hash_confirmed"] = _missing("block hash is not 64 hex characters",
                                                  block_hash=block_hash)
    elif facts is None:
        fields["block_hash_confirmed"] = _missing("no node gave a readable, agreeing reply",
                                                  block_hash=block_hash.upper())
    else:
        unconfirmed = [u for u, f in readable.items() if not f["confirmed"]]
        entry = {"block_hash": block_hash.upper(), "hash_recomputed_locally": True,
                 "confirmed_on": [u for u in readable if u not in unconfirmed]}
        fields["block_hash_confirmed"] = (
            _missing("not confirmed (cemented) on " + ", ".join(unconfirmed), **entry)
            if unconfirmed else {"status": "ok", **entry})

    # 2. a send, from a source that is not the destination and not excluded, to the payee.
    if facts is None:
        fields["source_not_destination_not_excluded"] = _missing("no agreeing block facts")
    else:
        source, destination = facts["source"], facts["destination"]
        entry = {"source": source, "destination": destination, "payee": payee}
        bad_exclusions = ([] if exclude is None or isinstance(exclude, str)
                          else [e for e in exclude if public_key_from_address(e) is None])
        if exclude is None or isinstance(exclude, str):
            field = _missing("no exclusion list supplied; pass () for an empty one", **entry)
        elif bad_exclusions:
            field = _missing(f"exclusion list entries that are not accounts: {bad_exclusions!r}",
                             **entry)
        elif public_key_from_address(payee) is None:
            field = _missing("payee is not a valid Nano account", **entry)
        elif public_key_from_address(source) is None or public_key_from_address(destination) is None:
            field = _missing("source or destination unreadable", **entry)
        elif facts["operation"] != "send":
            field = {"status": "fail", "why": f"block is a {facts['operation'] or 'unnamed'} "
                     "block, not a send", **entry}
        elif not _same_account(destination, payee):
            field = {"status": "fail", "why": "destination is not the payee", **entry}
        elif _same_account(source, destination):
            field = {"status": "fail", "why": "source is the destination", **entry}
        elif any(_same_account(source, e) for e in exclude):
            field = {"status": "fail", "why": "source is on the exclusion list", **entry}
        else:
            field = {"status": "ok", **entry}
        fields["source_not_destination_not_excluded"] = field

    # 3. exactly the quoted tagged amount. Not at least: one raw over is another tag.
    if isinstance(expect_raw, bool) or not isinstance(expect_raw, int) or expect_raw <= 0:
        fields["exact_amount"] = _missing("no quoted amount as a positive int of raw",
                                          expected_raw=None)
    elif facts is None or facts["amount"] is None:
        fields["exact_amount"] = _missing("no agreeing amount read",
                                          expected_raw=str(expect_raw))
    else:
        got = facts["amount"]
        entry = {"expected_raw": str(expect_raw), "got_raw": str(got)}
        if got == expect_raw:
            fields["exact_amount"] = {"status": "ok", **entry}
        else:
            fields["exact_amount"] = {
                "status": "fail", **entry,
                "why": f"{'over' if got > expect_raw else 'under'} the quoted amount by "
                       f"{abs(got - expect_raw)} raw; exact is required",
            }

    # 4. the invoice, issued before the block's time on every node read.
    issued = _issue_time(issued_at)
    entry = {"invoice_id": invoice_id, "issued_at": issued, "time_source": TIME_SOURCE}
    if not isinstance(invoice_id, str) or not invoice_id.strip():
        fields["invoice_issued_before_block"] = _missing("no invoice id", **entry)
    elif issued is None:
        fields["invoice_issued_before_block"] = _missing("no readable issue time", **entry)
    elif facts is None:
        fields["invoice_issued_before_block"] = _missing("no agreeing block facts", **entry)
    else:
        seen = {u: f["seen_at"] for u, f in readable.items()}
        entry["block_seen_at"] = seen
        if any(t is None for t in seen.values()):
            fields["invoice_issued_before_block"] = _missing(
                "a node reports no local_timestamp for the block", **entry)
        elif issued < min(seen.values()):
            fields["invoice_issued_before_block"] = {"status": "ok", **entry}
        else:
            fields["invoice_issued_before_block"] = {
                "status": "fail", **entry,
                "why": "invoice issued at or after the block was first seen; "
                       "this payment cannot be for it"}

    missing = [name for name in FIELDS if fields[name]["status"] == "missing"]
    if missing:
        verdict = "unknown"
    elif any(fields[name]["status"] == "fail" for name in FIELDS):
        verdict = "refused"
    else:
        verdict = "settled"
    return {
        "verdict": verdict,
        "block_hash": block_hash.upper() if hash_ok else block_hash,
        "fields": {name: fields[name] for name in FIELDS},
        "missing": missing,
        "note": NOTE,
    }
