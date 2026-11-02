"""Laws for the free verification path (spec: agent-tool-free-verification-path, 14 laws plus the server).

All offline. The one real block (B749B757..., a 0.0005 XNO send read from rpc.nano.to on 2026-11-02)
is the fixture; the node is never called.
"""

import ast
import importlib.util
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import free_verify as fv  # noqa: E402
import free_verify_fetch as ff  # noqa: E402

BLOCK = "B749B757EE750FC9AEA72F33CB429EACCD2ABEC9F2CCF59BF17AFAC304C9A58F"
PAYEE = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"
OTHER = "nano_3m8cz87zwxb1y16ob4bzp1eyek78qaig8ktohk7d45b18sh6u9exbowbnekr"
RAW = "500000000000000000000000000"
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "block_B749B757.json").read_text())


def reply(**over):
    r = json.loads(json.dumps(FIXTURE))
    r.update(over)
    return r


def quote(**over):
    q = {"quote_id": "q_1", "pay_to": PAYEE, "amount_raw": RAW, "block_hash": BLOCK}
    q.update(over)
    return q


def test_1_confirmed_exact_send_is_settled():
    v = fv.check_settlement(quote(), reply())
    assert v["verdict"] == "settled" and v["amount_raw"] == RAW and v["cost_usd"] == 0.0


def test_2_block_absent_is_unknown_never_unpaid():
    v = fv.check_settlement(quote(), {"blocks": {}})
    assert v["verdict"] == "unknown" and "never unpaid" in v["why"]
    assert "unpaid" not in fv.VERDICTS


def test_3_a_balance_cannot_answer_was_this_paid():
    with pytest.raises(NotImplementedError, match="a balance view answers what the payee has pocketed"):
        fv.check_from_balance({"balance": "0"})


def test_4_wrong_amount_names_both():
    v = fv.check_settlement(quote(amount_raw="600" + "0" * 24), reply())
    assert v["verdict"] == "mismatch" and RAW in v["why"] and "600" + "0" * 24 in v["why"]


def test_5_wrong_address_names_both():
    v = fv.check_settlement(quote(pay_to=OTHER), reply())
    assert v["verdict"] == "mismatch" and PAYEE in v["why"] and OTHER in v["why"]


def test_6_unconfirmed_is_unknown_not_mismatch():
    assert fv.check_settlement(quote(), reply(confirmed="false"))["verdict"] == "unknown"


def test_7_receive_is_mismatch():
    assert fv.check_settlement(quote(), reply(subtype="receive"))["verdict"] == "mismatch"


def test_8_node_error_is_unknown_and_quoted():
    v = fv.check_settlement(quote(), {"error": "Block not found"})
    assert v["verdict"] == "unknown" and "Block not found" in v["why"]


def test_9_a_difference_in_the_28th_digit_is_a_mismatch():
    wide = "5" + "0" * 26 + "1"
    assert len(wide) == 28
    v = fv.check_settlement(quote(amount_raw=wide), reply(amount="5" + "0" * 27))
    assert v["verdict"] == "mismatch"


@pytest.mark.parametrize("bad", ["5e29", "5.0", 5.0, "0.5"])
def test_10_float_shaped_amounts_raise(bad):
    with pytest.raises(ValueError, match="amount_raw must be an integer string of raw"):
        fv.check_settlement(quote(amount_raw=bad), reply())


def test_11_every_error_row_has_its_message():
    cases = [
        (quote(amount_raw="12ab"), "amount_raw must be digits only, got 12ab"),
        (quote(pay_to="nano_x"), "pay_to is not a Nano address: nano_x"),
        (quote(block_hash="zz"), "block_hash must be 64 hex characters, got zz"),
    ]
    for q, msg in cases:
        with pytest.raises(ValueError) as e:
            fv.check_settlement(q, reply())
        assert str(e.value) == msg
    with pytest.raises(ValueError, match="block_response must be the node's response object"):
        fv.check_settlement(quote(), [])
    q = quote()
    del q["pay_to"]
    with pytest.raises(ValueError, match="quote is missing pay_to"):
        fv.check_settlement(q, reply())


def test_12_cost_report_is_computed_from_the_verdicts():
    v = [fv.check_settlement(quote(), reply())] * 12
    r = fv.cost_report(v)
    assert r["checks"] == 12 and r["cost_usd"] == 0.0 and r["paid_surfaces_used"] == []
    assert r["claim"] == "12 settlement checks, 0.0 USD outgoing, no node run, no account required"


def test_13_surfaces_without_a_measurement_carry_the_caveat(tmp_path):
    rows = fv.free_surfaces()
    assert rows and all(r["measured_at"] and r["measured_from"] for r in rows)
    p = tmp_path / "s.json"
    p.write_text(json.dumps([{"name": "x", "measured_at": None, "measured_from": None}]))
    assert fv.free_surfaces(str(p))[0]["caveat"] == "not measured from a machine"
    p.write_text(json.dumps([{"name": "x", "measured_at": "2026-11-02", "measured_from": None}]))
    with pytest.raises(ValueError):
        fv.free_surfaces(str(p))


def test_14_the_library_opens_no_socket():
    src = (ROOT / "free_verify.py").read_text()
    tree = ast.parse(src)
    imports = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imports.add(n.module.split(".")[0])
    assert imports == {"argparse", "decimal", "hashlib", "json", "re", "sys"}
    for n in ast.walk(tree):  # strip docstrings
        if isinstance(n, (ast.FunctionDef, ast.Module)) and ast.get_docstring(n):
            n.body = n.body[1:] or [ast.Pass()]
    stripped = ast.unparse(tree)
    for word in ("urllib", "socket", "requests", "http.client", "float" + "("):
        assert word not in stripped


def test_15_lowercase_hash_and_blocks_info_shape():
    wrapped = {"blocks": {BLOCK: FIXTURE}}
    assert fv.check_settlement(quote(block_hash=BLOCK.lower()), wrapped)["verdict"] == "settled"


def test_16_no_block_named_is_unknown():
    assert fv.check_settlement(quote(block_hash=None), {})["verdict"] == "unknown"


def test_17_cli_exit_codes(tmp_path):
    (tmp_path / "q.json").write_text(json.dumps(quote()))
    (tmp_path / "b.json").write_text(json.dumps(FIXTURE))
    assert fv.main(["--quote", str(tmp_path / "q.json"), "--block", str(tmp_path / "b.json")]) == 0
    (tmp_path / "q.json").write_text(json.dumps(quote(pay_to=OTHER)))
    assert fv.main(["--quote", str(tmp_path / "q.json"), "--block", str(tmp_path / "b.json")]) == 3


def test_18_fetch_falls_through_dead_nodes_then_says_unknown():
    seen = []

    def fake(h, nodes):
        seen.append(nodes)
        return {"error": "no node answered"}, None

    v = ff.verify_with_nodes(quote(), ["https://a.example"], fake)
    assert v["verdict"] == "unknown" and v["node"] is None


def test_19_server_answers_and_rate_limits():
    limiter = ff._Limiter(2)
    handler = ff.make_handler(["x"], fetch=lambda h, n: (FIXTURE, "fake"), limiter=limiter)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/verify?pay_to=%s&amount_raw=%s&block=%s" % (server.server_port, PAYEE, RAW, BLOCK)
    try:
        with urllib.request.urlopen(base) as r:
            assert json.load(r)["verdict"] == "settled"
        with urllib.request.urlopen(base.replace(RAW, "1")) as r:
            assert json.load(r)["verdict"] == "mismatch"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base)
        assert e.value.code == 429
    finally:
        server.shutdown()


def test_20_server_refuses_a_bad_amount_with_400():
    handler = ff.make_handler(["x"], fetch=lambda h, n: (FIXTURE, "fake"), limiter=ff._Limiter(9))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/verify?pay_to=%s&amount_raw=0.5&block=%s" % (server.server_port, PAYEE, BLOCK)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url)
        assert e.value.code == 400
    finally:
        server.shutdown()
