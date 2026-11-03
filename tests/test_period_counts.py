"""nano_period_counts: external counterparties per period, with a stubbed ledger (no network)."""
import calendar
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nano_period_counts as pc  # noqa: E402

ME = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"
A = "nano_3saqoz5qfgmohfz3dg5ywwmxj7dwdp3g6xfbspt11g7gyrxgbupi1w9u4g4r"
B = "nano_3tcr79htpg9o1ibwmupcaqnbctnygpbzajm4ecq66pcbfca99kht7pjo9pg3"
OPERATOR = "nano_3kaf7r9n3ixpjao14z1h18i7mjdkzb55k74qka67dngpiswaadyiu3otq7pp"
FUNDED = "nano_1xug1q5t7nxoj3ywwzokiea9jz8fq8qfgzp8pbyfr3co3e5xgj755uofu8ue"


def ts(y, m, d):
    return str(calendar.timegm((y, m, d, 12, 0, 0)))


def row(n, typ, acct, amount, when, confirmed="true"):
    return {"hash": "%064X" % n, "type": typ, "account": acct, "amount": str(amount),
            "local_timestamp": when, "height": str(n), "confirmed": confirmed}


def ledger(rows):
    def page(account, url, head):
        return list(reversed(rows)) if head is None else []
    return page


XNO = 10 ** 30
BASE = [
    row(1, "receive", A, XNO, ts(2026, 9, 1)),
    row(2, "receive", A, XNO, ts(2026, 9, 20)),
    row(3, "receive", B, XNO, ts(2026, 10, 3)),
    row(4, "send", FUNDED, 5 * XNO, ts(2026, 10, 4)),
    row(5, "receive", FUNDED, XNO, ts(2026, 10, 5)),      # funded by us: operator, not demand
    row(6, "receive", OPERATOR, XNO, ts(2026, 10, 6)),
    row(7, "receive", ME, XNO, ts(2026, 10, 7)),          # self
    row(8, "receive", B, XNO, ts(2026, 10, 8), confirmed="false"),
    row(9, "receive", B, XNO, "0"),                       # node does not know when
]


class PeriodCounts(unittest.TestCase):
    def run_count(self, rows=BASE, **kw):
        return pc.count(ME, ["https://stub"], page_fn=ledger(rows), now=0, **kw)

    def test_months(self):
        r = self.run_count()
        self.assertEqual([p["period"] for p in r["periods"]], ["2026-09", "2026-10"])
        sep, octo = r["periods"]
        self.assertEqual((sep["external_distinct_payers"], sep["external_new_payers"], sep["external_payments"]), (1, 1, 2))
        self.assertEqual(sep["external_received"], "2")
        self.assertEqual((octo["external_distinct_payers"], octo["external_new_payers"]), (2, 2))  # B and OPERATOR (not yet declared)
        self.assertEqual(octo["operator_distinct_payers"], 1)   # FUNDED, found by the funded-set query

    def test_funded_set_is_a_query_over_the_payees_own_sends(self):
        r = self.run_count()
        self.assertEqual(r["funded_set_size"], 1)
        r2 = self.run_count(use_funded_set=False)
        self.assertEqual(r2["totals"]["external_distinct_payers"], 4)  # A, B, FUNDED, OPERATOR

    def test_declared_operator(self):
        r = self.run_count(operators=[OPERATOR])
        self.assertEqual(r["totals"]["external_distinct_payers"], 2)   # A and B
        self.assertEqual(r["totals"]["operator_distinct_payers"], 2)   # FUNDED and OPERATOR

    def test_self_unconfirmed_and_undated_are_never_in_a_period(self):
        r = self.run_count()
        self.assertEqual(r["totals"]["self_payments"], 1)
        self.assertEqual(r["totals"]["unconfirmed_not_counted"], 1)
        self.assertEqual(r["undated"]["external_payments"], 1)
        self.assertEqual(sum(p["external_payments"] for p in r["periods"]), 4)

    def test_week_and_day_buckets(self):
        self.assertEqual(pc.bucket(int(ts(2026, 9, 1)), "day"), "2026-09-01")
        self.assertEqual(pc.bucket(int(ts(2026, 9, 1)), "week"), "2026-W36")
        self.assertEqual(pc.bucket(int(ts(2026, 9, 1)), "month"), "2026-09")

    def test_truncated_history_refuses_instead_of_undercounting(self):
        cut = [dict(r, height=str(int(r["height"]) + 5)) for r in BASE]
        with self.assertRaises(pc.Refusal) as cm:
            self.run_count(rows=cut)
        self.assertEqual((cm.exception.code, cm.exception.exit_code), ("range_incomplete", 3))

    def test_unreachable_node_is_not_zero(self):
        def boom(*a, **k):
            raise OSError("down")
        with self.assertRaises(pc.Refusal) as cm:
            pc.count(ME, ["https://stub"], page_fn=boom)
        self.assertEqual(cm.exception.code, "node_unreachable")

    def test_bad_checksum_and_period(self):
        with self.assertRaises(pc.Refusal) as cm:
            pc.count(ME[:-1] + "3", ["https://stub"], page_fn=ledger(BASE))
        self.assertEqual(cm.exception.exit_code, 2)
        with self.assertRaises(pc.Refusal):
            pc.count(ME, ["https://stub"], period="year", page_fn=ledger(BASE))

    def test_unopened_account_is_zero_not_error(self):
        r = pc.count(ME, ["https://stub"], page_fn=ledger([]))
        self.assertEqual(r["totals"]["external_distinct_payers"], 0)
        self.assertEqual(r["periods"], [])

    def test_cli_exit_codes_and_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = pc.main(["--address", ME, "--period", "month", "--json"], page_fn=ledger(BASE))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())["totals"]["external_distinct_payers"], 3)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = pc.main(["--address", ME[:-1] + "3"], page_fn=ledger(BASE))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
