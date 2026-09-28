"""Law: every node call names itself.

Cloudflare-fronted Nano RPCs - rpc.nano.to, the one the README uses - answer
urllib's default `Python-urllib/3.x` User-Agent with 403 "error code: 1010", a
refusal that reads exactly like a bad node. Measured 2026-09-28: the same
account_info request answered 200 from curl and 403 from verify().
"""

import io
import json

import nano_settlement_verify


def test_node_call_sends_its_own_user_agent(monkeypatch):
    seen = {}

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        seen["ua"] = request.get_header("User-agent")
        seen["type"] = request.get_header("Content-type")
        seen["timeout"] = timeout
        return Reply(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", fake_urlopen)
    assert nano_settlement_verify.post_json("http://127.0.0.1:7076", {"action": "version"}) == {"ok": True}
    assert seen["ua"], "no User-Agent: urllib would send its default and Cloudflare refuses it"
    assert not seen["ua"].lower().startswith("python-urllib")
    assert seen["ua"].startswith("nano-settlement-verify/")
    assert seen["type"] == "application/json"
    assert seen["timeout"] == nano_settlement_verify.RPC_TIMEOUT_S
