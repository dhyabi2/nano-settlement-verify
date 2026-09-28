"""How many INDEPENDENT payers a list of Nano payer accounts holds.

Counting paying keys does not count buyers: one operator can open ten accounts and
pay from each. What an operator cannot hide on Nano is where the money came from.
Every account is opened by receiving a send, so its open block names the send that
funded it, and that send sits on the funder's chain. This module walks that link
back `hops` times for each payer and groups payers that share a funder, or that
funded one another. Each group is counted once.

It also answers the other half of the question: a payer whose funding traces back
to the seller itself is the seller's own money moving in a circle, and is reported
separately and never counted as independent demand.

What the count is and is not. It is a LOWER BOUND on common control: two payers in
one group are shown on chain to share an origin. It is NOT PROOF of independence:
an operator who funds each key from a different exchange withdrawal will read as
several payers. And one exchange withdraws to many strangers, so an exchange's hot
wallet would merge them - pass its account in `ignore` (the caller decides which
accounts are hubs; there is deliberately no built-in registry). The default is one
hop, the direct funder, because every extra hop reaches further towards the hubs.

Standard library only; it only reads (`account_info`, `block_info`), through the
same bounded `post_json` as `nano_settlement_verify`. No signing, no sending.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import nano_settlement_verify
from nano_settlement_verify import _account_body, _contents

__all__ = ["funding_chain", "independence", "Independence"]


def _key(address: object) -> str | None:
    """One key per account, however it is spelled (`nano_` or `xrb_`)."""
    return _account_body(address)


def _check_hops(hops: object) -> int:
    if isinstance(hops, bool) or not isinstance(hops, int):
        raise TypeError(f"hops must be an int, got {type(hops).__name__}")
    if hops < 1:
        raise ValueError(f"hops must be at least 1, got {hops}")
    return hops


_UNOPENED = object()


def _funder(account: str, rpc_url: str):
    """The account whose send opened `account`; None when that cannot be read;
    _UNOPENED when the node has no such account."""
    post = nano_settlement_verify.post_json  # looked up per call so a stub takes effect
    info = post(rpc_url, {"action": "account_info", "account": account})
    if "error" in info:
        return _UNOPENED
    if not info.get("open_block"):
        return None
    opened = post(rpc_url, {"action": "block_info", "json_block": "true", "hash": info["open_block"]})
    if "error" in opened:
        return None
    contents = _contents(opened)
    # A state open block carries the funding send's hash as its link; a pre-state
    # open block carries it as source.
    source = contents.get("link") or contents.get("source")
    if not source:
        return None
    send = post(rpc_url, {"action": "block_info", "json_block": "true", "hash": source})
    if "error" in send:
        return None
    funder = send.get("block_account") or _contents(send).get("account")
    return str(funder) if funder else None


def funding_chain(account: str, rpc_url: str, hops: int = 1) -> list[str] | None:
    """The accounts that funded `account`, nearest first, at most `hops` long.

    None when the account is not opened (nothing has ever been received). The
    chain stops early at an account whose origin cannot be read or at a loop.
    """
    hops = _check_hops(hops)
    first = _funder(account, rpc_url)
    if first is _UNOPENED:
        return None
    if first is None:
        return []
    chain = [first]
    seen = {_key(account), _key(first)}
    while len(chain) < hops:
        nxt = _funder(chain[-1], rpc_url)
        if nxt is None or nxt is _UNOPENED or _key(nxt) in seen:
            break
        seen.add(_key(nxt))
        chain.append(nxt)
    return chain


@dataclass
class Independence:
    """The answer. `independent` is the number of groups, each counted once."""

    keys: int
    independent: int
    groups: list[list[str]]
    funded_by_seller: list[str]
    unopened: list[str]
    hops: int
    chains: dict[str, list[str]] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "keys": self.keys,
                "independent": self.independent,
                "groups": self.groups,
                "funded_by_seller": self.funded_by_seller,
                "unopened": self.unopened,
                "hops": self.hops,
                "chains": self.chains,
            }
        )


def independence(
    payers: list[str],
    rpc_url: str,
    hops: int = 1,
    seller: str | None = None,
    ignore: list[str] | tuple[str, ...] = (),
) -> Independence:
    """Group `payers` by shared funding origin and count the groups.

    `seller`: a payer whose chain contains this account is listed in
    `funded_by_seller` and left out of every group. `ignore`: hub accounts (an
    exchange hot wallet, a faucet) that do not tie two payers together. Both are
    compared as accounts, in either spelling.
    """
    hops = _check_hops(hops)
    seller_key = _key(seller) if seller else None
    hubs = {_key(a) for a in ignore} - {None}

    # One entry per account, keeping the first spelling the caller used.
    unique: dict[str, str] = {}
    for payer in payers:
        k = _key(payer)
        if k is not None and k not in unique:
            unique[k] = payer

    chains: dict[str, list[str]] = {}
    unopened: list[str] = []
    circular: list[str] = []
    counted: list[str] = []
    for k, payer in unique.items():
        chain = funding_chain(payer, rpc_url, hops)
        if chain is None:
            unopened.append(payer)
            continue
        chains[payer] = chain
        if seller_key is not None and seller_key in {_key(a) for a in chain}:
            circular.append(payer)
        else:
            counted.append(payer)

    # Union-find: two payers join when their {self + chain} sets share an account.
    parent = {p: p for p in counted}

    def root(p: str) -> str:
        while parent[p] != p:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p

    owner: dict[str, str] = {}
    for payer in counted:
        for account in [payer, *chains[payer]]:
            k = _key(account)
            if k is None or k in hubs:
                continue
            if k in owner:
                parent[root(payer)] = root(owner[k])
            else:
                owner[k] = payer

    grouped: dict[str, list[str]] = {}
    for payer in counted:
        grouped.setdefault(root(payer), []).append(payer)
    groups = sorted((sorted(g) for g in grouped.values()), key=lambda g: g[0])

    return Independence(
        keys=len(unique),
        independent=len(groups),
        groups=groups,
        funded_by_seller=circular,
        unopened=unopened,
        hops=hops,
        chains=chains,
    )
