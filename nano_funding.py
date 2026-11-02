"""Funding provenance: was the payer's money already somebody's we should name? Read from the ledger only.

    python3 nano_funding.py --block HASH [--rpc URL]... [--sponsor ACCT]... [--self ACCT]...
                            [--window-hours 720] [--json]

Verdicts: `unsubsidised`, `sponsor_funded`, `dispenser_funded`, `self_funded`, `unknown`, each with the
exact prior receive that decides it. Reads only (block_info, account_history); no key, no wallet.

Rules that are the point of the tool:
  * The label is about WHETHER a flagged or dispenser funder sent anything in the window, not how much.
    Each funder row therefore carries received_raw and its share (parts per million) of the window's
    receives, so a reader can see a 0.00001 XNO starter beside 6 XNO from an unflagged source.
  * Payment time is the payer's SEND block's local_timestamp. A payee's receive block is stamped when the
    payee pocketed, which can be days later; reading it makes a batching payee look like a scripted payer.
    Give a receive block and the tool resolves it to the send it pockets.
  * A dispenser (one funder sending the same amount to many distinct accounts) is found from the ledger and
    NO flag can suppress it. `--sponsor` and `--self` only choose between the labels below dispenser; the
    output names the flags used so a reader who disagrees can drop one and re-run.
  * With two or more nodes every node must tell one story, or the verdict is `unknown` (nodes_disagree).
    Never a majority, never the first node. (local_timestamp is each node's own clock and may differ
    by a second between nodes; it is not part of the story compared.)
  * Fewer readable receives than the window needs is `unknown` (history_incomplete), not `unsubsidised`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import nano_settlement_verify
from nano_five_fields import _same_account, public_key_from_address

DISPENSER_MIN_RECIPIENTS = 10
HISTORY_PAGE = 100
MAX_PAGES = 5
_HEX64 = re.compile(r"[0-9A-Fa-f]{64}")
_ZERO = "0" * 64
LABELS = ("unsubsidised", "sponsor_funded", "dispenser_funded", "self_funded", "unknown")
EXIT = {"unsubsidised": 0, "sponsor_funded": 3, "dispenser_funded": 3, "self_funded": 3, "unknown": 2}


class _Unknown(Exception):
    def __init__(self, reason, **ev):
        super().__init__(reason)
        self.reason, self.ev = reason, ev


def _call(fetch, url, payload):
    r = fetch(url, payload)
    if not isinstance(r, dict) or "error" in r:
        raise _Unknown("node_error", node=url, detail=str(r)[:120])
    return r


def _int(x):
    return int(x) if isinstance(x, str) and x.isdigit() else None


def _history(fetch, url, account, head, until_ts):
    """Newest-first entries of `account` back to `until_ts`; (entries, complete)."""
    out, complete = [], False
    for _ in range(MAX_PAGES):
        p = {"action": "account_history", "account": account, "count": str(HISTORY_PAGE)}
        if head:
            p["head"] = head
        r = _call(fetch, url, p)
        page = r.get("history") or []
        if not isinstance(page, list):
            raise _Unknown("node_error", node=url, detail="history is not a list")
        for e in page:
            if until_ts is not None and (_int(e.get("local_timestamp")) or 0) < until_ts:
                return out, True
            out.append(e)
        nxt = r.get("previous")
        if not nxt or not page:
            return out, True            # reached the account's open block
        head = nxt
    return out, complete


def _dispenser(fetch, url, funder):
    """(is_dispenser, evidence): same amount sent to >= DISPENSER_MIN distinct accounts, last page only."""
    entries, _ = _history(fetch, url, funder, None, None)
    by_amount = {}
    for e in entries:
        if e.get("type") == "send" and _int(e.get("amount")):
            by_amount.setdefault(e["amount"], set()).add(e.get("account"))
    best = max(by_amount.items(), key=lambda kv: len(kv[1]), default=(None, set()))
    return len(best[1]) >= DISPENSER_MIN_RECIPIENTS, {
        "amount_raw": best[0], "distinct_recipients": len(best[1]), "sends_read": len(entries)}


def _one(fetch, url, block_hash, sponsors, selfs, window_hours):
    info = _call(fetch, url, {"action": "block_info", "json_block": "true", "hash": block_hash})
    c = info.get("contents") if isinstance(info.get("contents"), dict) else {}
    sub = info.get("subtype")
    send_hash, send = block_hash.upper(), info
    if sub == "receive":
        send_hash = (c.get("link") or "").upper()
        if not _HEX64.fullmatch(send_hash):
            raise _Unknown("receive_has_no_source_link")
        send = _call(fetch, url, {"action": "block_info", "json_block": "true", "hash": send_hash})
        c = send.get("contents") if isinstance(send.get("contents"), dict) else {}
        sub = send.get("subtype")
    if sub != "send":
        raise _Unknown("not_a_payment_block", subtype=sub)
    if send.get("confirmed") != "true":
        raise _Unknown("payment_not_confirmed")
    payer, payee = send.get("block_account"), c.get("link_as_account")
    t = _int(send.get("local_timestamp"))
    if not t or not payer or not payee:
        raise _Unknown("payment_time_or_parties_unreadable")
    start = t - int(window_hours * 3600)
    entries, complete = _history(fetch, url, payer, c.get("previous"), start)
    receives = [e for e in entries if e.get("type") == "receive" and e.get("confirmed") != "false"
                and start <= (_int(e.get("local_timestamp")) or 0) <= t and _int(e.get("amount"))]
    funders = {}
    for e in receives:
        funders.setdefault(e["account"], []).append(e)
    rows = []
    total_in = sum(int(e["amount"]) for e in receives)
    for f, rs in funders.items():
        is_disp, ev = _dispenser(fetch, url, f)
        if is_disp:
            cls = "dispenser_funded"
        elif _same_account(f, payee) or any(_same_account(f, s) for s in selfs):
            cls = "self_funded"
        elif any(_same_account(f, s) for s in sponsors):
            cls = "sponsor_funded"
        else:
            cls = "unsubsidised"
        rows.append({"funder": f, "classification": cls, "dispenser_evidence": ev,
                     "receive_hashes": [r["hash"] for r in rs],
                     "received_raw": str(sum(int(r["amount"]) for r in rs)),
                     "share_of_window_receives_ppm": (sum(int(r["amount"]) for r in rs) * 1_000_000 // total_in
                                                      if total_in else 0),
                     "label_from_flag": cls in ("self_funded", "sponsor_funded")})
    order = ("dispenser_funded", "self_funded", "sponsor_funded")
    verdict, decisive = None, None
    for cls in order:
        hit = [r for r in rows if r["classification"] == cls]
        if hit:
            verdict = cls
            rec = max((x for r in hit for x in funders[r["funder"]]), key=lambda x: _int(x["local_timestamp"]))
            decisive = {"funder": rec["account"], "receive_hash": rec["hash"], "amount_raw": rec["amount"],
                        "received_at": _int(rec["local_timestamp"])}
            break
    why = None
    if verdict is None:
        if not receives:
            verdict, why = "unknown", "no_receive_in_window" if complete else "history_incomplete"
        elif not complete:
            verdict, why = "unknown", "history_incomplete"
        else:
            verdict = "unsubsidised"
    return {"verdict": verdict, "why": why, "payment_send_hash": send_hash, "payer": payer,
            "payee": payee, "payment_time": t, "payment_time_source": "payer's send block local_timestamp",
            "receives_in_window": len(receives), "history_complete": complete,
            "decisive_receive": decisive, "funders": rows}


def provenance(block_hash, rpc_urls, sponsors=(), selfs=(), window_hours=720, fetch=None) -> dict:
    base = {"v": "funding-provenance-v1", "block_hash": str(block_hash).upper(),
            "window_hours": window_hours,
            "flags": {"sponsor": list(sponsors), "self": list(selfs)},
            "note": "A payer's funding is evidence about subsidy, not about who the payer is."}
    if isinstance(rpc_urls, str):
        raise TypeError("rpc_urls must be a sequence of URLs")
    if not isinstance(block_hash, str) or not _HEX64.fullmatch(block_hash):
        return dict(base, verdict="unknown", why="block_hash_not_64_hex", nodes_read=[])
    for a in list(sponsors) + list(selfs):
        if public_key_from_address(a) is None:
            return dict(base, verdict="unknown", why=f"flag_not_an_account:{a}", nodes_read=[])
    fetch = fetch or nano_settlement_verify.post_json
    results, failed = {}, {}
    for url in dict.fromkeys(rpc_urls):
        try:
            results[url] = _one(fetch, url, block_hash, list(sponsors), list(selfs), window_hours)
        except _Unknown as e:
            failed[url] = e.reason
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            failed[url] = f"{type(e).__name__}: {e}"
    base["nodes_read"] = list(results)
    base["nodes_failed"] = failed
    if not results:
        return dict(base, verdict="unknown", why=next(iter(failed.values()), "no_nodes_given"))
    stories = {json.dumps([r["verdict"], r["payer"], r["payee"], (r["decisive_receive"] or {}).get("receive_hash"),
                           sorted(f["funder"] + f["classification"] for f in r["funders"])])
               for r in results.values()}
    if len(stories) > 1:
        return dict(base, verdict="unknown", why="nodes_disagree",
                    answers={u: [r["verdict"], r["decisive_receive"]] for u, r in results.items()})
    r = next(iter(results.values()))
    return dict(base, **r)


def main(argv=None, fetch=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--block", required=True)
    p.add_argument("--rpc", action="append")
    p.add_argument("--sponsor", action="append", default=[])
    p.add_argument("--self", dest="selfs", action="append", default=[])
    p.add_argument("--window-hours", type=int, default=720)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    r = provenance(a.block, a.rpc or ["https://rpc.nano.to"], a.sponsor, a.selfs, a.window_hours, fetch)
    print(json.dumps(r, indent=None if a.json else 1))
    return EXIT[r["verdict"]]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
