"""Was a Nano send claimed? One read of the public chain, one JSON answer.

`verify` answers "did this send settle?". This answers the question after it:
*did the account it paid ever take the money?* A Nano send does not move funds
into the destination by itself; the destination has to publish a receive (or,
for a new account, an open) block naming the send. Until it does, the send sits
on the ledger as receivable, and an account that has never received anything
does not exist on the ledger at all.

    python3 nano_claim.py <send-block-hash> [rpc-url] [--nodes URL ...]
        [--attempt N] [--first-unknown-at T] [--retry-after S]
        [--escalate-reads N] [--escalate-hours H]

prints one JSON object:

    {"send_hash": "...", "from": "nano_1...", "to": "nano_3...",
     "amount_raw": "10000000000000000000000000", "send_confirmed": true,
     "outcome": "receivable", "receive_hash": null, "to_account_opened": false,
     "read_at": "2026-10-08T12:00:00Z", "source": "https://rpc.nano.to",
     "error": null, "absence_scope": {...}, "reconcile": null}

`outcome` is exactly one of:

- `claimed`    - a confirmed receive/open block on the destination's chain
                 names this send as its link; `receive_hash` is that block.
- `receivable` - the node reports the send as still waiting to be received.
                 `to_account_opened` says whether the destination exists yet.
- `unknown`    - the read did not settle the question: a node call failed or
                 answered something that is not a node reply, the receive was
                 not found within the history bound, or the facts disagree.
                 `error` says which. Exit code 2.
- `refused`    - the input is not a send this can ask about: a malformed hash
                 or a block that is not a send. Exit code 3.

A check that could not run never comes back as `claimed` or `receivable`. Only
those two exit 0.

`unknown` is a state with a way out, not "wait forever". It carries `reconcile`:
the `operation_id` to look up again (the send hash - the same identity, never a
new send), `resubmit: false` (do not re-send on unknown), the `nodes_checked`
and `checked_at` of this read, `retry_after_s` and `next_check_at`, and
`escalate_after`: after `unknown_reads` unknown reads or `hours` since the first
one, `escalate` turns true, `action` reads "escalate" and the caller stops
re-checking and takes it to a person. The caller carries `attempt` and
`first_unknown_at` from one read to the next. Answered reads have
`reconcile: null`.

Not found is never read as never sent. Whenever a node did not have the send,
the receive, or the destination account, `absence_scope` says what was not
found, on which `nodes`, `at` what instant, and in what `window`, with
`proves_absence: false`. With several nodes (`--nodes`), a node that found the
send outranks one that did not; not found on all of them stays `unknown`.

Every call is a read: `blocks_info`, `account_info`, `account_history`. No key,
no signing, no sending. Raw amounts are integer strings: 1 XNO is `10**30` raw.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import nano_settlement_verify
from nano_settlement_verify import is_valid_account

__all__ = [
    "Refused",
    "HISTORY_BOUND",
    "RETRY_AFTER_S",
    "ESCALATE_AFTER_READS",
    "ESCALATE_AFTER_HOURS",
    "OUTCOMES",
    "claim_status",
    "exit_code",
]

# How many of the destination's newest blocks are searched for the receive.
# The receive of a send is newer than the send, and a starter's destination is
# usually a young chain, so this is a bound on one read, not a tuning knob: a
# receive older than this is reported as not found within the bound, never as
# not claimed.
HISTORY_BOUND = 500

# What an `unknown` tells the caller to do next. A confirmed Nano send settles in
# about a second, so an unknown that outlives a dozen reads five minutes apart
# (an hour) is a node or network problem a person should look at; 24 hours is
# the outer bound for a caller that re-checks less often.
RETRY_AFTER_S = 300
ESCALATE_AFTER_READS = 12
ESCALATE_AFTER_HOURS = 24

OUTCOMES = ("claimed", "receivable", "unknown", "refused")

_HASH = re.compile(r"[0-9A-Fa-f]{64}")

# The same family nano_payers treats as "this endpoint told us nothing usable".
_UNREADABLE = (OSError, ValueError, KeyError, TypeError)


class Refused(Exception):
    """The input is not a send block this can ask about. Carries `reason`."""

    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


class _Unknown(Exception):
    """A read that did not answer the question; becomes outcome "unknown"."""


class _NotFound(_Unknown):
    """A node did not have something. Unknown, and scoped: `what` and `window`."""

    def __init__(self, message: str, what: str, window: str):
        self.what = what
        self.window = window
        super().__init__(message)


_TIME = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_TIME)


def _at(text: str) -> datetime:
    return datetime.strptime(text, _TIME).replace(tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime(_TIME)


def _call(rpc_url: str, payload: dict) -> dict:
    # Looked up per call so that a stub takes effect, as nano_payers does.
    reply = nano_settlement_verify.post_json(rpc_url, payload)
    if not isinstance(reply, dict):
        raise _Unknown(
            f"{payload['action']} answered {type(reply).__name__}, not a node reply"
        )
    return reply


def _send(send_hash: str, rpc_url: str) -> dict:
    """The send block, with the node's receivable flag, from one blocks_info call."""
    reply = _call(
        rpc_url,
        {
            "action": "blocks_info",
            "json_block": "true",
            "receivable": "true",
            "hashes": [send_hash],
        },
    )
    if "error" in reply:
        # A node that lacks the block may simply be behind; that is not proof
        # the block does not exist, so it is unknown rather than refused.
        if "not found" in str(reply["error"]).lower():
            raise _NotFound(f"blocks_info: {reply['error']}", "send block",
                            "each listed node's ledger at the instant read")
        raise _Unknown(f"blocks_info: {reply['error']}")
    blocks = reply.get("blocks")
    if not isinstance(blocks, dict) or not isinstance(blocks.get(send_hash), dict):
        raise _Unknown("blocks_info did not return this block")
    return blocks[send_hash]


def _opened(account: str, rpc_url: str) -> bool:
    reply = _call(rpc_url, {"action": "account_info", "account": account})
    if "error" in reply:
        if reply["error"] == "Account not found":
            return False
        raise _Unknown(f"account_info: {reply['error']}")
    if "frontier" not in reply:
        raise _Unknown(f"account_info carried no frontier; keys: {sorted(reply)}")
    return True


def _receive_of(send_hash: str, account: str, rpc_url: str, bound: int):
    """(receive hash, confirmed) of the block on `account` linking `send_hash`.

    Asks for `bound` blocks of history. A node that honours `raw` puts each
    block's link in the row; one that does not (rpc.nano.to, measured
    2026-10-08) gets a second, batched blocks_info call for the receive rows.
    """
    reply = _call(
        rpc_url,
        {"action": "account_history", "account": account, "count": str(bound), "raw": "true"},
    )
    history = reply.get("history")
    if history == "" or history is None:
        if "error" in reply:
            raise _Unknown(f"account_history: {reply['error']}")
        history = []
    if not isinstance(history, list):
        raise _Unknown(f"account_history answered history={type(history).__name__}")
    candidates = []
    for row in history:
        if not isinstance(row, dict):
            raise _Unknown("account_history row is not an object")
        kind = row.get("subtype") or row.get("type")
        if kind not in ("receive", "open"):
            continue
        if "link" in row:
            if str(row["link"]).upper() == send_hash:
                return str(row["hash"]).upper(), row.get("confirmed") == "true"
            continue
        candidates.append(str(row["hash"]).upper())
    if candidates:
        blocks = _call(
            rpc_url, {"action": "blocks_info", "json_block": "true", "hashes": candidates}
        ).get("blocks")
        if not isinstance(blocks, dict):
            raise _Unknown("blocks_info on the destination's receives returned no blocks")
        for block_hash in candidates:
            block = blocks.get(block_hash)
            if not isinstance(block, dict):
                raise _Unknown(f"blocks_info did not return receive {block_hash}")
            link = nano_settlement_verify._contents(block).get("link")
            if str(link or "").upper() == send_hash:
                return block_hash, block.get("confirmed") == "true"
    raise _NotFound(
        f"no receive linking this send in the newest {len(history)} block(s) of "
        f"{account} (bound {bound}); it may be older, or not on this node",
        "receive linking this send",
        f"the newest {bound} blocks of {account} on each listed node at the instant read",
    )


def _read_one(send_hash: str, rpc_url: str, history_bound: int, now) -> tuple[dict, object]:
    """One node's answer: (status, the _NotFound behind it or None)."""
    status = {
        "send_hash": send_hash,
        "from": None,
        "to": None,
        "amount_raw": None,
        "send_confirmed": None,
        "outcome": "unknown",
        "receive_hash": None,
        "to_account_opened": None,
        "read_at": now(),
        "source": rpc_url,
        "error": None,
    }
    absent = None
    try:
        block = _send(send_hash, rpc_url)
        operation = nano_settlement_verify._operation(block)
        # An operation of "" is this node's reply being unreadable, not a fact
        # about the block - and `Refused` is terminal: no `reconcile`, no
        # `absence_scope`, no retry, and it is re-raised below past the `_Unknown`
        # arm, so it leaves `claim_status`'s node loop and the remaining nodes are
        # never asked. The verdict then depended on which node happened to be
        # first. The destination two lines down already treats an unreadable
        # answer as `_Unknown`; so does this one now. A block that names a real
        # operation other than "send" is still `Refused`.
        if not operation:
            raise _Unknown(f"the node's reply for block {send_hash} names no operation")
        if operation != "send":
            raise Refused("not_a_send", f"block {send_hash} is a {operation} block")
        destination = nano_settlement_verify._paid_account(block)
        if not is_valid_account(destination):
            raise _Unknown(f"the send's destination {destination!r} is not a valid account")
        amount = str(block["amount"])
        if not amount.isdigit():
            raise _Unknown(f"the send's amount {amount!r} is not raw")
        status.update(
            {
                "from": str(block["block_account"]),
                "to": destination,
                "amount_raw": amount,
                "send_confirmed": block.get("confirmed") == "true",
            }
        )
        flag = block.get("receivable", block.get("pending"))
        if flag not in ("0", "1"):
            raise _Unknown(f"the node did not say whether the send is receivable ({flag!r})")
        status["to_account_opened"] = _opened(destination, rpc_url)
        if not status["to_account_opened"]:
            absent = _NotFound("account_info: Account not found", "destination account",
                               "each listed node's ledger at the instant read")
        if flag == "1":
            status["outcome"] = "receivable"
            return status, absent
        if not status["send_confirmed"]:
            raise _Unknown("the send is neither confirmed nor receivable")
        if not status["to_account_opened"]:
            raise _Unknown("the send is not receivable, yet the destination is unopened")
        receive_hash, confirmed = _receive_of(send_hash, destination, rpc_url, history_bound)
        status["receive_hash"] = receive_hash
        if not confirmed:
            raise _Unknown(f"receive {receive_hash} is not confirmed yet")
        status["outcome"] = "claimed"
        return status, None
    except Refused:
        raise
    except _NotFound as error:
        status["error"] = str(error)
        absent = error
    except _Unknown as error:
        status["error"] = str(error)
    except _UNREADABLE as error:
        status["error"] = f"{type(error).__name__}: {error}"
    status["outcome"] = "unknown"
    return status, absent


def claim_status(send_hash, rpc_url, *, history_bound: int = HISTORY_BOUND, now=_now,
                 attempt: int = 1, first_unknown_at: str | None = None,
                 retry_after_s: int = RETRY_AFTER_S,
                 escalate_after_reads: int = ESCALATE_AFTER_READS,
                 escalate_after_hours: float = ESCALATE_AFTER_HOURS) -> dict:
    """Read whether the send `send_hash` was claimed by the account it paid.

    `rpc_url` is one node URL or a list of them, asked in order: the first
    node that answers `claimed` settles it, a `receivable` answer stands
    unless a later node finds the claim, and only when no node answers is the
    outcome "unknown". Returns the JSON-ready dict described in the module
    docstring. Raises `Refused` for a malformed hash or a block that is not a
    send. Every other failure comes back as outcome "unknown" with `error`,
    `absence_scope` (when something was not found) and `reconcile` set, never
    as an exception and never as a clean outcome. `attempt` and
    `first_unknown_at` are carried by the caller from its earlier unknown reads.
    """
    if not isinstance(send_hash, str) or not _HASH.fullmatch(send_hash):
        raise Refused("malformed_hash", f"{send_hash!r} is not 64 hex characters")
    send_hash = send_hash.upper()
    urls = [rpc_url] if isinstance(rpc_url, str) else list(rpc_url)
    if not urls:
        raise ValueError("no node to read")
    reads = []
    for url in urls:
        status, absent = _read_one(send_hash, url, history_bound, now)
        reads.append((url, status, absent))
        if status["outcome"] == "claimed":
            break
    answered = [r for r in reads if r[1]["outcome"] != "unknown"]
    url, status, absent = answered[-1] if answered else reads[0]
    checked_at = status["read_at"] if answered else reads[-1][1]["read_at"]
    if not answered and len(reads) > 1:
        status["error"] = "; ".join(f"{u}: {s['error']}" for u, s, _ in reads)
    if absent is not None:
        same = [u for u, _, a in reads if a is not None and a.what == absent.what]
        status["absence_scope"] = {
            "not_found": absent.what,
            "nodes": same if not answered else [url],
            "at": status["read_at"],
            "window": absent.window,
            "proves_absence": False,
        }
    else:
        status["absence_scope"] = None
    status["reconcile"] = None
    if answered:
        return status

    first = first_unknown_at or checked_at
    deadline = _at(first) + timedelta(hours=escalate_after_hours)
    escalate = attempt >= escalate_after_reads or _at(checked_at) >= deadline
    status["reconcile"] = {
        "operation_id": send_hash,
        "resubmit": False,
        "nodes_checked": [u for u, _, _ in reads],
        "checked_at": checked_at,
        "attempt": attempt,
        "retry_after_s": retry_after_s,
        "next_check_at": None if escalate
        else _stamp(_at(checked_at) + timedelta(seconds=retry_after_s)),
        "escalate_after": {
            "unknown_reads": escalate_after_reads,
            "hours": escalate_after_hours,
            "first_unknown_at": first,
            "deadline": _stamp(deadline),
        },
        "escalate": escalate,
        "action": "escalate" if escalate else "recheck",
    }
    return status


def exit_code(status: dict) -> int:
    """0 for an answered read (claimed, receivable), 2 for unknown, 3 for refused."""
    return {"claimed": 0, "receivable": 0, "refused": 3}.get(status.get("outcome"), 2)


USAGE = (
    "usage: python3 nano_claim.py <send-block-hash> [rpc-url] [--nodes URL ...]\n"
    "       [--attempt N] [--first-unknown-at YYYY-MM-DDTHH:MM:SSZ]\n"
    "       [--retry-after SECONDS] [--escalate-reads N] [--escalate-hours H]"
)


class _Usage(Exception):
    pass


def _parse(argv: list[str]):
    import argparse

    class Parser(argparse.ArgumentParser):
        # argparse exits 2 on a bad flag, which is this tool's "unknown".
        def error(self, message):
            raise _Usage(message)

    parser = Parser(add_help=False)
    parser.add_argument("send_hash")
    parser.add_argument("rpc_url", nargs="?")
    parser.add_argument("--nodes", nargs="+", default=[])
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--first-unknown-at")
    parser.add_argument("--retry-after", type=int, default=RETRY_AFTER_S)
    parser.add_argument("--escalate-reads", type=int, default=ESCALATE_AFTER_READS)
    parser.add_argument("--escalate-hours", type=float, default=ESCALATE_AFTER_HOURS)
    args = parser.parse_args(argv)
    if args.attempt < 1 or args.retry_after < 0 or args.escalate_reads < 1 \
            or args.escalate_hours < 0:
        raise _Usage("--attempt and --escalate-reads are at least 1; times are not negative")
    if args.first_unknown_at is not None:
        try:
            _at(args.first_unknown_at)
        except ValueError:
            raise _Usage("--first-unknown-at is YYYY-MM-DDTHH:MM:SSZ") from None
    return args


def _main(argv: list[str]) -> int:
    """`python3 nano_claim.py <send-block-hash> [rpc-url] [--nodes URL ...] ...`."""
    import sys

    try:
        args = _parse(argv)
    except _Usage as error:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print(f"{USAGE}\n{error}", file=sys.stderr)
        return 64
    urls = ([args.rpc_url] if args.rpc_url else []) + args.nodes
    urls = list(dict.fromkeys(urls)) or ["https://rpc.nano.to"]
    try:
        status = claim_status(
            args.send_hash, urls if len(urls) > 1 else urls[0],
            attempt=args.attempt, first_unknown_at=args.first_unknown_at,
            retry_after_s=args.retry_after, escalate_after_reads=args.escalate_reads,
            escalate_after_hours=args.escalate_hours,
        )
    except Refused as error:
        status = {
            "send_hash": args.send_hash,
            "outcome": "refused",
            "reason": error.reason,
            "error": error.detail,
            "read_at": _now(),
            "source": urls[0],
        }
    print(json.dumps(status))
    return exit_code(status)


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_main(sys.argv[1:]))
