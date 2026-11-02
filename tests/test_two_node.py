"""Two-node verdict: no network. The block is a real one pinned verbatim in tests/fixtures."""
import hashlib, json, pathlib, re, subprocess, sys
import pytest
import nano_settlement_verify
import nano_two_node as tn

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = (ROOT / "TWO-NODE-CHECK.md").read_text()
REAL = json.loads((ROOT / "tests/fixtures/block_B749B757.json").read_text())
H = "B749B757EE750FC9AEA72F33CB429EACCD2ABEC9F2CCF59BF17AFAC304C9A58F"
PAYEE = REAL["contents"]["link_as_account"]
AMT = "500000000000000000000000000"
A, B = "https://a.example/rpc", "https://b.example/rpc"


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to open a socket")
    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


def fetcher(replies):
    def f(url, payload):
        assert payload == tn.REQUEST_FOR(H)
        r = replies[url]
        if isinstance(r, Exception):
            raise r
        return r
    return f


def run(replies, nodes=(A, B), issued=1790150000, amount=AMT, **kw):
    return tn.two_node_verdict(H, amount, "inv1", issued, PAYEE, nodes=nodes, fetch=fetcher(replies), **kw)


def test_all_five_fields_settle_and_say_what_settled_means():
    v = run({A: REAL, B: REAL})
    assert v["verdict"] == "settled" and v["missing_fields"] == []
    assert v["settled_means"] == "I can start work. This is NOT buyer_accept."
    assert v["operator_list_empty"] is True


def test_single_node_is_never_settled():
    v = run({A: REAL}, nodes=(A,))
    assert v["verdict"] == "unknown" and "nodes_read" in v["missing_fields"]


def test_same_endpoint_twice_is_one_witness():
    v = run({A: REAL, A + "/": REAL}, nodes=(A, A + "/"))
    assert v["verdict"] == "unknown"


def test_unreachable_second_node_drops_to_unknown():
    v = run({A: REAL, B: OSError("down")})
    assert v["verdict"] == "unknown"


@pytest.mark.parametrize("order", [(A, B), (B, A)])
def test_disagreement_is_unknown_whichever_node_is_first(order):
    other = dict(REAL, amount="1")
    v = run({A: REAL, B: other}, nodes=order)
    assert v["verdict"] == "unknown"


def test_unconfirmed_is_unknown():
    v = run({A: dict(REAL, confirmed="false"), B: dict(REAL, confirmed="false")})
    assert v["verdict"] == "unknown" and "block_hash_confirmed" in v["missing_fields"]


def test_one_raw_over_or_under_is_refused_not_at_least():
    assert run({A: REAL, B: REAL}, amount=str(int(AMT) - 1))["verdict"] == "refused"
    assert run({A: REAL, B: REAL}, amount=str(int(AMT) + 1))["verdict"] == "refused"


def test_amount_is_integer_string_not_float():
    assert run({A: REAL, B: REAL}, amount="1e27")["verdict"] == "unknown"
    assert run({A: REAL, B: REAL}, amount="500000000000000000000000001")["verdict"] == "refused"


def test_invoice_equal_to_or_after_block_is_refused():
    assert run({A: REAL, B: REAL}, issued=1790150110)["verdict"] == "refused"
    assert run({A: REAL, B: REAL}, issued=1790150111)["verdict"] == "refused"


def test_zero_local_timestamp_is_unknown():
    z = dict(REAL, local_timestamp="0")
    assert run({A: z, B: z})["verdict"] == "unknown"


def test_source_on_operator_list_is_refused_and_empty_list_flags_itself():
    v = run({A: REAL, B: REAL}, operator_accounts=[REAL["block_account"]])
    assert v["verdict"] == "refused" and v["operator_list_empty"] is False


def test_wrong_payee_is_refused():
    v = tn.two_node_verdict(H, AMT, "i", 1790150000, REAL["block_account"], nodes=(A, B),
                            fetch=fetcher({A: REAL, B: REAL}))
    assert v["verdict"] == "refused"


def test_bad_block_hash_refused_before_any_network_call():
    v = tn.two_node_verdict("zz", AMT, "i", 1, PAYEE, fetch=lambda *a: 1 / 0)
    assert v["verdict"] == "refused"


def test_exit_codes(capsys):
    argv = ["--block", H, "--amount-raw", AMT, "--invoice-id", "i", "--invoice-issued-at", "1790150000",
            "--payee", PAYEE, "--node", A, "--node", B, "--json"]
    f = fetcher({A: REAL, B: REAL})
    assert tn.main(argv, fetch=f) == 0
    assert tn.main(argv[:3] + [str(int(AMT) + 1)] + argv[4:], fetch=f) == 4
    assert tn.main(argv[:-5] + ["--node", A, "--json"], fetch=f) == 2


def test_documents_curl_recipe_is_the_request_the_code_sends():
    curls = re.findall(r"curl -s .*? -d '(\{.*?\})' (https://\S+)", DOC)
    assert len(curls) >= 2
    for body, _url in curls:
        assert json.loads(body.replace("<BLOCK>", H)) == tn.REQUEST_FOR(H)
    assert "Content-Type: application/json" in DOC and "-A 'Mozilla/5.0'" in DOC


def test_documents_hash_recompute_prints_the_block_hash(tmp_path):
    code = re.search(r"```python\n(.*?)```", DOC, re.S).group(1)
    (tmp_path / "reply.json").write_text(json.dumps(REAL))
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == H


def test_document_has_the_limits_section_and_the_worked_example():
    assert "NOT buyer acceptance" in DOC and H in DOC and "What this does not prove" in DOC
