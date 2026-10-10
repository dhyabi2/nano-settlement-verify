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


# --- unknown is a state with a way out, and not-found is scoped ------------------
#
# An outside agent put the condition: `unknown` must not be a polite word for
# "wait forever" - it must say do not re-send, and how to look again under the
# same identity, and when to stop and escalate. And absence on one node at one
# instant is not proof a send never happened, so every not-found says where and
# when it was read.

B = "http://127.0.0.1:7072"
LATER = "2026-10-08T12:05:00Z"


def nodes(monkeypatch, per_url):
    """Several stub nodes: `per_url` maps a URL to node() keyword arguments."""
    asked = []

    def post_json(rpc_url, payload):
        asked.append((rpc_url, payload["action"]))
        spec = per_url[rpc_url]
        if spec == "not found":
            return {"error": "Block not found"}
        fail = spec.get("fail", {})
        action = payload["action"]
        if action in fail:
            raise fail[action]
        if action == "blocks_info":
            hashes = payload["hashes"]
            if hashes == [SEND]:
                return {"blocks": {SEND: spec["send"]}}
            receives = spec.get("receives") or {}
            return {"blocks": {h: receives[h] for h in hashes if h in receives}}
        if action == "account_info":
            return spec["account"]
        if action == "account_history":
            return {"account": DEST, "history": spec.get("history", [])}
        raise AssertionError(f"unexpected action {action}")

    monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)
    return asked


CLAIMED_NODE = {"send": send_block(receivable="0"), "account": opened(),
                "history": [history_row()], "receives": {RECEIVE: receive_block()}}


def test_unknown_carries_a_reconcile_path_under_the_same_operation_id(monkeypatch):
    node(monkeypatch, fail={"blocks_info": OSError("timed out")})
    status = read()
    rec = status["reconcile"]
    assert rec["operation_id"] == SEND
    assert rec["resubmit"] is False
    assert rec["nodes_checked"] == [A]
    assert rec["checked_at"] == NOW
    assert rec["retry_after_s"] == nano_claim.RETRY_AFTER_S
    assert rec["next_check_at"] == LATER
    assert rec["attempt"] == 1
    assert rec["escalate"] is False and rec["action"] == "recheck"
    assert rec["escalate_after"] == {
        "unknown_reads": nano_claim.ESCALATE_AFTER_READS,
        "hours": nano_claim.ESCALATE_AFTER_HOURS,
        "first_unknown_at": NOW,
        "deadline": "2026-10-09T12:00:00Z",
    }


def test_an_answered_read_carries_no_reconcile_and_no_absence(monkeypatch):
    node(monkeypatch, **CLAIMED_NODE)
    status = read()
    assert status["outcome"] == "claimed"
    assert status["reconcile"] is None and status["absence_scope"] is None


def test_unknown_escalates_after_the_read_limit(monkeypatch):
    node(monkeypatch, fail={"blocks_info": OSError("timed out")})
    status = read(attempt=nano_claim.ESCALATE_AFTER_READS)
    rec = status["reconcile"]
    assert rec["escalate"] is True and rec["action"] == "escalate"
    assert rec["next_check_at"] is None
    assert rec["resubmit"] is False
    assert nano_claim.exit_code(status) == 2


def test_unknown_escalates_after_the_time_limit(monkeypatch):
    node(monkeypatch, fail={"blocks_info": OSError("timed out")})
    rec = read(attempt=2, first_unknown_at="2026-10-07T11:00:00Z")["reconcile"]
    assert rec["escalate"] is True and rec["action"] == "escalate"
    assert rec["escalate_after"]["deadline"] == "2026-10-08T11:00:00Z"


def test_reconcile_limits_are_settable(monkeypatch):
    node(monkeypatch, fail={"blocks_info": OSError("timed out")})
    rec = read(attempt=2, retry_after_s=30, escalate_after_reads=3,
               escalate_after_hours=1)["reconcile"]
    assert rec["retry_after_s"] == 30
    assert rec["next_check_at"] == "2026-10-08T12:00:30Z"
    assert rec["escalate_after"]["unknown_reads"] == 3
    assert rec["escalate_after"]["deadline"] == "2026-10-08T13:00:00Z"
    assert rec["escalate"] is False


def test_a_send_not_found_is_scoped_never_claimed_absent(monkeypatch):
    nodes(monkeypatch, {A: "not found"})
    status = read()
    assert status["outcome"] == "unknown"
    scope = status["absence_scope"]
    assert scope["not_found"] == "send block"
    assert scope["nodes"] == [A] and scope["at"] == NOW
    assert scope["proves_absence"] is False
    assert "ledger" in scope["window"]


def test_a_receive_not_found_within_the_bound_is_scoped_to_the_bound(monkeypatch):
    rows = [history_row(block_hash=f"{i:064X}", account=OTHER) for i in range(5)]
    receives = {row["hash"]: receive_block(link="CD" * 32) for row in rows}
    node(monkeypatch, send=send_block(receivable="0"), account=opened(),
         history=rows, receives=receives)
    scope = read(history_bound=5)["absence_scope"]
    assert scope["not_found"] == "receive linking this send"
    assert "newest 5" in scope["window"] and DEST in scope["window"]
    assert scope["proves_absence"] is False


def test_an_unopened_destination_is_scoped_too(monkeypatch):
    node(monkeypatch, send=send_block(receivable="1"), account=NOT_OPENED)
    status = read()
    assert status["outcome"] == "receivable"
    scope = status["absence_scope"]
    assert scope["not_found"] == "destination account"
    assert scope["nodes"] == [A] and scope["proves_absence"] is False


def test_not_found_on_one_node_and_found_on_another_is_the_found_answer(monkeypatch):
    nodes(monkeypatch, {A: "not found", B: CLAIMED_NODE})
    status = nano_claim.claim_status(SEND, [A, B], now=lambda: NOW)
    assert status["outcome"] == "claimed"
    assert status["source"] == B
    assert status["receive_hash"] == RECEIVE
    assert nano_claim.exit_code(status) == 0


def test_a_failing_node_then_a_receivable_one_is_receivable(monkeypatch):
    nodes(monkeypatch, {A: {"fail": {"blocks_info": OSError("refused")}},
                        B: {"send": send_block(receivable="1"), "account": opened()}})
    status = nano_claim.claim_status(SEND, [A, B], now=lambda: NOW)
    assert status["outcome"] == "receivable" and status["source"] == B


def test_not_found_on_every_node_stays_unknown_scoped_to_all_of_them(monkeypatch):
    nodes(monkeypatch, {A: "not found", B: "not found"})
    status = nano_claim.claim_status(SEND, [A, B], now=lambda: NOW)
    assert status["outcome"] == "unknown"
    assert status["reconcile"]["nodes_checked"] == [A, B]
    assert status["absence_scope"]["nodes"] == [A, B]
    assert A in status["error"] and B in status["error"]
    assert nano_claim.exit_code(status) == 2


def test_cli_takes_nodes_and_reconcile_flags(monkeypatch, capsys):
    nodes(monkeypatch, {A: "not found", B: "not found"})
    code = nano_claim._main([SEND, "--nodes", A, B, "--retry-after", "30",
                             "--escalate-reads", "3", "--attempt", "3"])
    status = json.loads(capsys.readouterr().out)
    assert code == 2
    assert status["reconcile"]["nodes_checked"] == [A, B]
    assert status["reconcile"]["retry_after_s"] == 30
    assert status["reconcile"]["escalate"] is True


def test_cli_usage_error_is_not_confused_with_unknown(monkeypatch, capsys):
    node(monkeypatch)
    assert nano_claim._main([SEND, "--attempt", "zero"]) == 64
    assert nano_claim._main([]) == 64


# --- node order must not decide the verdict ----------------------------------

B = "http://127.0.0.1:7072"


def two_nodes(monkeypatch, per_url):
    """Answer each action per endpoint. `per_url` maps a URL to a `node`-style dict."""
    asked = []

    def post_json(rpc_url, payload):
        action = payload["action"]
        asked.append((rpc_url, action))
        spec = per_url[rpc_url]
        if action == "blocks_info":
            hashes = payload["hashes"]
            if hashes == [SEND]:
                return {"blocks": {SEND: spec["send"]}}
            return {"blocks": {h: spec.get("receives", {})[h]
                               for h in hashes if h in spec.get("receives", {})}}
        if action == "account_info":
            return spec["account"]
        if action == "account_history":
            return {"account": DEST, "history": spec.get("history", [])}
        raise AssertionError(f"unexpected action {action}")

    monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)
    return asked


def _honest():
    return {"send": send_block(receivable="0"), "account": opened(),
            "history": [history_row()], "receives": {RECEIVE: receive_block()}}


def _proxy():
    """An endpoint that answers 200 but drops `subtype`, so the block names no
    operation. `nano_settlement_verify._operation` reads "" - which is this
    endpoint being unreadable, not a fact about the block."""
    send = send_block(receivable="0")
    del send["subtype"]
    return {"send": send, "account": opened(),
            "history": [history_row()], "receives": {RECEIVE: receive_block()}}


def test_a_proxy_listed_first_does_not_turn_a_claimed_send_into_a_refusal(monkeypatch):
    """`Refused` is terminal - no reconcile path, no absence_scope, no retry -
    and it is re-raised out of the node loop, so the honest endpoint after the
    proxy was never asked. The same two nodes in the other order read `claimed`.
    A seller that WAS paid concluded from exit 3 that its hash is not a send.
    """
    asked = two_nodes(monkeypatch, {A: _proxy(), B: _honest()})

    status = nano_claim.claim_status(SEND, [A, B], now=lambda: NOW)

    assert status["outcome"] == "claimed"
    assert status["receive_hash"] == RECEIVE
    assert nano_claim.exit_code(status) == 0
    assert B in {url for url, _ in asked}, "the honest endpoint was asked"


def test_the_same_two_nodes_read_the_same_either_way_round(monkeypatch):
    first = two_nodes(monkeypatch, {A: _honest(), B: _proxy()})
    forwards = nano_claim.claim_status(SEND, [A, B], now=lambda: NOW)
    assert first  # the stub was used
    two_nodes(monkeypatch, {A: _proxy(), B: _honest()})
    backwards = nano_claim.claim_status(SEND, [A, B], now=lambda: NOW)

    assert forwards["outcome"] == backwards["outcome"] == "claimed"


def test_a_lone_proxy_is_unknown_with_a_reconcile_path_not_refused(monkeypatch):
    """One endpoint, unreadable: "we could not look" (exit 2, retry), which is
    what the module docstring says an unreadable reply must be."""
    two_nodes(monkeypatch, {A: _proxy()})

    status = nano_claim.claim_status(SEND, [A], now=lambda: NOW)

    assert status["outcome"] == "unknown"
    assert "names no operation" in status["error"]
    assert status["reconcile"]
    assert nano_claim.exit_code(status) == 2
