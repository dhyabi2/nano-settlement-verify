"""Stranger recount on both rails, from recorded replies. No network."""
import json
import pytest
import nano_settlement_verify
import nano_recount as rc

PAYEE = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"
BROKEN = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx8"
P1 = "nano_3gqrm67xjp33gew1pmrbhx8rm6y1yrcogbid36ku5nc9izp8nn3ec5pa48yt"
P2 = "nano_1xug1q5t7nxoj3ywwzokiea9jz8fq8qfgzp8pbyfr3co3e5xgj755uofu8ue"
NODE = "http://node.example"
PRICE = 100000000000000000000000000   # 0.0001 XNO
EVM = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
EVM_BAD = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAEd"
SENT = []


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    SENT.clear()
    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("socket")))


def row(kind, acct, h, when, amount=PRICE, confirmed="true"):
    return {"type": kind, "account": acct, "amount": str(amount), "height": str(h), "confirmed": confirmed,
            "local_timestamp": str(when), "hash": f"{h:064X}"}


def nano_node(monkeypatch, history):
    def post_json(url, payload):
        SENT.append((url, payload))
        return {"account": PAYEE, "history": history}
    monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)


def test_nano_fixture_exact_counts(monkeypatch):
    nano_node(monkeypatch, [row("receive", P1, 4, 1790600000), row("receive", P1, 3, 1790500000),
                            row("receive", P2, 2, 1790400000), row("send", P1, 5, 1790700000),
                            row("receive", P2, 1, 1790300000, confirmed="false")])
    r = rc.recount(PAYEE, nodes=[NODE], now=1790800000)
    assert (r["distinct_payers"], r["payments"]) == (2, 3)
    assert r["total_received"] == "0.0003" and r["method_is_an_inference"] is False
    assert r["excluded"]["unconfirmed"] == 1 and r["complete"] is True
    assert r["first_payment_at"] < r["last_payment_at"]


def test_zero_is_published_not_withheld(monkeypatch):
    nano_node(monkeypatch, [])
    r = rc.recount(PAYEE, nodes=[NODE])
    assert (r["distinct_payers"], r["payments"], r["complete"]) == (0, 0, True)


def test_exclude_self_removes_a_declared_account(monkeypatch):
    nano_node(monkeypatch, [row("receive", P1, 2, 1790600000), row("receive", P2, 1, 1790500000)])
    assert rc.recount(PAYEE, nodes=[NODE], exclude=[P2])["distinct_payers"] == 1


def test_rule_stated_after_rows_warns(monkeypatch):
    nano_node(monkeypatch, [row("receive", P1, 1, 1790600000)])
    r = rc.recount(PAYEE, nodes=[NODE], rule_stated_at="2030-01-01T00:00:00Z")
    assert "rule_stated_after_the_rows_it_covers" in r["warnings"]
    ok = rc.recount(PAYEE, nodes=[NODE], rule_stated_at="2000-01-01T00:00:00Z")
    assert "warnings" not in ok


def topic(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


def log(src, to, block, amount):
    return {"topics": [rc.TRANSFER_TOPIC, topic(src), topic(to)], "data": hex(amount), "blockNumber": hex(block)}


def evm_fetch(logs, fail_above=None, latest=1000):
    def f(url, p):
        SENT.append((url, p))
        if p["method"] == "eth_blockNumber":
            return {"result": hex(latest)}
        q = p["params"][0]
        lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
        if fail_above is not None and hi - lo + 1 > fail_above:
            return {"error": {"message": "limit exceeded"}}
        return {"result": [l for l in logs if lo <= int(l["blockNumber"], 16) <= hi]}
    return f


A1 = "0x" + "11" * 20
A2 = "0x" + "22" * 20


def test_usdc_fixture_exact_counts_same_schema():
    logs = [log(A1, EVM, 10, 1_500_000), log(A1, EVM, 600, 500_000), log(A2, EVM, 900, 2_000_000)]
    r = rc.recount(EVM, from_block=0, fetch=evm_fetch(logs))
    assert (r["distinct_payers"], r["payments"], r["total_received"]) == (2, 3, "4")
    nano = {"v", "address", "network", "asset", "distinct_payers", "payments", "total_received",
            "method_is_an_inference", "excluded", "_rule", "source", "as_of", "complete", "method"}
    assert nano <= set(r)


def test_usdc_self_sends_excluded_and_counted():
    logs = [log(EVM, EVM, 5, 1), log(A1, EVM, 6, 10)]
    r = rc.recount(EVM, from_block=0, fetch=evm_fetch(logs))
    assert r["distinct_payers"] == 1 and r["excluded"]["self_sends"] == 1


def test_window_halves_when_the_node_limits_a_wide_range():
    logs = [log(A1, EVM, b, 1) for b in (1, 4000, 9999, 20000)]
    r = rc.recount(EVM, from_block=0, fetch=evm_fetch(logs, fail_above=5000, latest=25000))
    assert r["payments"] == 4 and r["complete"] is True


def test_truncated_range_refuses_and_prints_no_count():
    with pytest.raises(rc.Refusal) as e:
        rc.recount(EVM, from_block=0, fetch=evm_fetch([log(A1, EVM, 1, 1)], fail_above=100))
    assert e.value.code == "range_incomplete" and e.value.exit_code == 3


def test_cli_truncated_exit3_no_count(capsys):
    code = rc.main(["--address", EVM, "--from-block", "0", "--json"], fetch=evm_fetch([], fail_above=100))
    out = json.loads(capsys.readouterr().out)
    assert code == 3 and out["refused"] == "range_incomplete" and "distinct_payers" not in out


def test_node_http_error_on_logs_is_range_incomplete_not_a_crash():
    def f(url, p):
        if p["method"] == "eth_blockNumber":
            return {"result": "0x10"}
        raise OSError("429")
    with pytest.raises(rc.Refusal) as e:
        rc.recount(EVM, from_block=0, fetch=f)
    assert e.value.code == "range_incomplete"


def test_unreachable_node_is_exit_3():
    with pytest.raises(rc.Refusal) as e:
        rc.recount(EVM, from_block=0, fetch=lambda *a: (_ for _ in ()).throw(OSError("down")))
    assert (e.value.code, e.value.exit_code) == ("node_unreachable", 3)


def test_bad_checksum_refuses_both_rails_before_any_call():
    boom = lambda *a: (_ for _ in ()).throw(AssertionError("called"))
    for bad in (BROKEN, EVM_BAD):
        with pytest.raises(rc.Refusal) as e:
            rc.recount(bad, from_block=0, fetch=boom)
        assert e.value.exit_code == 2


def test_usage_errors_exit_64():
    for kw in (dict(address=PAYEE, network="eip155:8453"), dict(address=EVM, network="nano:mainnet"),
               dict(address=EVM, from_block=None), dict(address=EVM, from_block=0, asset="USDT")):
        with pytest.raises(rc.Refusal) as e:
            rc.recount(fetch=lambda *a: 1 / 0, **kw)
        assert e.value.exit_code == 64


def test_keccak_and_topic_are_the_real_constants():
    assert rc.keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert rc.TRANSFER_TOPIC == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    assert rc.eip55_valid(EVM) and not rc.eip55_valid(EVM_BAD)


def test_no_key_in_any_request(monkeypatch):
    nano_node(monkeypatch, [row("receive", P1, 1, 1790600000)])
    rc.recount(PAYEE, nodes=[NODE])
    rc.recount(EVM, from_block=0, fetch=evm_fetch([log(A1, EVM, 1, 1)]))
    blob = json.dumps(SENT).lower()
    for w in ("apikey", "api_key", "token", "authorization", "secret", "seed"):
        assert w not in blob
