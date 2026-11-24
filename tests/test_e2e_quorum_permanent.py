"""Law: e2e_quorum_check.py reports a permanent NotCorroborated on the live block as a failure.

The script's live section reads a NotCorroborated as "the node is not reachable
from here" and skips - unless it is `permanent`: then an endpoint answered and
showed the block can never be bound, which is no reachability problem, and the
script must fail. These tests run the script's loopback sections for real (on
127.0.0.1 only) and replace only the live call, so they never leave the machine.
"""

import io
import pathlib
import runpy
from contextlib import redirect_stdout

import pytest

import nano_settlement_verify
from nano_settlement_verify import quorum

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "e2e_quorum_check.py"
LIVE_HASH = "6B7AF8D4F9B922F1AEF96CE7B543E11C26EE7178573401813437FDBAC814965F"


def _run(monkeypatch, permanent):
    real = quorum.verify_corroborated

    def verify_corroborated(block_hash, *args, **kwargs):
        if str(block_hash).upper() == LIVE_HASH:
            error = quorum.NotCorroborated(0, 2, {"live": "stubbed"})
            error.permanent = permanent
            raise error
        return real(block_hash, *args, **kwargs)

    def no_live_network(rpc_url, payload):
        if not str(rpc_url).startswith("http://127.0.0.1"):
            raise AssertionError(f"the test tried to reach {rpc_url}")
        return real_post(rpc_url, payload)

    real_post = nano_settlement_verify.post_json
    monkeypatch.setattr(quorum, "verify_corroborated", verify_corroborated)
    monkeypatch.setattr(nano_settlement_verify, "post_json", no_live_network)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, "127.0.0.1")
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NANO_RPC", "http://127.0.0.1:1")
    out = io.StringIO()
    with redirect_stdout(out):
        runpy.run_path(str(SCRIPT), run_name="__main__")
    return out.getvalue()


def test_a_permanent_not_corroborated_on_the_live_block_fails_the_script(monkeypatch):
    with pytest.raises(SystemExit) as caught:
        _run(monkeypatch, permanent=True)
    assert "permanently unbindable" in str(caught.value.code)
    assert str(caught.value.code).startswith("FAIL:")


def test_an_ordinary_not_corroborated_on_the_live_block_is_skipped(monkeypatch):
    printed = _run(monkeypatch, permanent=False)
    assert "skipped, the node is not reachable from here: NotCorroborated" in printed
    assert "all hold end to end" in printed
