"""Laws for links/public_check.py (backlog 146): a message never carries a link a stranger cannot open."""
import urllib.request

import pytest

from links import public_check as pc

RAW404 = "https://raw.githubusercontent.com/dhyabi2/nano-settlement-verify/main/FIVE_FIELDS.md"
GOOD = "https://raw.githubusercontent.com/dhyabi2/nano-settlement-verify/main/README.md"


def fake(table):
    def f(url):
        v = table[url]
        if isinstance(v, Exception):
            raise v
        return v
    return f


def test_404_is_not_public():
    r = pc.check_text(f"see {RAW404}", fake({RAW404: (404, RAW404, b"404: Not Found", "text/plain")}))
    assert r["urls"][0]["do_not_send"] is True and r["verdict_for_the_message"] == "refuse"


def test_private_repo_raw_url_reason():
    r = pc.probe(RAW404, fake({RAW404: (404, RAW404, b"404: Not Found", "text/plain")}))
    assert r["reason"] == "private_repo_or_missing_path"


def test_unknown_is_not_public():
    r = pc.probe(GOOD, fake({GOOD: TimeoutError("timed out")}))
    assert r["public"] == "unknown" and r["do_not_send"] is True and r["public"] is not True


def test_timeout_retries_once_only():
    n = []

    def f(url):
        n.append(1)
        raise TimeoutError("timed out")
    pc.probe(GOOD, f)
    assert len(n) == 2


def test_our_egress_block_is_labelled_as_ours():
    r = pc.probe(GOOD, fake({GOOD: pc.EgressBlocked(GOOD)}))
    assert r["reason"] == "our_egress_blocked" and "our limit" in r["detail"] and r["do_not_send"]


def test_no_credentials_are_sent(monkeypatch):
    seen = {}

    class Resp:
        status = 200
        headers = {"Content-Type": "text/plain"}
        def read(self, n=-1): return b"ok"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class Op:
        def open(self, req, timeout=None):
            seen["h"] = {k.lower() for k in req.headers}
            return Resp()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: Op())
    st, *_ = pc.default_fetch(GOOD)
    assert st == 200
    assert not ({"authorization", "cookie", "x-api-key"} & seen["h"])
    assert "user-agent" in seen["h"]


def test_tls_failure_never_retries_insecurely():
    import inspect
    src = inspect.getsource(pc)
    assert "verify=False" not in src and "_create_unverified_context" not in src and "CERT_NONE" not in src
    n = []

    def f(url):
        n.append(1)
        raise pc.TlsError("bad cert")
    r = pc.probe(GOOD, f)
    assert r["reason"] == "tls" and len(n) == 1


def test_strip_leaves_evidence_or_refuses():
    t = {GOOD: (200, GOOD, b"# readme", "text/plain"), RAW404: (404, RAW404, b"x", "")}
    assert pc.check_text(f"{GOOD} and {RAW404}", fake(t))["verdict_for_the_message"] == "strip"
    assert pc.check_text(RAW404, fake(t))["verdict_for_the_message"] == "refuse"


def test_auth_wall_200_is_not_public():
    u = "https://example.org/x"
    r = pc.probe(u, fake({u: (200, u, b"<html>Please sign in to continue</html>", "text/html")}))
    assert r["public"] is False and r["reason"] == "auth_wall"


def test_empty_200_is_not_public():
    u = "https://example.org/x"
    assert pc.probe(u, fake({u: (200, u, b"", "text/html")}))["public"] is False


def test_caveat_is_verbatim_and_fixed():
    r = pc.check_text("no links here", fake({}))
    assert r["from_this_vantage_only"] == pc.CAVEAT
    assert r["all_public"] is True and r["verdict_for_the_message"] == "send"


def test_trailing_punctuation_is_not_part_of_the_url():
    assert pc.extract_urls("see (https://a.example/x). Also https://b.example/y, ok") == \
        ["https://a.example/x", "https://b.example/y"]


def test_dead_and_good_fixtures_2026_10_15():
    t = {GOOD: (200, GOOD, b"x" * 40739, "text/plain"),
         RAW404: (404, RAW404, b"404: Not Found", "text/plain")}
    assert pc.probe(GOOD, fake(t))["public"] is True
    assert pc.probe(RAW404, fake(t))["public"] is False


def test_ranged_206_counts_as_public():
    u = "https://example.org/x"
    assert pc.probe(u, fake({u: (206, u, b"partial", "text/plain")}))["public"] is True
