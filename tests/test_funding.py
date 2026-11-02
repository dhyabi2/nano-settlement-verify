"""Funding provenance against a tiny fake ledger. No network."""
import json
import pytest
import nano_settlement_verify
import nano_funding as nf

HASH = "A" * 64
SEND2 = "B" * 64
RECV = "C" * 64
PREV = "D" * 64


def acct(n):  # valid checksummed accounts, derived from the library's own encoder if present
    return ACCTS[n]


def _mk_accounts():
    import nano_five_fields as ff
    out = {}
    for i, n in enumerate("payer payee spons dispenser plain selfie other".split()):
        out[n] = ff._address_of_key(bytes([i + 1]) * 32)
    return out


ACCTS = _mk_accounts()
PAYER, PAYEE, SPONS, DISP, PLAIN, SELFIE = (ACCTS[k] for k in "payer payee spons dispenser plain selfie".split())
T = 1_790_000_000


def send_block(sub="send", ts=T, confirmed="true"):
    return {"block_account": PAYER, "amount": "500", "local_timestamp": str(ts), "confirmed": confirmed,
            "subtype": sub, "contents": {"previous": PREV, "link_as_account": PAYEE, "link": "00" * 32}}


def ledger(funders, dispenser_sends=0, receive_ts=None, send=None, complete=True):
    """funders: [(account, amount, receive_local_timestamp)]"""
    def fetch(url, p):
        a = p["action"]
        if a == "block_info":
            h = p["hash"]
            if h == HASH:
                return send or send_block()
            if h == RECV:   # the payee's receive block, stamped LATE (pocketed days afterwards)
                return {"subtype": "receive", "confirmed": "true", "local_timestamp": str(T + 9 * 86400),
                        "contents": {"link": HASH}}
            return {"error": "Block not found"}
        if a == "account_history":
            acc = p["account"]
            if acc == PAYER:
                hist = [{"type": "receive", "account": f, "amount": str(m), "local_timestamp": str(ts),
                         "hash": ("%064X" % (i + 1)), "confirmed": "true"} for i, (f, m, ts) in enumerate(funders)]
                r = {"account": acc, "history": hist}
                if not complete:
                    r["previous"] = "E" * 64
                    r["history"] += [{"type": "receive", "account": PLAIN, "amount": "1",
                                      "local_timestamp": str(T - 1), "hash": "F" * 64, "confirmed": "true"}] * 0
                return r
            if acc == DISP:
                return {"account": acc, "history": [
                    {"type": "send", "account": ACCTS["other"][:-4] + "%04d" % i, "amount": "1000",
                     "local_timestamp": str(T - 100), "hash": "9" * 64} for i in range(dispenser_sends)]}
            return {"account": acc, "history": []}
        raise AssertionError(a)
    return fetch


def v(funders, **kw):
    fetch = ledger(funders, kw.pop("dispenser_sends", 0), send=kw.pop("send", None), complete=kw.pop("complete", True))
    nodes = kw.pop("nodes", ["https://n1.example"])
    return nf.provenance(kw.pop("block", HASH), nodes, fetch=fetch, **kw)


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("socket")))


def test_unflagged_funder_is_unsubsidised():
    r = v([(PLAIN, 100, T - 5000)])
    assert r["verdict"] == "unsubsidised" and r["decisive_receive"] is None


def test_sponsor_flag_labels_sponsor_funded_and_names_the_flag():
    r = v([(SPONS, 100, T - 5000)], sponsors=[SPONS])
    assert r["verdict"] == "sponsor_funded" and r["flags"]["sponsor"] == [SPONS]
    assert r["decisive_receive"]["funder"] == SPONS and r["decisive_receive"]["receive_hash"]
    assert v([(SPONS, 100, T - 5000)])["verdict"] == "unsubsidised"   # drop the flag, the label drops


def test_dispenser_found_from_ledger_and_no_flag_suppresses_it():
    r = v([(DISP, 100, T - 5000)], dispenser_sends=12)
    assert r["verdict"] == "dispenser_funded"
    r2 = v([(DISP, 100, T - 5000)], dispenser_sends=12, selfs=[PLAIN], sponsors=[PLAIN])
    assert r2["verdict"] == "dispenser_funded"
    r3 = v([(DISP, 100, T - 5000)], dispenser_sends=12, sponsors=[DISP])
    assert r3["verdict"] == "dispenser_funded"


def test_nine_recipients_is_not_a_dispenser():
    assert v([(DISP, 100, T - 5000)], dispenser_sends=9)["verdict"] == "unsubsidised"


def test_self_funded_when_funder_is_payee_or_flagged_self():
    assert v([(PAYEE, 100, T - 5000)])["verdict"] == "self_funded"
    assert v([(SELFIE, 100, T - 5000)], selfs=[SELFIE])["verdict"] == "self_funded"


def test_payment_time_is_the_send_block_not_the_receive_block():
    """Handing over the payee's LATE receive block resolves to the send, and the window is cut at the send's time."""
    late = v([(PLAIN, 100, T + 86400)], block=RECV)       # funded AFTER the payment: not in window
    assert late["payment_send_hash"] == HASH and late["payment_time"] == T
    assert late["verdict"] == "unknown" and late["why"] == "no_receive_in_window"
    ok = v([(SPONS, 100, T - 10)], block=RECV, sponsors=[SPONS])
    assert ok["verdict"] == "sponsor_funded"


def test_old_funding_outside_the_window_is_not_counted():
    r = v([(SPONS, 100, T - 800 * 3600)], sponsors=[SPONS], window_hours=720)
    assert r["verdict"] == "unknown" and r["why"] == "no_receive_in_window"


def test_incomplete_history_is_unknown_not_unsubsidised():
    r = v([(PLAIN, 100, T - 5000)], complete=False)
    assert r["verdict"] == "unknown" and r["why"] == "history_incomplete"


def test_flagged_funder_still_found_when_history_incomplete():
    r = v([(SPONS, 100, T - 5000)], complete=False, sponsors=[SPONS])
    assert r["verdict"] == "sponsor_funded"


def test_unconfirmed_payment_and_non_send_are_unknown():
    assert v([(PLAIN, 1, T - 5)], send=send_block(confirmed="false"))["why"] == "payment_not_confirmed"
    assert v([(PLAIN, 1, T - 5)], send=send_block(sub="change"))["why"] == "not_a_payment_block"
    assert v([(PLAIN, 1, T - 5)], send=send_block(ts=0))["verdict"] == "unknown"


def test_two_nodes_that_disagree_are_unknown_whichever_first():
    def fetch(url, p):
        return ledger([(PLAIN if "a" in url else SPONS, 100, T - 5)])(url, p)
    for order in (["https://a.x", "https://b.x"], ["https://b.x", "https://a.x"]):
        r = nf.provenance(HASH, order, sponsors=[SPONS], fetch=fetch)
        assert r["verdict"] == "unknown" and r["why"] == "nodes_disagree"


def test_clock_skew_between_nodes_is_not_disagreement():
    def fetch(url, p):
        skew = 1 if "b" in url else 0
        return ledger([(SPONS, 100, T - 5 + skew)])(url, p)
    r = nf.provenance(HASH, ["https://a.x", "https://b.x"], sponsors=[SPONS], fetch=fetch)
    assert r["verdict"] == "sponsor_funded"


def test_bad_inputs_make_no_call():
    boom = lambda *a: (_ for _ in ()).throw(AssertionError("called"))
    assert nf.provenance("xyz", ["u"], fetch=boom)["verdict"] == "unknown"
    assert nf.provenance(HASH, ["u"], sponsors=["nano_bad"], fetch=boom)["verdict"] == "unknown"


def test_share_shows_a_tiny_flagged_receive_beside_a_large_plain_one():
    r = v([(DISP, 10, T - 20), (PLAIN, 990, T - 10)], dispenser_sends=12)
    rows = {f["funder"]: f for f in r["funders"]}
    assert r["verdict"] == "dispenser_funded"
    assert rows[DISP]["share_of_window_receives_ppm"] == 10_000 and rows[PLAIN]["share_of_window_receives_ppm"] == 990_000


def test_exit_codes(capsys):
    fetch = ledger([(PLAIN, 100, T - 5)])
    assert nf.main(["--block", HASH, "--rpc", "https://n.x", "--json"], fetch=fetch) == 0
    assert nf.main(["--block", HASH, "--rpc", "https://n.x", "--sponsor", PLAIN, "--json"], fetch=fetch) == 3
    assert nf.main(["--block", "zz", "--json"], fetch=fetch) == 2


def test_doc_recipe_matches_the_requests_the_code_sends():
    import pathlib, re
    doc = (pathlib.Path(__file__).resolve().parent.parent / "FUNDING-PROVENANCE.md").read_text()
    bodies = [json.loads(b) for b in re.findall(r"-d '(\{.*?\})'", doc)]
    sent = []
    base = ledger([(PLAIN, 100, T - 5)])
    def spy(url, p):
        sent.append(p)
        return base(url, p)
    nf.provenance(HASH, ["https://n.x"], fetch=spy)
    kinds = {b["action"]: set(b) for b in bodies}
    for p in sent:
        want = {k for k in kinds[p["action"]] }
        assert set(p) <= want | {"head"}, (p, want)
    assert {p["action"] for p in sent} == set(kinds)
    assert "Payment time is the payer's send block" in doc and "never" in doc
