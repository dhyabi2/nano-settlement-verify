"""node_set: every HTTP call is stubbed; a test that touches the network is a bug."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import node_set  # noqa: E402
from node_set import probe as pr  # noqa: E402

GOOD = json.dumps({"confirmed": "true", "amount": pr.PROBE_AMOUNT})


def stub(table):
    def post(url, body, timeout):
        assert body["hash"] == pr.PROBE_BLOCK and body["action"] == "block_info"
        v = table[url]
        if isinstance(v, Exception):
            raise v
        return v
    return post


def write(tmp_path, data):
    p = tmp_path / "NODES.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    return str(p)


def test_fallback_urls_reads_usable_in_order(tmp_path):
    p = write(tmp_path, {"usable": ["https://a", "https://b", "https://c"]})
    assert node_set.fallback_urls(path=p) == ["https://a", "https://b", "https://c"]


def test_fallback_urls_missing_file_returns_empty(tmp_path):
    assert node_set.fallback_urls(path=str(tmp_path / "none.json")) == []


def test_fallback_urls_malformed_returns_empty(tmp_path):
    assert node_set.fallback_urls(path=write(tmp_path, "{")) == []


def test_fallback_urls_drops_plaintext(tmp_path):
    p = write(tmp_path, {"usable": ["http://leak", "https://ok"]})
    assert node_set.fallback_urls(path=p) == ["https://ok"]


def test_fallback_urls_exclude(tmp_path):
    p = write(tmp_path, {"usable": ["https://a", "https://b"]})
    assert node_set.fallback_urls(exclude="https://a", path=p) == ["https://b"]


def test_probe_marks_usable_on_exact_amount():
    r = pr.probe(["https://a"], stub({"https://a": (200, GOOD)}))
    assert r["usable"] == ["https://a"]


def test_probe_rejects_right_shape_wrong_amount():
    body = json.dumps({"confirmed": "true", "amount": "1"})
    r = pr.probe(["https://a"], stub({"https://a": (200, body)}))
    assert r["unusable"][0]["why"] == "wrong_amount"


def test_probe_rejects_unconfirmed():
    body = json.dumps({"confirmed": "false", "amount": pr.PROBE_AMOUNT})
    r = pr.probe(["https://a"], stub({"https://a": (200, body)}))
    assert r["unusable"][0]["why"] == "not_confirmed"


def test_probe_rejects_rpc_error():
    r = pr.probe(["https://a"], stub({"https://a": (200, '{"error": "Block not found"}')}))
    assert r["unusable"][0]["why"] == "rpc_error"


def test_probe_records_http_code_and_bad_json():
    r = pr.probe(["https://a", "https://b"], stub({"https://a": (403, ""), "https://b": (200, "<html>")}))
    assert [u["why"] for u in r["unusable"]] == ["http_403", "bad_json"]


def test_probe_records_connect_rejected_and_timeout():
    r = pr.probe(["https://a", "https://b"], stub({"https://a": ConnectionRefusedError(), "https://b": TimeoutError()}))
    assert r["unusable"][0] == {"url": "https://a", "why": "connect_rejected", "http": None}
    assert r["unusable"][1]["why"] == "timeout"


def test_probe_exits_3_when_none_usable(tmp_path):
    out = tmp_path / "n.json"
    rc = pr.main(["--out", str(out), "--candidate", "https://a"], post=stub({"https://a": ConnectionRefusedError()}))
    data = json.loads(out.read_text())
    assert rc == 3 and data["usable"] == [] and data["two_node_check_possible"] is False


def test_probe_sets_two_node_possible():
    t = {"https://a": (200, GOOD), "https://b": (200, GOOD), "https://c": (403, "")}
    assert pr.probe(["https://a", "https://b"], stub(t))["two_node_check_possible"] is True
    assert pr.probe(["https://a", "https://c"], stub(t))["two_node_check_possible"] is False


def test_probe_dedupes_candidates():
    calls = []
    def post(u, b, t):
        calls.append(u)
        return 200, GOOD
    pr.probe(["https://a", "https://a"], post)
    assert calls == ["https://a"]


def test_probed_from_defaults_to_unknown(tmp_path):
    out = tmp_path / "n.json"
    pr.main(["--out", str(out), "--candidate", "https://a"], post=stub({"https://a": (200, GOOD)}))
    assert json.loads(out.read_text())["probed_from"] == "unknown"


def test_unwritable_out_exits_5(tmp_path):
    rc = pr.main(["--out", str(tmp_path / "nodir" / "n.json"), "--candidate", "https://a"],
                 post=stub({"https://a": (200, GOOD)}))
    assert rc == 5


def test_nodes_json_committed_is_not_empty():
    data = json.loads((ROOT / "node_set" / "NODES.json").read_text())
    assert data["usable_count"] >= 2 and len(data["usable"]) == data["usable_count"]
    assert data["probed_from"] != "unknown"
    assert all(u.startswith("https://") for u in data["usable"])
