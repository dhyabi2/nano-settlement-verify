"""nano_stated_payer_test: one verdict per payer against a stated test, on a fake node (no network)."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nano_settlement_verify  # noqa: E402
import nano_stated_payer_test as st  # noqa: E402
from nano_five_fields import _address_of_key  # noqa: E402

URL = "http://127.0.0.1:7076"
NAMES = "payee us exchange stranger a b c d e f".split()
ACCT = {n: _address_of_key(bytes([i + 1]) * 32) for i, n in enumerate(NAMES)}
PAYEE, US, EXCH, STRANGER = ACCT["payee"], ACCT["us"], ACCT["exchange"], ACCT["stranger"]
A, B, C, D, E, F = (ACCT[n] for n in "a b c d e f".split())


def h(tag, account):
    return (tag + account[5:])[:64].upper().ljust(64, "0")


def ledger(funded_by, broken=frozenset()):
    """A fake node: `funded_by[account]` is the account whose send opened it.

    Absent accounts are unopened. Accounts in `broken` have an open block whose
    funding send the node cannot find.
    """
    by_hash = {}
    for acct, funder in funded_by.items():
        by_hash[h("0", acct)] = {"block_account": acct, "subtype": "open",
                                 "contents": {"type": "state", "account": acct, "link": h("5", acct)}}
        if acct not in broken:
            by_hash[h("5", acct)] = {"block_account": funder, "subtype": "send",
                                     "contents": {"type": "state", "account": funder, "link_as_account": acct}}

    def post(url, payload):
        if payload["action"] == "account_info":
            acct = payload["account"].replace("xrb_", "nano_", 1)
            if acct not in funded_by:
                return {"error": "Account not found"}
            return {"open_block": h("0", acct), "frontier": h("0", acct), "block_count": "1"}
        if payload["action"] == "block_info":
            return by_hash.get(payload["hash"], {"error": "Block not found"})
        raise AssertionError(payload["action"])

    return post


def run(payers, funded_by, hops=1, excluded=(US,), exchange=(EXCH,), urls=(URL,), post=None, **kw):
    return st.stated_test(PAYEE, payers, list(urls), excluded, exchange, hops,
                          post or ledger(funded_by, **kw))


class NoSockets(unittest.TestCase):
    def setUp(self):
        self._real = nano_settlement_verify.urllib.request.urlopen

        def refuse(*a, **k):
            raise AssertionError("a test tried to open a socket")

        nano_settlement_verify.urllib.request.urlopen = refuse

    def tearDown(self):
        nano_settlement_verify.urllib.request.urlopen = self._real


class Verdicts(NoSockets):
    def test_independent_names_the_stranger_and_its_open_block(self):
        r = run([A], {A: STRANGER})["payers"][0]
        self.assertEqual(r["verdict"], "independent")
        self.assertEqual(r["witness"], {"account": A, "open_block": h("0", A),
                                        "funding_send": h("5", A), "funder": STRANGER})

    def test_excluded_party_at_hop_one_is_named(self):
        r = run([A], {A: US})["payers"][0]
        self.assertEqual(r["verdict"], "not-independent")
        self.assertEqual(r["deciding_hop"], 1)
        self.assertEqual(r["party"], "excluded:" + US)
        self.assertEqual(r["witness"]["funder"], US)

    def test_payee_funding_its_own_payer_is_a_circle(self):
        r = run([A], {A: PAYEE})["payers"][0]
        self.assertEqual((r["verdict"], r["party"], r["deciding_hop"]), ("not-independent", "payee", 1))
        self.assertEqual(r["witness"]["open_block"], h("0", A))

    def test_exchange_only_funding_is_not_determinable_from_funding_alone(self):
        r = run([A], {A: EXCH})["payers"][0]
        self.assertEqual(r["verdict"], "not-determinable")
        self.assertEqual(r["why"], "not determinable from funding alone")
        self.assertEqual(r["witness"]["funder"], EXCH)

    def test_exchange_funding_without_the_carve_out_is_counted(self):
        # The carve-out is the caller's list, not a guess: unlisted, an exchange is a stranger.
        r = run([A], {A: EXCH}, exchange=())["payers"][0]
        self.assertEqual(r["verdict"], "independent")

    def test_unopened_is_never_independent(self):
        rep = run([A], {})
        self.assertEqual(rep["payers"][0]["verdict"], "unopened")
        self.assertEqual(rep["counts"]["independent"], 0)

    def test_unreadable_funding_send_is_never_independent(self):
        r = run([A], {A: STRANGER}, broken=frozenset({A}))["payers"][0]
        self.assertEqual(r["verdict"], "unreadable")
        self.assertEqual(r["witness"]["open_block"], h("0", A))

    def test_node_that_fails_reads_as_unreadable(self):
        def down(url, payload):
            raise OSError("connection refused")

        r = run([A], {}, post=down)["payers"][0]
        self.assertEqual(r["verdict"], "unreadable")
        self.assertIn(URL, r["nodes_failed"])

    def test_xrb_and_nano_spelling_of_one_key(self):
        xrb = "xrb_" + US[5:]
        r = run([A], {A: US}, excluded=(xrb,))["payers"][0]
        self.assertEqual(r["verdict"], "not-independent")
        rep = run([A, "xrb_" + A[5:]], {A: STRANGER})
        self.assertEqual(len(rep["payers"]), 1)  # one key, one verdict

    def test_payer_that_is_an_excluded_party_is_hop_zero(self):
        r = run([US], {US: STRANGER})["payers"][0]
        self.assertEqual((r["verdict"], r["deciding_hop"]), ("not-independent", 0))


class Hops(NoSockets):
    def test_default_is_one_hop_and_does_not_look_further(self):
        self.assertEqual(run([A], {A: B, B: US})["payers"][0]["verdict"], "independent")

    def test_two_hops_finds_the_party_behind_a_stranger(self):
        r = run([A], {A: B, B: US}, hops=2)["payers"][0]
        self.assertEqual((r["verdict"], r["deciding_hop"]), ("not-independent", 2))
        self.assertEqual(r["witness"]["account"], B)

    def test_an_exchange_behind_a_stranger_is_not_walked_past(self):
        r = run([A], {A: B, B: EXCH, EXCH: US}, hops=3)["payers"][0]
        self.assertEqual(r["verdict"], "independent")


class Nodes(NoSockets):
    def test_two_nodes_that_disagree_give_unreadable(self):
        one, two = ledger({A: STRANGER}), ledger({A: US})

        def post(url, payload):
            return (one if url == "a" else two)(url, payload)

        r = run([A], {}, urls=("a", "b"), post=post)["payers"][0]
        self.assertEqual((r["verdict"], r["why"]), ("unreadable", "nodes_disagree"))


class Cli(NoSockets):
    def test_json_and_text_outputs_and_exit_code(self):
        post = ledger({A: STRANGER, B: US, C: EXCH})
        args = ["--payee", PAYEE, "--excluded", US, "--exchange", EXCH, "--rpc", URL,
                "--payer", A, "--payer", B, "--payer", C]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = st.main(args + ["--json"], post=post)
        rep = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual([r["verdict"] for r in rep["payers"]],
                         ["independent", "not-independent", "not-determinable"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = st.main(args + ["--payer", D], post=post)
        self.assertEqual(code, 2)
        self.assertIn("unopened", buf.getvalue())
        self.assertIn("not determinable from funding alone", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
