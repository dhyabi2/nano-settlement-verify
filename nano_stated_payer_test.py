"""One verdict per payer against a stated independence test, re-runnable by anyone.

    python3 nano_stated_payer_test.py --payee nano_... --payer nano_... [--payer ...]
        [--excluded nano_...]... [--exchange nano_...]... [--hop largest-receive|opening-send]
        [--payment HASH]... [--hops 1] [--rpc URL]... [--json]

The test, as the seller stated it for its Dec 8 payer count and amended on
2026-11-10: a payer counts as independent iff "the largest receive into its
account before the payment was funded by neither Unstuck nor us - one hop back,
exchange hot wallets set aside". Read here as:

  * hop rule `largest-receive` (the default, the amended test): the funder is the
    source account of the largest receive into the payer before the payment. "Before
    the payment": only receives at heights below the payment (a send from the payer
    to the payee) count. The payment is a `--payment` hash found in the payer's
    history, else the payer's first send to the payee in its history; with none, all
    receives to date count and the verdict says so (`before`: "no payment yet - all
    receives to date"). Of equal largest receives, the earliest is taken. `--hops N`
    walks the rule N times: behind a funder, its largest receive below the send
    that funded the account before it.
  * hop rule `opening-send` (the test as first stated, kept so verdicts given under
    it stay re-derivable): the funder is the account whose send opened the payer
    (its open block's link or source); `--hops N` walks that link N times.
  * `not-independent`: a funder within the hops is the payee or an `--excluded`
    party (or the payer is one of them itself, hop 0). The output names which and
    at which hop. The payee funding its own payer is the seller's money in a circle.
  * `not-determinable`: the hop-1 funder is an exchange hot wallet (`--exchange`) -
    "not determinable from funding alone". Nothing behind an exchange is walked: it
    is the exchange's history, shared by every customer it paid.
  * `independent`: funded within the hops by none of the above and not only by an
    exchange.
  * `unopened`: the node has no open block for the account. `unreadable`: a block
    the verdict needs could not be read, the address is malformed, or two nodes
    told different stories. Neither is ever counted as independent.

Every verdict names its `hop_rule`. Its `witness` is the deciding hop's: the
account and its open block; under `largest-receive` also the chosen receive block,
its height and raw amount, its source, the send it received and what it was before
(`before`, `payment_block`); under `opening-send` the funding send and the funder.
The whole trail read is kept, so the verdict can be re-checked on any node with
`block_info` and `account_history`.

What each rule does not test. `largest-receive`: the other receives (each
`independent` carries `caveat` and `receives`: how many receives came before the
payment, their raw total and the chosen one's share). `opening-send`: receives after
the open block (`caveat` "opener only" and `opener`: the opening amount, the number
and raw total of later receives and the opener's share). Run `nano_funding.py` on
the payment block for who funded a payment.

A funder the node names must itself be a valid Nano account (prefix, length,
alphabet, checksum); anything else is `unreadable`, never a stranger.

`nano_` and `xrb_` spellings of one key are one payer: `payers` holds one entry per
distinct key (malformed payers first), and `counts` sum to that, not to the number
of `--payer` arguments.

Standard library only; reads `account_info`, `block_info` and `account_history`
(the whole history in one read, at most 4096 blocks per account; a longer history
under `largest-receive` is `unreadable`). With several `--rpc` every node that
answers must give the same verdict and witness, or the payer is `unreadable`; a node
that fails is listed in `nodes_failed`, and `fewer_nodes` is true when the verdict
came from fewer nodes than were asked.

Exit 0: every payer got a ledger verdict. 2: any payer is `unopened` or
`unreadable` (argparse also exits 2 on a usage error). 3: a `--payee`,
`--excluded` or `--exchange` that is not a Nano account, a bad `--hops` or `--hop`.
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys

import nano_independence
from nano_independence import _check_hops, _key, _utc_now
from nano_settlement_verify import public_key_from_address

STATED_TEST = ("Funded by neither us nor you, one hop back, exchange hot wallets set aside; "
               "one hop back = the largest receive into the payer before the payment (amended 2026-11-10)")
STATED_TEST_V1 = "Funded by neither us nor you, one hop back, exchange hot wallets set aside"
CARVE_OUT = "not determinable from funding alone"
VERDICTS = ("independent", "not-independent", "not-determinable", "unopened", "unreadable")
DEFAULT_RPC = "https://rpc.nano.to"
LARGEST_RECEIVE, OPENING_SEND = "largest-receive", "opening-send"
HOP_RULES = (LARGEST_RECEIVE, OPENING_SEND)
DEFAULT_HOP = LARGEST_RECEIVE
NO_PAYMENT = "no payment yet - all receives to date"
CAVEAT = ("opener only: the funder tested is the account whose send opened the payer; "
          "receives after the open block were not tested")
CAVEAT_LARGEST = ("largest receive only: the funder tested is the source of the largest receive before "
                  "the payment; the other receives were not tested")
READING = {
    LARGEST_RECEIVE: "funder = the source of the largest receive into the payer before the payment "
                     "(ties: the earliest); other receives are not tested",
    OPENING_SEND: "funder = the account whose send opened the payer; later receives are not tested",
}
HISTORY_CAP = 4096  # the most blocks of an account's history read in one account_history call
EXIT_BAD_INPUT = 3
# What a node read can raise; any of them makes that node's answer for that payer unreadable.
NODE_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException)
WITNESS_KEYS = {
    OPENING_SEND: ("account", "open_block", "funding_send", "funder"),
    LARGEST_RECEIVE: ("account", "open_block", "receive_block", "receive_height", "amount_raw", "source",
                      "funding_send", "funder", "before", "payment_block"),
}


def _witness(step: dict | None, rule: str = OPENING_SEND) -> dict | None:
    if not step:
        return None
    return {k: step.get(k) for k in WITNESS_KEYS[rule]}


def _raw(value) -> int | None:
    text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return int(text) if text.isdigit() else None


def _share(part: int, total: int) -> str:
    """part/total as a percentage with four decimals, rounded down, in integers."""
    if total <= 0:
        return "unreadable"
    q = part * 1000000 // total
    return f"{q // 10000}.{q % 10000:04d}%"


def _kind(row: dict):
    return row.get("subtype") or row.get("type")


def _history(account: str, count, rpc_url: str, post, exact: bool = False):
    """(rows, None) for the whole history of `account`, oldest first, or (None, why)."""
    if not isinstance(count, int) or count < 1:
        return None, "account_info gave no block count"
    if count > HISTORY_CAP:
        return None, f"{count} blocks, more than the {HISTORY_CAP} read"
    try:
        reply = post(rpc_url, {"action": "account_history", "account": account, "count": str(count),
                               "reverse": "true"})
    except NODE_ERRORS as e:
        return None, f"account_history: {type(e).__name__}: {e}"
    if not isinstance(reply, dict):
        return None, "account_history reply is not an object"
    history = reply.get("history")
    if "error" in reply or not isinstance(history, list):
        return None, f"account_history: {reply.get('error', 'no history list')}"
    if reply.get("next") or len(history) > count or (exact and len(history) != count):
        return None, "account_history did not give the whole history in one read"
    for row in history:
        if not isinstance(row, dict):
            return None, "account_history row is not an object"
    return history, None


def _largest_receive(account: str, rpc_url: str, post, payee_key, before_send=None, payments=frozenset()):
    """(funder, step, why) for `account` under the largest-receive rule.

    Hop 1 (before_send None): only receives below the payment count - a `payments`
    hash found in the history, else the first send to the payee, else all receives.
    Behind a funder (before_send given): only receives below that send. funder is
    _UNOPENED (step None) for an unknown account and None when it cannot be read."""
    post = post or nano_independence.nano_settlement_verify.post_json
    info = post(rpc_url, {"action": "account_info", "account": account})
    if "error" in info:
        return nano_independence._UNOPENED, None, None
    step = {"account": account, "hop_rule": LARGEST_RECEIVE,
            "checked_at_frontier": info.get("frontier") or None,
            "block_count": nano_independence._count(info.get("block_count")),
            "open_block": info.get("open_block") or None,
            "receive_block": None, "receive_height": None, "amount_raw": None, "source": None,
            "funding_send": None, "funder": None, "before": None, "payment_block": None}
    if not step["open_block"]:
        return None, step, f"account_info for {account} names no open block"
    rows, why = _history(account, step["block_count"], rpc_url, post, exact=True)
    if rows is None:
        return None, step, why
    heights = []
    for i, row in enumerate(rows):
        given = _raw(row.get("height"))
        if given is not None and given != i + 1:
            return None, step, "account_history heights are not in order from 1"
        heights.append(i + 1)
    if str(rows[0].get("hash") or "").upper() != str(step["open_block"]).upper():
        return None, step, "the first block in account_history is not the open block"

    limit = None  # rows at index >= limit are not before the payment
    if before_send is not None:
        for i, row in enumerate(rows):
            if str(row.get("hash") or "").upper() == str(before_send).upper():
                if _kind(row) != "send":
                    return None, step, f"{before_send} in the history of {account} is not a send"
                limit = i
                step["before"] = (f"below height {heights[i]}, the send {str(row.get('hash')).upper()} "
                                  "that funded the account before it")
                break
        if limit is None:
            return None, step, f"the funding send {before_send} is not in the history of {account}"
    else:
        wanted = {str(p).upper() for p in payments}
        given = [i for i, row in enumerate(rows) if str(row.get("hash") or "").upper() in wanted]
        for i in given:
            row = rows[i]
            if _kind(row) != "send" or _key(row.get("account")) != payee_key:
                return None, step, f"the --payment block {row.get('hash')} is not a send from the payer to the payee"
        to_payee = [i for i, row in enumerate(rows) if _kind(row) == "send"
                    and public_key_from_address(row.get("account")) is not None
                    and _key(row.get("account")) == payee_key]
        pick = min(given) if given else (min(to_payee) if to_payee else None)
        if pick is None:
            step["before"] = NO_PAYMENT
            limit = len(rows)
        else:
            limit = pick
            step["payment_block"] = str(rows[pick].get("hash")).upper()
            how = "the --payment given" if given else "the payer's first send to the payee"
            step["before"] = f"below height {heights[pick]}, the payment {step['payment_block']} ({how})"

    best, best_amount, total, n = None, -1, 0, 0
    for i in range(limit):
        row = rows[i]
        if _kind(row) not in ("receive", "open"):
            continue
        amount = _raw(row.get("amount"))
        if amount is None:
            return None, step, "a receive in account_history has no raw amount"
        n += 1
        total += amount
        if amount > best_amount:  # strictly larger: of equal amounts the earliest stays
            best, best_amount = i, amount
    if best is None:
        return None, step, "no receive before the payment in account_history"
    row = rows[best]
    step["receive_block"] = str(row.get("hash") or "").upper()
    step["receive_height"] = heights[best]
    step["amount_raw"] = str(best_amount)
    step["receives"] = {"before_payment": n, "before_payment_raw": str(total),
                        "chosen_share": _share(best_amount, total)}

    # The source the history names is checked against the blocks themselves.
    received = post(rpc_url, {"action": "block_info", "json_block": "true", "hash": step["receive_block"]})
    if "error" in received:
        return None, step, f"block_info {step['receive_block']}: {received.get('error')}"
    contents = nano_independence._contents(received)
    send_hash = contents.get("source") or contents.get("link")
    if not send_hash:
        return None, step, f"the receive {step['receive_block']} names no send"
    if _raw(received.get("amount")) not in (None, best_amount):
        return None, step, "account_history and block_info disagree on the receive's amount"
    send = post(rpc_url, {"action": "block_info", "json_block": "true", "hash": send_hash})
    if "error" in send:
        return None, step, f"block_info {send_hash}: {send.get('error')}"
    funder = send.get("block_account") or nano_independence._contents(send).get("account")
    if not funder:
        return None, step, f"the send {send_hash} names no account"
    funder = str(funder)
    step["funding_send"] = str(send_hash).upper()
    step["source"] = step["funder"] = funder
    named = row.get("account")
    if named and public_key_from_address(funder) is not None and _key(named) != _key(funder):
        return None, step, "account_history and block_info disagree on the receive's source"
    return funder, step, None


def _read(payer: str, rpc_url: str, payee_key: str, excluded: dict, exchanges: set, hops: int, post,
          rule: str = DEFAULT_HOP, payments=frozenset()) -> dict:
    """The verdict for one payer from one node."""
    trail: list[dict] = []

    def out(verdict, why, hop=None, step=None, party=None):
        return {"payer": payer, "verdict": verdict, "why": why, "hop_rule": rule, "deciding_hop": hop,
                "party": party, "witness": _witness(step, rule), "trail": trail}

    def role(account):
        k = _key(account)
        if k == payee_key:
            return "payee"
        if k in excluded:
            return f"excluded:{excluded[k]}"
        return None

    if role(payer):
        return out("not-independent", f"the payer is the {role(payer)} itself", 0, party=role(payer))
    current, seen, before = payer, {_key(payer)}, None
    for hop in range(1, hops + 1):
        if rule == LARGEST_RECEIVE:
            funder, step, why = _largest_receive(current, rpc_url, post, payee_key, before, payments)
        else:
            (funder, step), why = nano_independence._funder(current, rpc_url, post), None
        if step is not None:
            trail.append(step)
        if funder is nano_independence._UNOPENED:
            if hop == 1:
                return out("unopened", "the node has no open block for this account")
            return out("unreadable", f"the hop-{hop - 1} funder {current} reads as unopened", hop)
        if funder is None:
            return out("unreadable", (why or f"the funding send of {current} could not be read")[:200], hop, step)
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
        current, before = funder, step.get("funding_send")
    r = out("independent", "no funder within the hops is the payee, an excluded party, or only an exchange",
            1, trail[0])
    if rule == LARGEST_RECEIVE:
        r["caveat"] = CAVEAT_LARGEST
        r["receives"] = trail[0].get("receives")
    else:
        r["caveat"] = CAVEAT
        r["opener"] = _opener_summary(payer, trail[0], rpc_url, post)
    return r


def _unreadable_opener(why: str) -> dict:
    return {"amount_raw": "unreadable", "later_receives": "unreadable", "later_receives_raw": "unreadable",
            "opener_share": "unreadable", "unreadable_why": why[:200]}


def _opener_summary(payer: str, step: dict, rpc_url: str, post) -> dict:
    """What one account_history read gives cheaply about an `independent` payer under
    the opening-send rule: the opening send's amount, how many receives came after it
    and their total, and the opener's share of everything received. Any failure gives
    `unreadable` fields."""
    post = post or nano_independence.nano_settlement_verify.post_json
    open_block = str(step.get("open_block") or "").upper()
    history, why = _history(payer, step.get("block_count"), rpc_url, post)
    if history is None:
        return _unreadable_opener(why)
    opener, later, later_raw = None, 0, 0
    for row in history:
        if _kind(row) not in ("receive", "open"):
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
    return json.dumps([r["verdict"], r["deciding_hop"], r["party"], r["witness"]], sort_keys=True)


def _blank(p: str, why: str, rule: str) -> dict:
    return {"payer": p, "verdict": "unreadable", "why": why, "hop_rule": rule, "deciding_hop": None,
            "party": None, "witness": None, "trail": []}


def stated_test(payee: str, payers, rpc_urls, excluded=(), exchange=(), hops: int = 1, post=None,
                hop_rule: str = DEFAULT_HOP, payments=()) -> dict:
    """Apply the stated test to each payer. Returns a JSON-ready dict."""
    hops = _check_hops(hops)
    if hop_rule not in HOP_RULES:
        raise ValueError(f"hop rule must be one of {', '.join(HOP_RULES)}, got {hop_rule!r}")
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
    pays = frozenset(str(p).upper() for p in payments)

    unique: dict[str, str] = {}
    results = []
    for p in payers:
        if public_key_from_address(p) is None:
            results.append(_blank(p, "not a Nano account", hop_rule))
            continue
        unique.setdefault(_key(p), p)

    read_at = _utc_now()
    for p in unique.values():
        answers, failed = {}, {}
        for url in urls:
            try:
                answers[url] = _read(p, url, payee_key, excl, hubs, hops, post, hop_rule, pays)
            except NODE_ERRORS as e:
                failed[url] = f"{type(e).__name__}: {e}"[:160]
        if not answers:
            r = _blank(p, "no node answered", hop_rule)
        elif len({_story(a) for a in answers.values()}) > 1:
            r = _blank(p, "nodes_disagree", hop_rule)
            r["answers"] = {u: [a["verdict"], a["witness"]] for u, a in answers.items()}
        else:
            r = dict(next(iter(answers.values())))
            for field, unreadable in (("opener", _unreadable_opener), ("receives", None)):
                if field in r and len({json.dumps(a.get(field), sort_keys=True) for a in answers.values()}) > 1:
                    r[field] = unreadable("nodes gave different histories") if unreadable else "unreadable"
        r["nodes_read"] = list(answers)
        r["nodes_failed"] = failed
        # A node that failed means the verdict rests on fewer nodes than were asked for.
        r["fewer_nodes"] = bool(failed) and bool(answers)
        results.append(r)

    return {
        "v": "stated-payer-test-v2",
        "test": STATED_TEST if hop_rule == LARGEST_RECEIVE else STATED_TEST_V1,
        "hop_rule": hop_rule,
        "reading": READING[hop_rule],
        "caveat": CAVEAT_LARGEST if hop_rule == LARGEST_RECEIVE else CAVEAT,
        "rpc": urls,
        "payee": payee,
        "excluded": list(excluded),
        "exchange": list(exchange),
        "payments": sorted(pays),
        "hops": hops,
        "read_at": read_at,
        "counts": {v: sum(1 for r in results if r["verdict"] == v) for v in VERDICTS},
        "payers": results,
    }


def to_text(report: dict) -> str:
    rule = report.get("hop_rule", OPENING_SEND)
    lines = [f"stated test: {report['test']} (hop rule {rule}, hops={report['hops']}, read {report['read_at']})"]
    for r in report["payers"]:
        w = r["witness"] or {}
        line = f"{r['payer']}  {r['verdict']}  - {r['why']}"
        if rule == LARGEST_RECEIVE and w.get("open_block"):
            line += f"\n    open {w['open_block']}  before: {w.get('before')}"
            if w.get("receive_block"):
                line += (f"\n    largest receive {w['receive_block']} (height {w.get('receive_height')}) "
                         f"{w.get('amount_raw')} raw from {w.get('source')}  send {w.get('funding_send')}")
        elif w.get("open_block"):
            line += f"\n    open {w['open_block']}  send {w.get('funding_send')}  funder {w.get('funder')}"
        if r["verdict"] == "independent" and rule == LARGEST_RECEIVE:
            s = r.get("receives") or {}
            if isinstance(s, dict) and s:
                line += (f"\n    {r.get('caveat', CAVEAT_LARGEST)}: {s.get('before_payment')} receive(s) before the "
                         f"payment totalling {s.get('before_payment_raw')} raw; chosen share {s.get('chosen_share')}")
        elif r["verdict"] == "independent":
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
    p.add_argument("--hop", default=DEFAULT_HOP,
                   help=f"what one hop back reads: {LARGEST_RECEIVE} (default, amended 2026-11-10) "
                        f"or {OPENING_SEND} (as first stated)")
    p.add_argument("--payment", action="append", default=[],
                   help="a payment block (payer -> payee); receives below it are the ones tested")
    p.add_argument("--hops", type=int, default=1)
    p.add_argument("--rpc", action="append")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        report = stated_test(a.payee, a.payer, a.rpc or [DEFAULT_RPC], a.excluded, a.exchange, a.hops, post,
                             a.hop, a.payment)
    except (ValueError, TypeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_BAD_INPUT
    print(json.dumps(report, indent=1) if a.json else to_text(report))
    return 2 if report["counts"]["unopened"] or report["counts"]["unreadable"] else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
