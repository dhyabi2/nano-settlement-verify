"""External counterparties per period for one Nano address. Keyless, read-only, standard library only.

    python3 nano_period_counts.py --address nano_... [--period day|week|month] [--node URL]...
        [--operator nano_...]... [--no-funded-set] [--json]

Answers "how many counterparties that are not us paid this address in each period?" with a query a
stranger can repeat, not an assertion. Every confirmed RECEIVE on the address is put in one class:

    self      the sender is the address itself (never counted)
    operator  the sender is in the set of accounts this address has ever SENT to (its funded set, read from
              its own chain, unless --no-funded-set) or is named with --operator
    external  anyone else

The funded-set rule is the one moltbookrevenueagent set out: "'operator' means 'in the set of addresses I have
ever funded', which is a query, not an assertion." Money that came from the payee's own funding is the payee's
money moving in a circle, so it is not demand. It is a rule about KEYS, not about people: two external keys can
still be one person, and a key funded by the operator through a third account is not caught here (see
nano_funding.py for the provenance walk).

Periods are UTC calendar day, ISO week (Monday) or month of `local_timestamp`, which is when the node saw the
payee pocket the block. A node reports 0 when it does not know; such a payment is counted in `undated`, never in
a period. A truncated history REFUSES (exit 3); it never prints a smaller number. Exit: 0 counted, 2 bad
checksum, 3 unreadable or incomplete, 64 usage.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time

import nano_payers
import nano_settlement_verify as nsv

PERIODS = ("day", "week", "month")


class Refusal(Exception):
    def __init__(self, code, exit_code, detail=""):
        super().__init__(f"{code}: {detail}")
        self.code, self.exit_code, self.detail = code, exit_code, detail


def bucket(ts: int, period: str) -> str:
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date()
    if period == "day":
        return d.isoformat()
    if period == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    return f"{d.year}-{d.month:02d}"


def walk(account: str, rpc_url: str, page_fn=None):
    """Every history row of `account` back to its open block: (rows, complete)."""
    page_fn = page_fn or nano_payers._history_page
    rows, seen, head, oldest = [], set(), None, None
    for _ in range(nano_payers.HISTORY_PAGES_MAX):
        page = page_fn(account, rpc_url, head)
        fresh = 0
        for b in page:
            if not isinstance(b, dict):
                raise nsv.NotANodeReply("a history row is not a block")
            h = str(b.get("hash") or "")
            if h and h in seen:
                continue
            seen.add(h)
            fresh += 1
            height = nano_payers._int_field(b, "height", account)
            oldest = height if oldest is None or height < oldest else oldest
            rows.append(b)
        if oldest == 1 or fresh == 0 or len(page) < nano_payers.HISTORY_PAGE:
            break
        head = str(page[-1].get("hash") or "")
        if not head:
            break
    return rows, (oldest is None or oldest == 1)


def count(address, nodes=None, period="month", operators=(), use_funded_set=True, now=None, page_fn=None):
    if period not in PERIODS:
        raise Refusal("usage", 64, "period must be one of " + ", ".join(PERIODS))
    if not nsv.is_valid_account(address):
        raise Refusal("address_checksum_invalid", 2, address)
    for o in operators:
        if not nsv.is_valid_account(o):
            raise Refusal("address_checksum_invalid", 2, f"--operator {o}")
    nodes = list(nodes or ["https://rpc.nano.to"])
    last = None
    rows = None
    for url in nodes:
        try:
            rows, complete = walk(address, url, page_fn)
        except nsv.NotANodeReply as e:
            last = str(e)
            continue
        except Exception as e:  # network errors are "could not look", never "nobody paid"
            last = f"{type(e).__name__}: {e}"
            continue
        if not complete:
            raise Refusal("range_incomplete", 3, f"{url} did not reach the open block")
        used = url
        break
    else:
        raise Refusal("node_unreachable", 3, last or "no node answered")

    me = nano_payers.public_key_from_address(address)
    key = nano_payers.public_key_from_address
    operator_keys = {key(o) for o in operators}
    funded = set()
    if use_funded_set:
        for b in rows:
            if b.get("type") == "send" and nano_payers._confirmed(b) and b.get("account"):
                k = key(b["account"])
                if k:
                    funded.add(k)
    funded |= operator_keys

    periods: dict[str, dict] = {}
    seen_external: set = set()
    undated = {"external_payments": 0, "operator_payments": 0}
    totals = {"external_distinct": set(), "operator_distinct": set(), "external_payments": 0,
              "operator_payments": 0, "self_payments": 0, "unconfirmed": 0}
    receives = sorted((b for b in rows if b.get("type") == "receive"),
                      key=lambda b: (nano_payers._int_field(b, "local_timestamp", address) or 1 << 62))
    for b in receives:
        if not nano_payers._confirmed(b):
            totals["unconfirmed"] += 1
            continue
        sender = b.get("account")
        k = key(sender)
        if k is None:
            raise nsv.NotANodeReply(f"a receive names source {sender!r}, which fails its checksum")
        if k == me:
            totals["self_payments"] += 1
            continue
        ts = nano_payers._int_field(b, "local_timestamp", address)
        amount = nano_payers._int_field(b, "amount", address)
        cls = "operator" if k in funded else "external"
        totals[f"{cls}_payments"] += 1
        totals[f"{cls}_distinct"].add(k)
        if not ts:
            undated[f"{cls}_payments"] += 1
            continue
        slot = periods.setdefault(bucket(ts, period), {
            "external_payers": set(), "external_new_payers": set(), "external_payments": 0, "external_raw": 0,
            "operator_payers": set(), "operator_payments": 0})
        if cls == "external":
            slot["external_payers"].add(k)
            slot["external_payments"] += 1
            slot["external_raw"] += amount
            if k not in seen_external:
                slot["external_new_payers"].add(k)
                seen_external.add(k)
        else:
            slot["operator_payers"].add(k)
            slot["operator_payments"] += 1

    out_periods = []
    for name in sorted(periods):
        s = periods[name]
        out_periods.append({"period": name, "external_distinct_payers": len(s["external_payers"]),
                            "external_new_payers": len(s["external_new_payers"]),
                            "external_payments": s["external_payments"],
                            "external_received": nsv_fmt(s["external_raw"]),
                            "operator_distinct_payers": len(s["operator_payers"]),
                            "operator_payments": s["operator_payments"]})
    as_of = datetime.datetime.fromtimestamp(now if now is not None else time.time(), datetime.timezone.utc)
    return {
        "v": 1, "address": address, "network": "nano:mainnet", "period": period, "complete": True,
        "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": {"node": used, "key_in_path": False},
        "rule": "external = a confirmed receive whose sender is neither the address itself nor in its funded "
                "set (accounts it has ever sent to) nor in --operator; keys, not people; no clustering",
        "funded_set_used": use_funded_set, "funded_set_size": len(funded), "periods": out_periods,
        "undated": undated,
        "totals": {"external_distinct_payers": len(totals["external_distinct"]),
                   "external_payments": totals["external_payments"],
                   "operator_distinct_payers": len(totals["operator_distinct"]),
                   "operator_payments": totals["operator_payments"],
                   "self_payments": totals["self_payments"], "unconfirmed_not_counted": totals["unconfirmed"]},
    }


def nsv_fmt(raw: int) -> str:
    s = str(raw).rjust(31, "0")
    whole, frac = s[:-30], s[-30:].rstrip("0")
    return whole + ("." + frac if frac else "")


def main(argv=None, page_fn=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--address", required=True)
    p.add_argument("--period", default="month", choices=PERIODS)
    p.add_argument("--node", action="append")
    p.add_argument("--operator", action="append", default=[])
    p.add_argument("--no-funded-set", action="store_true")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        r = count(a.address, a.node, a.period, a.operator, not a.no_funded_set, page_fn=page_fn)
    except Refusal as e:
        print(json.dumps({"refused": e.code, "detail": e.detail, "complete": False}))
        return e.exit_code
    print(json.dumps(r, indent=None if a.json else 1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
