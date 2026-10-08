"""Was a Nano send claimed? One read of the public chain, one JSON answer.

`verify` answers "did this send settle?". This answers the question after it:
*did the account it paid ever take the money?* A Nano send does not move funds
into the destination by itself; the destination has to publish a receive (or,
for a new account, an open) block naming the send. Until it does, the send sits
on the ledger as receivable, and an account that has never received anything
does not exist on the ledger at all.

    python3 nano_claim.py <send-block-hash> [rpc-url]

prints one JSON object:

    {"send_hash": "...", "from": "nano_1...", "to": "nano_3...",
     "amount_raw": "10000000000000000000000000", "send_confirmed": true,
     "outcome": "receivable", "receive_hash": null, "to_account_opened": false,
     "read_at": "2026-10-08T12:00:00Z", "source": "https://rpc.nano.to",
     "error": null}

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

Every call is a read: `blocks_info`, `account_info`, `account_history`. No key,
no signing, no sending. Raw amounts are integer strings: 1 XNO is `10**30` raw.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import nano_settlement_verify
from nano_settlement_verify import is_valid_account

__all__ = [
    "Refused",
    "HISTORY_BOUND",
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


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    raise _Unknown(
        f"no receive linking this send in the newest {len(history)} block(s) of "
        f"{account} (bound {bound}); it may be older, or not on this node"
    )


def claim_status(send_hash, rpc_url: str, *, history_bound: int = HISTORY_BOUND,
                 now=_now) -> dict:
    """Read once whether the send `send_hash` was claimed by the account it paid.

    Returns the JSON-ready dict described in the module docstring. Raises
    `Refused` for a malformed hash or a block that is not a send. Every other
    failure - a node call that raises or answers garbage - comes back as
    outcome "unknown" with `error` set, never as an exception and never as a
    clean outcome.
    """
    if not isinstance(send_hash, str) or not _HASH.fullmatch(send_hash):
        raise Refused("malformed_hash", f"{send_hash!r} is not 64 hex characters")
    send_hash = send_hash.upper()
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
    try:
        block = _send(send_hash, rpc_url)
        operation = nano_settlement_verify._operation(block)
        if operation != "send":
            raise Refused("not_a_send", f"block {send_hash} is a {operation or 'unknown'} block")
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
        if flag == "1":
            status["outcome"] = "receivable"
            return status
        if not status["send_confirmed"]:
            raise _Unknown("the send is neither confirmed nor receivable")
        if not status["to_account_opened"]:
            raise _Unknown("the send is not receivable, yet the destination is unopened")
        receive_hash, confirmed = _receive_of(send_hash, destination, rpc_url, history_bound)
        status["receive_hash"] = receive_hash
        if not confirmed:
            raise _Unknown(f"receive {receive_hash} is not confirmed yet")
        status["outcome"] = "claimed"
        return status
    except Refused:
        raise
    except _Unknown as error:
        status["error"] = str(error)
    except _UNREADABLE as error:
        status["error"] = f"{type(error).__name__}: {error}"
    status["outcome"] = "unknown"
    return status


def exit_code(status: dict) -> int:
    """0 for an answered read (claimed, receivable), 2 for unknown, 3 for refused."""
    return {"claimed": 0, "receivable": 0, "refused": 3}.get(status.get("outcome"), 2)


def _main(argv: list[str]) -> int:
    """`python3 nano_claim.py <send-block-hash> [rpc-url]`."""
    import sys

    if not argv or len(argv) > 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: python3 nano_claim.py <send-block-hash> [rpc-url]", file=sys.stderr)
        return 64
    send_hash = argv[0]
    rpc_url = argv[1] if len(argv) > 1 else "https://rpc.nano.to"
    try:
        status = claim_status(send_hash, rpc_url)
    except Refused as error:
        status = {
            "send_hash": send_hash,
            "outcome": "refused",
            "reason": error.reason,
            "error": error.detail,
            "read_at": _now(),
            "source": rpc_url,
        }
    print(json.dumps(status))
    return exit_code(status)


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_main(sys.argv[1:]))
