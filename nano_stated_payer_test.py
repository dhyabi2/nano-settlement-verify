"""One verdict per payer against a stated independence test, re-runnable by anyone.

    python3 nano_stated_payer_test.py --payee nano_... --payer nano_... [--payer ...]
        [--excluded nano_...]... [--exchange nano_...]... [--hops 1] [--rpc URL]... [--json]

The test, as a seller stated it for a published payer count: a payer is independent
when it was "funded by neither us nor you, one hop back, exchange hot wallets set
aside". Read here as:

  * funder = the account whose send opened the payer (its open block's link or
    source). One hop is the direct funder; `--hops N` walks that link N times.
  * `not-independent`: a funder within the hops is the payee or an `--excluded`
    party (or the payer is one of them itself, hop 0). The output names which and
    at which hop. The payee funding its own payer is the seller's money in a circle.
  * `not-determinable`: the funding that is visible ends at an exchange hot wallet
    (`--exchange`) with no non-exchange funder before it - "not determinable from
    funding alone". Nothing behind an exchange is walked: it is the exchange's
    history, shared by every customer it paid.
  * `independent`: funded within the hops by none of the above and not only by an
    exchange.
  * `unopened`: the node has no open block for the account. `unreadable`: a block
    the verdict needs could not be read, the address is malformed, or two nodes
    told different stories. Neither is ever counted as independent.

Each verdict carries the deciding hop's witness - the account, its open block hash,
the funding send hash and the funder - plus the whole trail read, so the verdict can
be re-checked on any node with `block_info`.

What it does not test: receives after the open block. The stated test is the
first funder only; an account opened by a dust send from a stranger and funded
later by a party reads as independent here. So every `independent` verdict carries
`caveat` ("opener only") and `opener`, from one `account_history` read of the
payer: the opening amount, the number and raw total of later receives, and the
opener's share of everything received - a small share says the opener is not where
the money came from. Those fields are `unreadable` when that read fails; the
verdict stands. Run `nano_funding.py` on the payment block for who funded a payment.

A funder the node names must itself be a valid Nano account (prefix, length,
alphabet, checksum); anything else is `unreadable`, never a stranger.

`nano_` and `xrb_` spellings of one key are one payer: `payers` holds one entry per
distinct key (malformed payers first), and `counts` sum to that, not to the number
of `--payer` arguments.

Standard library only; reads `account_info`, `block_info` (through
`nano_independence`'s funding reader) and, for `independent` payers,
`account_history`. With several `--rpc` every node that answers must give the same
verdict and witness, or the payer is `unreadable`; a node that fails is listed in
`nodes_failed`, and `fewer_nodes` is true when the verdict came from fewer nodes
than were asked.

Exit 0: every payer got a ledger verdict. 2: any payer is `unopened` or
`unreadable` (argparse also exits 2 on a usage error). 3: a `--payee`,
`--excluded` or `--exchange` that is not a Nano account, or a bad `--hops`.
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys

import nano_independence
from nano_independence import _check_hops, _key, _utc_now
from nano_settlement_verify import public_key_from_address

STATED_TEST = "Funded by neither us nor you, one hop back, exchange hot wallets set aside"
CARVE_OUT = "not determinable from funding alone"
VERDICTS = ("independent", "not-independent", "not-determinable", "unopened", "unreadable")
DEFAULT_RPC = "https://rpc.nano.to"
CAVEAT = ("opener only: the funder tested is the account whose send opened the payer; "
          "receives after the open block were not tested")
HISTORY_CAP = 4096  # the most blocks of a payer's history read to total its later receives
EXIT_BAD_INPUT = 3
# What a node read can raise; any of them makes that node's answer for that payer unreadable.
NODE_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException)


def _witness(step: dict | None) -> dict | None:
    if not step:
        return None
    return {k: step.get(k) for k in ("account", "open_block", "funding_send", "funder")}


def _read(payer: str, rpc_url: str, payee_key: str, excluded: dict, exchanges: set, hops: int, post) -> dict:
    """The verdict for one payer from one node."""
    trail: list[dict] = []

    def out(verdict, why, hop=None, step=None, party=None):
        return {"payer": payer, "verdict": verdict, "why": why, "deciding_hop": hop,
                "party": party, "witness": _witness(step), "trail": trail}

    def role(account):
        k = _key(account)
        if k == payee_key:
            return "payee"
        if k in excluded:
            return f"excluded:{excluded[k]}"
        return None

    if role(payer):
        return out("not-independent", f"the payer is the {role(payer)} itself", 0, party=role(payer))
    current, seen = payer, {_key(payer)}
    for hop in range(1, hops + 1):
        funder, step = nano_independence._funder(current, rpc_url, post)
        if step is not None:
            trail.append(step)
        if funder is nano_independence._UNOPENED:
            if hop == 1:
                return out("unopened", "the node has no open block for this account")
            return out("unreadable", f"the hop-{hop - 1} funder {current} reads as unopened", hop)
        if funder is None:
            return out("unreadable", f"the funding send of {current} could not be read", hop, step)
        if public_key_from_address(funder) is None:
            # The node named something that is not a Nano account (wrong type, bad
            # checksum, garbage): it can match no list, so it must not read as a stranger.
            return out("unreadable", f"the hop-{hop} funder the node gave is not a Nano account: {funder!r}"[:200],
                       hop, step)
        if role(funder):
            return out("not-independent", f"hop {hop} funder is the {role(funder)}", hop, step, role(funder))
        if _key(funder) in exchanges:
            if hop == 1:
                return out("not-determinable", CARVE_OUT, hop, step, "exchange")
            break  # a non-exchange funder was already seen; nothing behind an exchange is walked
        if _key(funder) in seen:
            break  # a funding loop: every account in it has been checked
        seen.add(_key(funder))
        current = funder
    r = out("independent", "no funder within the hops is the payee, an excluded party, or only an exchange",
            1, trail[0])
    r["caveat"] = CAVEAT
    r["opener"] = _opener_summary(payer, trail[0], rpc_url, post)
    return r


def _unreadable_opener(why: str) -> dict:
    return {"amount_raw": "unreadable", "later_receives": "unreadable", "later_receives_raw": "unreadable",
            "opener_share": "unreadable", "unreadable_why": why[:200]}


def _raw(value) -> int | None:
    text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return int(text) if text.isdigit() else None


def _share(part: int, total: int) -> str:
    """part/total as a percentage with four decimals, rounded down, in integers."""
    if total <= 0:
        return "unreadable"
    q = part * 1000000 // total
    return f"{q // 10000}.{q % 10000:04d}%"


def _opener_summary(payer: str, step: dict, rpc_url: str, post) -> dict:
    """What one account_history read gives cheaply about an `independent` payer: the
    opening send's amount, how many receives came after it and their total, and the
    opener's share of everything received. Any failure gives `unreadable` fields."""
    post = post or nano_independence.nano_settlement_verify.post_json
    count = step.get("block_count")
    open_block = str(step.get("open_block") or "").upper()
    if not isinstance(count, int) or count < 1:
        return _unreadable_opener("account_info gave no block count")
    if count > HISTORY_CAP:
        return _unreadable_opener(f"{count} blocks, more than the {HISTORY_CAP} read")
    try:
        reply = post(rpc_url, {"action": "account_history", "account": payer, "count": str(count),
                               "reverse": "true"})
    except NODE_ERRORS as e:
        return _unreadable_opener(f"account_history: {type(e).__name__}: {e}")
    if not isinstance(reply, dict):
        return _unreadable_opener("account_history reply is not an object")
    history = reply.get("history")
    if "error" in reply or not isinstance(history, list):
        return _unreadable_opener(f"account_history: {reply.get('error', 'no history list')}")
    if reply.get("next") or len(history) > count:
        return _unreadable_opener("account_history did not give the whole history in one read")
    opener, later, later_raw = None, 0, 0
    for row in history:
        if not isinstance(row, dict):
            return _unreadable_opener("account_history row is not an object")
        kind = row.get("subtype") or row.get("type")
        if kind not in ("receive", "open"):
            continue
        amount = _raw(row.get("amount"))
        if amount is None:
            return _unreadable_opener("a receive in account_history has no raw amount")
        if str(row.get("hash") or "").upper() == open_block:
            opener = amount
        else:
            later += 1
            later_raw += amount
    if opener is None:
        return _unreadable_opener("the open block is not in account_history")
    return {"amount_raw": str(opener), "later_receives": later, "later_receives_raw": str(later_raw),
            "opener_share": _share(opener, opener + later_raw)}


def _story(r: dict) -> str:
    w = r["witness"] or {}
    return json.dumps([r["verdict"], r["deciding_hop"], r["party"], w.get("open_block"),
                       w.get("funding_send"), w.get("funder")])


def stated_test(payee: str, payers, rpc_urls, excluded=(), exchange=(), hops: int = 1, post=None) -> dict:
    """Apply the stated test to each payer. Returns a JSON-ready dict."""
    hops = _check_hops(hops)
    if isinstance(rpc_urls, str):
        raise TypeError("rpc_urls must be a sequence of URLs")
    urls = list(dict.fromkeys(rpc_urls))
    if not urls:
        raise ValueError("at least one rpc url is needed")
    for label, accounts in (("payee", [payee]), ("excluded", list(excluded)), ("exchange", list(exchange))):
        for a in accounts:
            if public_key_from_address(a) is None:
                raise ValueError(f"{label} is not a Nano account: {a!r}")
    payee_key = _key(payee)
    excl = {}
    for a in excluded:
        excl.setdefault(_key(a), a)
    hubs = {_key(a) for a in exchange}

    unique: dict[str, str] = {}
    results = []
    for p in payers:
        if public_key_from_address(p) is None:
            results.append({"payer": p, "verdict": "unreadable", "why": "not a Nano account", "deciding_hop": None,
                            "party": None, "witness": None, "trail": []})
            continue
        unique.setdefault(_key(p), p)

    read_at = _utc_now()
    for p in unique.values():
        answers, failed = {}, {}
        for url in urls:
            try:
                answers[url] = _read(p, url, payee_key, excl, hubs, hops, post)
            except NODE_ERRORS as e:
                failed[url] = f"{type(e).__name__}: {e}"[:160]
        if not answers:
            r = {"payer": p, "verdict": "unreadable", "why": "no node answered", "deciding_hop": None,
                 "party": None, "witness": None, "trail": []}
        elif len({_story(a) for a in answers.values()}) > 1:
            r = {"payer": p, "verdict": "unreadable", "why": "nodes_disagree", "deciding_hop": None,
                 "party": None, "witness": None, "trail": [],
                 "answers": {u: [a["verdict"], a["witness"]] for u, a in answers.items()}}
        else:
            r = dict(next(iter(answers.values())))
            openers = {json.dumps(a.get("opener"), sort_keys=True) for a in answers.values()}
            if len(openers) > 1:
                r["opener"] = _unreadable_opener("nodes gave different histories")
        r["nodes_read"] = list(answers)
        r["nodes_failed"] = failed
        # A node that failed means the verdict rests on fewer nodes than were asked for.
        r["fewer_nodes"] = bool(failed) and bool(answers)
        results.append(r)

    return {
        "v": "stated-payer-test-v1",
        "test": STATED_TEST,
        "reading": "funder = the account whose send opened the payer; later receives are not tested",
        "caveat": CAVEAT,
        "rpc": urls,
        "payee": payee,
        "excluded": list(excluded),
        "exchange": list(exchange),
        "hops": hops,
        "read_at": read_at,
        "counts": {v: sum(1 for r in results if r["verdict"] == v) for v in VERDICTS},
        "payers": results,
    }


def to_text(report: dict) -> str:
    lines = [f"stated test: {report['test']} (hops={report['hops']}, read {report['read_at']})"]
    for r in report["payers"]:
        w = r["witness"] or {}
        line = f"{r['payer']}  {r['verdict']}  - {r['why']}"
        if w.get("open_block"):
            line += f"\n    open {w['open_block']}  send {w.get('funding_send')}  funder {w.get('funder')}"
        if r["verdict"] == "independent":
            o = r.get("opener") or _unreadable_opener("not read")
            line += (f"\n    {r.get('caveat', CAVEAT)}: opener sent {o['amount_raw']} raw, then "
                     f"{o['later_receives']} later receive(s) totalling {o['later_receives_raw']} raw; "
                     f"opener share {o['opener_share']}")
        if r.get("fewer_nodes"):
            line += (f"\n    from {len(r['nodes_read'])} of {len(r['nodes_read']) + len(r['nodes_failed'])} nodes; "
                     "failed: " + "; ".join(f"{u} ({e})" for u, e in r["nodes_failed"].items()))
        lines.append(line)
    lines.append("counts: " + ", ".join(f"{k} {v}" for k, v in report["counts"].items()))
    return "\n".join(lines)


def main(argv=None, post=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--payee", required=True, help="the seller's account")
    p.add_argument("--payer", action="append", required=True)
    p.add_argument("--excluded", action="append", default=[], help="an account of either party")
    p.add_argument("--exchange", action="append", default=[], help="an exchange hot wallet, set aside")
    p.add_argument("--hops", type=int, default=1)
    p.add_argument("--rpc", action="append")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        report = stated_test(a.payee, a.payer, a.rpc or [DEFAULT_RPC], a.excluded, a.exchange, a.hops, post)
    except (ValueError, TypeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_BAD_INPUT
    print(json.dumps(report, indent=1) if a.json else to_text(report))
    return 2 if report["counts"]["unopened"] or report["counts"]["unreadable"] else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
