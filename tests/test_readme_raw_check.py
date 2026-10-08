"""Law: the README's no-code check asks the node exactly what verify() asks.

Two outside agents (Moltbook, 2026-10-08) refused to run claimant-authored code
to verify a claimant's payment. The README therefore shows the raw node call a
skeptic can type instead. If that call drifts from the one verify() makes, the
skeptic checks a different thing than the library does, so the two are pinned
together here.
"""

import json
import pathlib
import re

import nano_settlement_verify

README = pathlib.Path(__file__).resolve().parent.parent / "README.md"
SECTION = "## Check it without running our code"


def _section():
    text = README.read_text(encoding="utf-8")
    assert SECTION in text, "README lost its no-code check"
    return text.split(SECTION, 1)[1].split("\n## ", 1)[0]


def test_readme_curl_body_is_the_payload_verify_sends(monkeypatch):
    body = re.search(r"-d '(\{.*?\})'", _section(), re.S)
    assert body, "no curl -d body in the no-code check"
    shown = json.loads(body.group(1))

    sent = {}

    def fake_post(rpc_url, payload):
        sent.update(payload)
        return {"error": "Block not found"}

    monkeypatch.setattr(nano_settlement_verify, "post_json", fake_post)
    try:
        nano_settlement_verify.verify(shown["hash"], 1, "nano_x", "https://node")
    except nano_settlement_verify.NotFound:
        pass
    assert shown == sent


def test_readme_names_the_headers_and_fields_verify_relies_on():
    section = _section()
    # rpc.nano.to answers a body without this header "Action not provided" (400),
    # and urllib's default User-Agent with 403 - both read like a bad block.
    for needed in ("Content-Type: application/json", "-A ", "confirmed",
                   "subtype", "amount", "link_as_account", "destination"):
        assert needed in section, needed
