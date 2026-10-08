"""Laws for nano_claim: was a send claimed, read once off the public chain.

No test touches the network: the node is a stub that answers by RPC action,
and the no_sockets fixture fails any test that tries to open a socket.

Most of these laws are about the answers it must NOT give: a node that could
not be read, a receive that could not be found, a block that is not a send -
each must come back as something other than a clean "claimed" or "receivable",
with a nonzero exit, because a missing read that looks like an outcome is the
one mistake a reader of this output cannot see.

The addresses are real and checksum-valid: the sender and the destination of
the starter send whose hash is SEND below.
"""

import json

import pytest

import nano_settlement_verify

import nano_claim

A = "http://127.0.0.1:7071"

SEND = "243AF6BFC56A199CB2C40C0ABFF4C583B905615E68F54D6CBCCD1A5F0D0DF57C"
SENDER = "nano_1434j1n4sin4cefs5njibag4tsmo596fmg3s6bdogtod3ndmdfez5yuebrh9"
DEST = "nano_3rra4cwps4w6oh8sfctde3tt1g3tjgh39tgxbxdkbn8kmu5yykmgtqaehp6p"
OTHER = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"
STARTER = 10000000000000000000000000  # 0.00001 XNO, in raw
RECEIVE = "AB" * 32
NOW = "2026-10-08T12:00:00Z"


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a socket")

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


def send_block(receivable="0", confirmed="true", subtype="send", link_as_account=DEST):
    """A blocks_info entry for the send, as a node serves it with json_block."""
    return {
        "block_account": SENDER,
        "amount": str(STARTER),
        "height": "29",
        "confirmed": confirmed,
        "subtype": subtype,
        "receivable": receivable,
        "pending": receivable,
        "contents": {
            "type": "state",
            "account": SENDER,
            "link": SEND,
            "link_as_account": link_as_account,
        },
    }


def opened():
    return {"frontier": RECEIVE, "open_block": RECEIVE, "block_count": "1", "balance": str(STARTER)}


NOT_OPENED = {"error": "Account not found", "balance": "0"}


def history_row(block_hash=RECEIVE, kind="receive", confirmed="true", account=SENDER):
    """An account_history row as rpc.nano.to serves it: no link, even with raw."""
    return {
        "type": kind,
        "account": account,
        "amount": str(STARTER),
        "height": "1",
        "hash": block_hash,
        "confirmed": confirmed,
    }


def receive_block(link=SEND, confirmed="true"):
    return {
        "block_account": DEST,
        "amount": str(STARTER),
        "confirmed": confirmed,
        "subtype": "open",
        "contents": {"type": "state", "account": DEST, "link": link},
    }


def node(monkeypatch, send=None, account=None, history=None, receives=None, fail=None):
    """Answer each RPC action from the arguments; `fail` maps an action to an exception."""
    asked = []
    fail = fail or {}

    def post_json(rpc_url, payload):
        action = payload["action"]
        asked.append(payload)
        assert rpc_url == A
        if action in fail:
            raise fail[action]
        if action == "blocks_info":
            hashes = payload["hashes"]
            if hashes == [SEND]:
                return {"blocks": {SEND: send}}
            return {"blocks": {h: (receives or {})[h] for h in hashes if h in (receives or {})}}
        if action == "account_info":
            return account
        if action == "account_history":
            return {"account": DEST, "history": history if history is not None else []}
        raise AssertionError(f"unexpected action {action}")

    monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)
    return asked


def read(send_hash=SEND, **kwargs):
    return nano_claim.claim_status(send_hash, A, now=lambda: NOW, **kwargs)


def test_a_received_send_reads_as_claimed_with_its_receive_hash(monkeypatch):
    node(monkeypatch, send=send_block(receivable="0"), account=opened(),
         history=[history_row()], receives={RECEIVE: receive_block()})
    status = read()
    assert status["outcome"] == "claimed"
    assert status["receive_hash"] == RECEIVE
    assert status["to_account_opened"] is True
    assert status["send_confirmed"] is True
    assert status["from"] == SENDER and status["to"] == DEST
    assert status["amount_raw"] == str(STARTER)
    assert status["read_at"] == NOW and status["source"] == A
    assert nano_claim.exit_code(status) == 0


def test_a_raw_history_row_with_a_link_is_matched_without_a_second_lookup(monkeypatch):
    row = {"type": "state", "subtype": "receive", "link": SEND, "hash": RECEIVE, "confirmed": "true"}
    asked = node(monkeypatch, send=send_block(receivable="0"), account=opened(), history=[row])
    assert read()["outcome"] == "claimed"
    assert [p["action"] for p in asked].count("blocks_info") == 1


def test_still_receivable_to_an_unopened_account(monkeypatch):
    node(monkeypatch, send=send_block(receivable="1"), account=NOT_OPENED)
    status = read()
    assert status["outcome"] == "receivable"
    assert status["to_account_opened"] is False
    assert status["receive_hash"] is None
    assert nano_claim.exit_code(status) == 0


def test_still_receivable_to_an_account_that_is_open(monkeypatch):
    node(monkeypatch, send=send_block(receivable="1"), account=opened())
    status = read()
    assert status["outcome"] == "receivable"
    assert status["to_account_opened"] is True
    assert status["receive_hash"] is None


def test_a_block_that_is_not_a_send_is_refused(monkeypatch):
    node(monkeypatch, send=send_block(subtype="receive"), account=opened())
    with pytest.raises(nano_claim.Refused) as caught:
        read()
    assert caught.value.reason == "not_a_send"


@pytest.mark.parametrize("bad", ["", "XYZ", SEND[:-1], SEND + "0", "G" * 64, None, 42])
def test_a_malformed_hash_is_refused_before_any_node_is_asked(monkeypatch, bad):
    asked = node(monkeypatch)
    with pytest.raises(nano_claim.Refused) as caught:
        read(bad)
    assert caught.value.reason == "malformed_hash"
    assert asked == []


def test_a_lowercase_hash_is_read_as_the_same_block(monkeypatch):
    node(monkeypatch, send=send_block(receivable="1"), account=NOT_OPENED)
    assert read(SEND.lower())["send_hash"] == SEND


@pytest.mark.parametrize("action", ["blocks_info", "account_info", "account_history"])
def test_a_node_that_cannot_be_read_is_unknown_never_clean(monkeypatch, action):
    node(monkeypatch, send=send_block(receivable="0"), account=opened(),
         history=[history_row()], receives={RECEIVE: receive_block()},
         fail={action: OSError("connection refused")})
    status = read()
    assert status["outcome"] == "unknown"
    assert "connection refused" in status["error"]
    assert nano_claim.exit_code(status) != 0


def test_a_node_error_reply_is_unknown(monkeypatch):
    node(monkeypatch, send=send_block(), account={"error": "Unsupported RPC Action"})
    status = read()
    assert status["outcome"] == "unknown"
    assert status["to_account_opened"] is None
    assert nano_claim.exit_code(status) != 0


def test_a_node_that_does_not_say_whether_it_is_receivable_is_unknown(monkeypatch):
    block = send_block()
    del block["receivable"], block["pending"]
    node(monkeypatch, send=block, account=opened())
    assert read()["outcome"] == "unknown"


def test_a_send_the_node_does_not_have_is_unknown_not_refused(monkeypatch):
    def post_json(rpc_url, payload):
        return {"error": "Block not found"}

    monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)
    status = read()
    assert status["outcome"] == "unknown"
    assert nano_claim.exit_code(status) != 0


def test_a_receive_not_found_within_the_bound_is_unknown(monkeypatch):
    rows = [history_row(block_hash=f"{i:064X}", account=OTHER) for i in range(5)]
    receives = {row["hash"]: receive_block(link="CD" * 32) for row in rows}
    node(monkeypatch, send=send_block(receivable="0"), account=opened(),
         history=rows, receives=receives)
    status = read(history_bound=5)
    assert status["outcome"] == "unknown"
    assert status["receive_hash"] is None
    assert "5" in status["error"]
    assert nano_claim.exit_code(status) != 0


def test_the_history_read_asks_for_no_more_than_the_bound(monkeypatch):
    asked = node(monkeypatch, send=send_block(receivable="0"), account=opened(),
                 history=[history_row()], receives={RECEIVE: receive_block()})
    read(history_bound=7)
    (history_call,) = [p for p in asked if p["action"] == "account_history"]
    assert history_call["count"] == "7"


def test_a_receive_that_is_not_yet_confirmed_is_not_claimed(monkeypatch):
    node(monkeypatch, send=send_block(receivable="0"), account=opened(),
         history=[history_row(confirmed="false")],
         receives={RECEIVE: receive_block(confirmed="false")})
    status = read()
    assert status["outcome"] == "unknown"
    assert status["receive_hash"] == RECEIVE


def test_not_receivable_but_the_account_is_unopened_is_unknown(monkeypatch):
    node(monkeypatch, send=send_block(receivable="0"), account=NOT_OPENED)
    assert read()["outcome"] == "unknown"


def test_an_unconfirmed_send_that_is_not_receivable_is_unknown(monkeypatch):
    node(monkeypatch, send=send_block(receivable="0", confirmed="false"), account=opened())
    status = read()
    assert status["outcome"] == "unknown"
    assert status["send_confirmed"] is False


def test_cli_prints_one_json_object_and_exits_nonzero_on_unknown(monkeypatch, capsys):
    node(monkeypatch, fail={"blocks_info": OSError("timed out")})
    code = nano_claim._main([SEND, A])
    out = capsys.readouterr().out
    status = json.loads(out)
    assert status["outcome"] == "unknown" and code != 0


def test_cli_refuses_a_malformed_hash_with_json_and_nonzero(monkeypatch, capsys):
    node(monkeypatch)
    code = nano_claim._main(["not-a-hash"])
    status = json.loads(capsys.readouterr().out)
    assert status["outcome"] == "refused" and status["reason"] == "malformed_hash"
    assert code != 0


def test_cli_prints_claimed_and_exits_zero(monkeypatch, capsys):
    node(monkeypatch, send=send_block(receivable="0"), account=opened(),
         history=[history_row()], receives={RECEIVE: receive_block()})
    assert nano_claim._main([SEND, A]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "claimed"
