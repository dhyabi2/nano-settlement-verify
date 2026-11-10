"""nano_stated_payer_test: one verdict per payer against a stated test, on a fake node (no network)."""
import hashlib
import http.client
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

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

    def rows(acct):
        # The open receive, then one send to each account this one opened.
        return [{"type": "receive", "hash": h("0", acct), "account": funded_by[acct], "amount": "1000"}] + [
            {"type": "send", "hash": h("5", x), "account": x, "amount": "1"}
            for x, f in funded_by.items() if f == acct and x not in broken]

    def post(url, payload):
        if payload["action"] == "account_info":
            acct = payload["account"].replace("xrb_", "nano_", 1)
            if acct not in funded_by:
                return {"error": "Account not found"}
            return {"open_block": h("0", acct), "frontier": h("0", acct), "block_count": str(len(rows(acct)))}
        if payload["action"] == "block_info":
            return by_hash.get(payload["hash"], {"error": "Block not found"})
        if payload["action"] == "account_history":
            acct = payload["account"].replace("xrb_", "nano_", 1)
            return {"account": acct, "history": rows(acct)}
        raise AssertionError(payload["action"])

    return post


def run(payers, funded_by, hops=1, excluded=(US,), exchange=(EXCH,), urls=(URL,), post=None, hop=None,
        payments=(), **kw):
    return st.stated_test(PAYEE, payers, list(urls), excluded, exchange, hops,
                          post or ledger(funded_by, **kw), hop or st.DEFAULT_HOP, payments)


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
        r = run([A], {A: STRANGER}, hop="opening-send")["payers"][0]
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


def with_funder_value(funded_by, account, value):
    """A fake node whose funding send for `account` names `value` as its account."""
    base = ledger(funded_by)

    def post(url, payload):
        reply = base(url, payload)
        if payload["action"] == "block_info" and payload["hash"] == h("5", account):
            reply = dict(reply, block_account=value)
            reply["contents"] = dict(reply["contents"], account=value)
        return reply

    return post


class MalformedFunder(NoSockets):
    def test_funder_that_is_not_a_nano_account_is_unreadable(self):
        bad_checksum = PAYEE[:-1] + ("1" if PAYEE[-1] != "1" else "3")
        for value in ("garbage", 12345, ["x"], bad_checksum, "xrb_" + STRANGER[5:-1]):
            rep = run([A], {}, post=with_funder_value({A: STRANGER}, A, value))
            r = rep["payers"][0]
            self.assertEqual(r["verdict"], "unreadable", value)
            self.assertIn("not a Nano account", r["why"])
            self.assertEqual(rep["counts"]["independent"], 0)

    def test_malformed_funder_at_hop_two_is_unreadable(self):
        r = run([A], {}, hops=2, post=with_funder_value({A: B, B: STRANGER}, B, "nano_garbage"))["payers"][0]
        self.assertEqual((r["verdict"], r["deciding_hop"]), ("unreadable", 2))

    def test_a_valid_xrb_funder_is_still_a_stranger(self):
        r = run([A], {}, post=with_funder_value({A: STRANGER}, A, "xrb_" + STRANGER[5:]))["payers"][0]
        self.assertEqual(r["verdict"], "independent")


def with_history(funded_by, rows=None, fail=None):
    base = ledger(funded_by)

    def post(url, payload):
        if payload["action"] == "account_history":
            if fail is not None:
                raise fail
            return {"account": payload["account"], "history": rows}
        reply = base(url, payload)
        if payload["action"] == "account_info" and rows:
            reply = dict(reply, block_count=str(len(rows)))
        return reply

    return post


class OpenerOnlyCaveat(NoSockets):
    def test_independent_carries_the_opener_only_caveat_and_share(self):
        rows = [{"type": "receive", "hash": h("0", A), "account": STRANGER, "amount": "123"},
                {"type": "send", "hash": "S" * 64, "account": B, "amount": "50"},
                {"type": "receive", "hash": "R" * 64, "account": C, "amount": "391000"},
                {"type": "receive", "hash": "Q" * 64, "account": D, "amount": "8877"}]
        post = with_history({A: STRANGER}, rows)
        r = run([A], {}, post=post, hop="opening-send")["payers"][0]
        self.assertEqual(r["verdict"], "independent")
        self.assertIn("opener only", r["caveat"])
        self.assertEqual(r["opener"], {"amount_raw": "123", "later_receives": 2, "later_receives_raw": "399877",
                                       "opener_share": "0.0307%"})
        buf = io.StringIO()
        with redirect_stdout(buf):
            st.main(["--payee", PAYEE, "--rpc", URL, "--payer", A, "--hop", "opening-send"], post=post)
        self.assertIn("opener only", buf.getvalue())
        self.assertIn("2 later receive(s) totalling 399877 raw; opener share 0.0307%", buf.getvalue())

    def test_a_failed_history_read_gives_unreadable_fields_not_a_crash(self):
        for fail in (http.client.IncompleteRead(b""), OSError("reset")):
            r = run([A], {}, post=with_history({A: STRANGER}, fail=fail), hop="opening-send")["payers"][0]
            self.assertEqual(r["verdict"], "independent")
            self.assertEqual(r["opener"]["later_receives"], "unreadable")
            self.assertEqual(r["opener"]["opener_share"], "unreadable")

    def test_a_history_without_the_open_block_is_unreadable(self):
        rows = [{"type": "receive", "hash": "R" * 64, "amount": "5"}]
        r = run([A], {}, post=with_history({A: STRANGER}, rows), hop="opening-send")["payers"][0]
        self.assertEqual(r["opener"]["amount_raw"], "unreadable")

    def test_documents_name_the_caveat(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("STATED-PAYER-TEST.md", "README.md"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                self.assertIn("opener only", f.read(), name)


class Robustness(NoSockets):
    def test_http_exception_makes_one_payer_unreadable_and_the_run_goes_on(self):
        base = ledger({A: STRANGER, B: US})

        def post(url, payload):
            if payload.get("account") == A:
                raise http.client.IncompleteRead(b"{")
            return base(url, payload)

        rep = run([A, B], {}, post=post)
        self.assertEqual([r["verdict"] for r in rep["payers"]], ["unreadable", "not-independent"])

    def test_invalid_account_arguments_exit_3_without_a_traceback(self):
        for flag in ("--payee", "--excluded", "--exchange"):
            args = ["--payee", PAYEE, "--payer", A, "--rpc", URL]
            if flag == "--payee":
                args[1] = "nano_garbage"
            else:
                args += [flag, PAYEE[:-1] + ("1" if PAYEE[-1] != "1" else "3")]
            err, out = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = st.main(args, post=ledger({A: STRANGER}))
            self.assertEqual(code, 3, flag)
            self.assertIn("is not a Nano account", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())

    def test_a_failed_node_among_several_is_said(self):
        good = ledger({A: STRANGER})

        def post(url, payload):
            if url == "b":
                raise OSError("connection refused")
            return good(url, payload)

        rep = run([A], {}, urls=("a", "b"), post=post)
        r = rep["payers"][0]
        self.assertEqual(r["verdict"], "independent")
        self.assertIs(r.get("fewer_nodes"), True)
        self.assertIn("from 1 of 2 nodes", st.to_text(rep))
        self.assertIs(run([A], {A: STRANGER}, urls=("a", "b"))["payers"][0].get("fewer_nodes"), False)

    def test_counts_are_per_distinct_key(self):
        rep = run([A, "xrb_" + A[5:], B, "nano_bad"], {A: STRANGER, B: US})
        self.assertEqual(len(rep["payers"]), 3)
        self.assertEqual(sum(rep["counts"].values()), 3)
        self.assertEqual(rep["payers"][0]["payer"], "nano_bad")


XNO = 10 ** 30  # raw per XNO


def blk(*parts):
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest().upper()


def chain(histories):
    """A fake node from whole histories: `histories[acct]` is a list of
    (kind, counterparty, raw amount), oldest first, kind "receive" or "send".
    Returns (post, hashes) with hashes[acct][i] the hash of that account's i-th block."""
    by_hash, rows_of, hashes = {}, {}, {}
    for acct, items in histories.items():
        rows, hs = [], []
        for i, (kind, other, amount) in enumerate(items):
            bh = blk(acct, i, kind)
            hs.append(bh)
            rows.append({"type": kind, "hash": bh, "account": other, "amount": str(amount), "height": str(i + 1)})
            if kind == "receive":
                sh = blk("send", bh)
                by_hash[bh] = {"block_account": acct, "subtype": "open" if i == 0 else "receive",
                               "amount": str(amount), "contents": {"type": "state", "account": acct, "link": sh}}
                by_hash[sh] = {"block_account": other, "subtype": "send",
                               "contents": {"type": "state", "account": other, "link_as_account": acct}}
            else:
                by_hash[bh] = {"block_account": acct, "subtype": "send", "amount": str(amount),
                               "contents": {"type": "state", "account": acct, "link_as_account": other}}
        rows_of[acct], hashes[acct] = rows, hs

    def post(url, payload):
        acct = str(payload.get("account", "")).replace("xrb_", "nano_", 1)
        if payload["action"] == "account_info":
            if acct not in rows_of:
                return {"error": "Account not found"}
            return {"open_block": hashes[acct][0], "frontier": hashes[acct][-1],
                    "block_count": str(len(rows_of[acct]))}
        if payload["action"] == "block_info":
            return by_hash.get(payload["hash"], {"error": "Block not found"})
        if payload["action"] == "account_history":
            return {"account": acct, "history": rows_of[acct]}
        raise AssertionError(payload["action"])

    return post, hashes


class LargestReceive(NoSockets):
    """The amended test (2026-11-10): one hop back = the largest receive before the payment."""

    def test_default_rule_is_largest_receive_and_every_verdict_names_it(self):
        post, _ = chain({A: [("receive", STRANGER, 5)]})
        rep = run([A], {}, post=post)
        self.assertEqual(st.DEFAULT_HOP, "largest-receive")
        self.assertEqual(rep["hop_rule"], "largest-receive")
        self.assertEqual(rep["payers"][0]["hop_rule"], "largest-receive")

    def test_dust_opener_then_391_xno_from_a_party_hinges_on_the_391(self):
        # Opened by 0.000123 XNO from a stranger, then 391 XNO from us: the opening-send
        # rule reads `independent`; the amended rule must see the 391 and say not-independent.
        post, hs = chain({A: [("receive", STRANGER, 123 * 10 ** 24), ("receive", US, 391 * XNO)]})
        r = run([A], {}, post=post)["payers"][0]
        self.assertEqual((r["verdict"], r["party"], r["deciding_hop"]), ("not-independent", "excluded:" + US, 1))
        w = r["witness"]
        self.assertEqual((w["receive_block"], w["amount_raw"], w["source"]), (hs[A][1], str(391 * XNO), US))
        self.assertEqual(w["open_block"], hs[A][0])
        self.assertEqual(w["funding_send"], blk("send", hs[A][1]))
        self.assertEqual(w["before"], st.NO_PAYMENT)
        # The old rule, still selectable, keeps its old verdict so earlier counts re-derive.
        old = run([A], {}, post=post, hop="opening-send")["payers"][0]
        self.assertEqual((old["verdict"], old["witness"]["funder"]), ("independent", STRANGER))

    def test_dust_opener_from_a_party_then_391_from_a_stranger_is_independent(self):
        post, hs = chain({A: [("receive", US, 123 * 10 ** 24), ("receive", STRANGER, 391 * XNO)]})
        r = run([A], {}, post=post)["payers"][0]
        self.assertEqual((r["verdict"], r["witness"]["source"]), ("independent", STRANGER))
        self.assertEqual(r["receives"], {"before_payment": 2, "before_payment_raw": str(391 * XNO + 123 * 10 ** 24),
                                         "chosen_share": "99.9999%"})
        self.assertEqual(run([A], {}, post=post, hop="opening-send")["payers"][0]["verdict"], "not-independent")

    def test_receives_after_the_payment_are_ignored(self):
        post, hs = chain({A: [("receive", STRANGER, 5 * XNO), ("send", PAYEE, 5 * XNO), ("receive", US, 1000 * XNO)]})
        r = run([A], {}, post=post)["payers"][0]
        self.assertEqual((r["verdict"], r["witness"]["source"]), ("independent", STRANGER))
        self.assertEqual(r["witness"]["payment_block"], hs[A][1])
        self.assertIn("below height 2, the payment " + hs[A][1], r["witness"]["before"])
        self.assertEqual(r["receives"]["before_payment"], 1)

    def test_a_given_payment_moves_the_cut(self):
        post, hs = chain({A: [("receive", STRANGER, 5), ("send", PAYEE, 5), ("receive", US, 900),
                              ("send", PAYEE, 900)]})
        first = run([A], {}, post=post)["payers"][0]  # found: the first send to the payee
        self.assertEqual((first["verdict"], first["witness"]["payment_block"]), ("independent", hs[A][1]))
        second = run([A], {}, post=post, payments=(hs[A][3].lower(),))["payers"][0]
        self.assertEqual((second["verdict"], second["witness"]["source"]), ("not-independent", US))
        self.assertIn("the --payment given", second["witness"]["before"])

    def test_a_send_to_someone_else_is_not_the_payment(self):
        post, _ = chain({A: [("receive", STRANGER, 5), ("send", B, 5), ("receive", US, 900)]})
        r = run([A], {}, post=post)["payers"][0]
        self.assertEqual((r["verdict"], r["witness"]["before"]), ("not-independent", st.NO_PAYMENT))

    def test_a_payment_hash_that_is_not_a_send_to_the_payee_is_unreadable(self):
        post, hs = chain({A: [("receive", STRANGER, 5), ("send", B, 5)]})
        rep = run([A], {}, post=post, payments=(hs[A][1],))
        self.assertEqual(rep["payers"][0]["verdict"], "unreadable")
        self.assertEqual(rep["counts"]["independent"], 0)

    def test_ties_take_the_earliest_of_the_equal_largest(self):
        post, hs = chain({A: [("receive", B, 1), ("receive", STRANGER, 391), ("receive", US, 391)]})
        r = run([A], {}, post=post)["payers"][0]
        self.assertEqual((r["verdict"], r["witness"]["receive_block"], r["witness"]["receive_height"]),
                         ("independent", hs[A][1], 2))
        post, hs = chain({A: [("receive", B, 1), ("receive", US, 391), ("receive", STRANGER, 391)]})
        r = run([A], {}, post=post)["payers"][0]
        self.assertEqual((r["verdict"], r["witness"]["source"]), ("not-independent", US))

    def test_exchange_only_largest_receive_is_not_determinable(self):
        post, hs = chain({A: [("receive", STRANGER, 123 * 10 ** 24), ("receive", EXCH, 391 * XNO),
                              ("send", PAYEE, XNO)]})
        r = run([A], {}, post=post)["payers"][0]
        self.assertEqual((r["verdict"], r["why"], r["party"]), ("not-determinable", st.CARVE_OUT, "exchange"))
        self.assertEqual((r["witness"]["source"], r["witness"]["receive_block"]), (EXCH, hs[A][1]))
        self.assertEqual(run([A], {}, post=post, hop="opening-send")["payers"][0]["verdict"], "independent")

    def test_two_hops_walk_the_funders_largest_receive_before_its_send(self):
        # A's largest receive is from B; B's largest receive before that send is from us,
        # and what B received after it does not count.
        post, hs = chain({A: [("receive", B, 100)],
                          B: [("receive", STRANGER, 1), ("receive", US, 50), ("send", A, 100),
                              ("receive", STRANGER, 10 ** 6)]})
        # The send B made must be the one A received; point A's receive at it.
        def linked(url, payload):
            reply = post(url, payload)
            if payload["action"] == "block_info" and payload["hash"] == hs[A][0]:
                reply = dict(reply, contents=dict(reply["contents"], link=hs[B][2]))
            return reply

        r = run([A], {}, post=linked, hops=2)["payers"][0]
        self.assertEqual((r["verdict"], r["deciding_hop"], r["witness"]["source"]), ("not-independent", 2, US))
        self.assertIn("below height 3, the send " + hs[B][2], r["witness"]["before"])
        self.assertEqual(run([A], {}, post=linked, hops=1)["payers"][0]["verdict"], "independent")

    def test_unopened_and_unreadable_are_never_independent(self):
        rep = run([A], {}, post=chain({})[0])
        self.assertEqual(rep["payers"][0]["verdict"], "unopened")
        post, hs = chain({A: [("receive", STRANGER, 5)]})

        def no_history(url, payload):
            if payload["action"] == "account_history":
                return {"error": "unavailable"}
            return post(url, payload)

        rep = run([A], {}, post=no_history)
        self.assertEqual(rep["payers"][0]["verdict"], "unreadable")
        self.assertEqual(rep["counts"]["independent"], 0)

    def test_history_and_block_info_must_agree_on_the_source(self):
        post, hs = chain({A: [("receive", STRANGER, 5)]})

        def lying(url, payload):
            reply = post(url, payload)
            if payload["action"] == "account_history":
                reply = dict(reply, history=[dict(reply["history"][0], account=B)])
            return reply

        self.assertEqual(run([A], {}, post=lying)["payers"][0]["verdict"], "unreadable")

    def test_cli_text_names_rule_receive_and_before_and_bad_rule_exits_3(self):
        post, hs = chain({A: [("receive", STRANGER, 5), ("receive", US, 9)]})
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = st.main(["--payee", PAYEE, "--excluded", US, "--rpc", URL, "--payer", A], post=post)
        text = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("hop rule largest-receive", text)
        self.assertIn("largest receive " + hs[A][1], text)
        self.assertIn("before: no payment yet - all receives to date", text)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(st.main(["--payee", PAYEE, "--rpc", URL, "--payer", A, "--hop", "nope"], post=post), 3)

    def test_document_quotes_the_amendment_with_date_and_source(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "STATED-PAYER-TEST.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("the largest receive into the payer's account before the payment", text)
        self.assertIn("2026-11-10", text)
        self.assertIn("https://github.com/dhyabi2/agent-conversations/issues/11#issuecomment-6762616429", text)
        self.assertIn("--hop opening-send", text)


SOURCE = "https://example.org/service/statement#hot-wallet"


class SharedHotWallet(NoSockets):
    """The second amendment (2026-11-10): a hop ending at a publicly identified shared
    hot wallet (exchange, payment service, custodian) is not determinable, never
    independent, and the verdict names the wallet."""

    def test_named_payment_service_wallet_makes_the_payer_not_determinable_and_is_named(self):
        post, hs = chain({A: [("receive", STRANGER, 5), ("receive", F, 900)]})
        rep = st.stated_test(PAYEE, [A], [URL], (US,), (), 1, post, st.DEFAULT_HOP, (),
                             [(F, "payment-service", SOURCE)])
        r = rep["payers"][0]
        self.assertEqual((r["verdict"], r["why"], r["party"]), ("not-determinable", st.CARVE_OUT, "payment-service"))
        self.assertEqual(r["shared_wallet"], {"account": F, "kind": "payment-service", "source": SOURCE})
        self.assertEqual(rep["shared_wallets"], [{"account": F, "kind": "payment-service", "source": SOURCE}])
        self.assertEqual(rep["counts"]["independent"], 0)
        self.assertIn("publicly identified shared hot wallet", rep["test"])

    def test_the_same_funder_without_the_flag_reads_independent(self):
        post, hs = chain({A: [("receive", STRANGER, 5), ("receive", F, 900)]})
        r = run([A], {}, post=post, exchange=())["payers"][0]
        self.assertEqual((r["verdict"], r["witness"]["source"]), ("independent", F))
        self.assertNotIn("shared_wallet", r)

    def test_cli_shared_wallet_names_account_kind_source_in_json_and_text(self):
        post, hs = chain({A: [("receive", F, 900)]})
        args = ["--payee", PAYEE, "--excluded", US, "--rpc", URL, "--payer", A,
                "--shared-wallet", F + "=custodian=" + SOURCE + "?a=b"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(st.main(args + ["--json"], post=post), 0)
        r = json.loads(buf.getvalue())["payers"][0]
        self.assertEqual(r["shared_wallet"], {"account": F, "kind": "custodian", "source": SOURCE + "?a=b"})
        buf = io.StringIO()
        with redirect_stdout(buf):
            st.main(args, post=post)
        text = buf.getvalue()
        self.assertIn("not-determinable", text)
        self.assertIn(f"shared hot wallet {F}  kind custodian  source {SOURCE}?a=b", text)

    def test_exchange_flag_still_works_as_kind_exchange_with_source_null(self):
        post, hs = chain({A: [("receive", EXCH, 900)]})
        args = ["--payee", PAYEE, "--rpc", URL, "--payer", A, "--exchange", EXCH, "--json"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(st.main(args, post=post), 0)
        rep = json.loads(buf.getvalue())
        r = rep["payers"][0]
        self.assertEqual((r["verdict"], r["party"]), ("not-determinable", "exchange"))
        self.assertEqual(r["shared_wallet"], {"account": EXCH, "kind": "exchange", "source": None})
        self.assertEqual(rep["exchange"], [EXCH])
        buf = io.StringIO()
        with redirect_stdout(buf):
            st.main(args[:-1], post=post)
        self.assertIn("source none given (--exchange)", buf.getvalue())

    def test_bad_kind_http_source_bad_account_or_shape_exit_3(self):
        post, hs = chain({A: [("receive", F, 900)]})
        bad = [F + "=bank=" + SOURCE, F + "=exchange=http://example.org/x", F + "=exchange=https://",
               F + "=exchange=ftp://example.org", PAYEE[:-1] + ("1" if PAYEE[-1] != "1" else "3") + "=exchange=" + SOURCE,
               F + "=exchange", F]
        for value in bad:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = st.main(["--payee", PAYEE, "--rpc", URL, "--payer", A, "--shared-wallet", value], post=post)
            self.assertEqual(code, 3, value)
            self.assertIn("error:", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())

    def test_hop_depth_is_on_every_verdict(self):
        post = ledger({A: STRANGER, B: US, C: EXCH, US: STRANGER})
        rep = run([A, B, C, D, US, "nano_garbage"], {}, post=post)
        depth = {r["payer"]: (r["verdict"], r["hop_depth"]) for r in rep["payers"]}
        self.assertEqual(depth, {A: ("independent", 1), B: ("not-independent", 1), C: ("not-determinable", 1),
                                 D: ("unopened", None), US: ("not-independent", 0),
                                 "nano_garbage": ("unreadable", None)})
        for r in rep["payers"]:
            self.assertEqual(r["hop_depth"], r["deciding_hop"])
        r = run([A], {A: B, B: US}, hops=2)["payers"][0]
        self.assertEqual((r["verdict"], r["hop_depth"]), ("not-independent", 2))

    def test_v1_and_v2_wordings_are_kept_for_re_derivation(self):
        self.assertIn("exchange hot wallets set aside", st.STATED_TEST_V1)
        self.assertIn("largest receive into the payer before the payment", st.STATED_TEST_V2)
        self.assertIn("exchange, payment service, or custodian", st.STATED_TEST)


if __name__ == "__main__":
    unittest.main()
