"""five_field_verdict: five named fields, any missing -> unknown, never settled.

No test touches the network. The two real-chain replies below were read from
rpc.nano.to on 2026-10-14 and are pinned verbatim, so the local hash recompute
is checked against blocks the network actually hashed, not only against
blocks this file built.
"""

import copy
import hashlib
import json
import pathlib
import re

import pytest

import nano_settlement_verify
from nano_five_fields import (
    FIELDS,
    NOTE,
    block_hash_of,
    five_field_verdict,
    read_nodes,
)


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a socket")

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


# --- real blocks, as rpc.nano.to returned them ----------------------------------

REAL_LEGACY_SEND_HASH = "C7C2000B48DB2DEDD9FDB8583EE73C33897193A18D2870FC94A1D3115EA51C08"
REAL_LEGACY_SEND = {
    "block_account": "nano_13ezf4od79h1tgj9aiu4djzcmmguendtjfuhwfukhuucboua8cpoihmh8byo",
    "amount": "84428134000000000000000000000000",
    "balance": "1238933579573579623600346831066759150",
    "height": "873739",
    "local_timestamp": "1575913460",
    "successor": "C7764B37F04C74CF814B19C4DC6FFD53B5E999495C8E5C0D7EA1372A57E1E9C6",
    "confirmed": "true",
    "contents": {
        "type": "send",
        "previous": "4F823F653BAF34E2576C4FD68AA6C076B0F05C45B0B798FDB776BFEE06646D09",
        "destination": "nano_3zzfetd9yzztzus6nskm4xf651qpoq9wf1i4wkyc4dtmo9dp7n5qu46t4m35",
        "balance": "00EE9C243DD0305866FD94B74CFFFFEE",
        "work": "d34227f9b4d1c8cb",
        "signature": "EB2C27BA9BDEC33C03B43B694D2E69E2DDB420678BE34176020BEAF37019C684"
                     "56D84A04679F26CCC62EF4807A79609054DDE1C6E8CFF290C6EE6416D5307805",
        "balance_nano": "0",
    },
    "balance_nano": "1238933.57957357962360034683106675915",
    "amount_nano": "84.428134",
}

REAL_STATE_EPOCH_HASH = "023B94B7D27B311666C8636954FE17F1FD2EAA97A8BAC27DE5084FBBD5C6B02C"
REAL_STATE_EPOCH_CONTENTS = {
    "type": "state",
    "account": "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3",
    "previous": "6875C0DBFE5C44D8F8CFF431BC69ED5587C68F89F0663F2BC1FBBFCB46DC5989",
    "representative": "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3",
    "balance": "325586539664609129644855132177",
    "link": "65706F636820763220626C6F636B000000000000000000000000000000000000",
    "link_as_account": "nano_1sdifxjpia5p8ai86u5hefoi1111111111111111111111111111ngspq7ps",
}


def test_block_hash_of_recomputes_a_real_state_block():
    assert block_hash_of(REAL_STATE_EPOCH_CONTENTS) == REAL_STATE_EPOCH_HASH


def test_block_hash_of_recomputes_a_real_legacy_send():
    assert block_hash_of(REAL_LEGACY_SEND["contents"]) == REAL_LEGACY_SEND_HASH


def test_block_hash_of_refuses_what_it_cannot_hash():
    assert block_hash_of("opaque string") is None
    assert block_hash_of({"type": "open"}) is None
    assert block_hash_of({**REAL_STATE_EPOCH_CONTENTS, "balance": "1.5"}) is None


# --- a state send this file builds, hashed the way the network hashes -----------

_ALPHABET = "13456789abcdefghijkmnopqrstuwxyz"


def address(seed: int) -> str:
    key = hashlib.sha256(str(seed).encode()).digest()
    check = hashlib.blake2b(key, digest_size=5).digest()[::-1]

    def b32(value: int, chars: int) -> str:
        return "".join(_ALPHABET[(value >> (5 * i)) & 31] for i in reversed(range(chars)))

    return "nano_" + b32(int.from_bytes(key, "big"), 52) + b32(int.from_bytes(check, "big"), 8)


PAYER = address(1)
PAYEE = address(2)
OTHER = address(3)
PRICE = 10**24 + 4217          # a tagged amount: the price plus the invoice's tag
ISSUED = 1790000000            # the invoice's issue time, unix seconds
SEEN = ISSUED + 60             # when the node first saw the block
NODE_A = "https://rpc.nano.to"
NODE_B = "https://second-node.example/proxy"


def state_send(source=PAYER, destination=PAYEE, amount=PRICE, seen=SEEN, confirmed="true"):
    """(hash, block_info reply) for a state send whose hash really is its contents' hash."""
    contents = {
        "type": "state",
        "account": source,
        "previous": "AB" * 32,
        "representative": OTHER,
        "balance": str(5 * 10**30),
        "link": nano_settlement_verify.public_key_from_address(destination).hex().upper(),
        "link_as_account": destination,
    }
    reply = {
        "block_account": source,
        "amount": str(amount),
        "confirmed": confirmed,
        "subtype": "send",
        "height": "7",
        "local_timestamp": str(seen),
        "contents": contents,
    }
    return block_hash_of(contents), reply


def verdict(block_hash, reads, **terms):
    base = dict(expect_raw=PRICE, invoice_id="inv-0042", issued_at=ISSUED,
                payee=PAYEE, exclude=())
    base.update(terms)
    return five_field_verdict(block_hash, reads, **base)


def two_nodes(reply_a, reply_b=None):
    return [(NODE_A, reply_a), (NODE_B, copy.deepcopy(reply_b or reply_a))]


# --- all present -> settled -----------------------------------------------------


def test_all_five_present_and_true_is_settled_and_says_it_is_not_acceptance():
    block_hash, reply = state_send()

    out = verdict(block_hash, two_nodes(reply))

    assert out["verdict"] == "settled"
    assert out["missing"] == []
    assert list(out["fields"]) == list(FIELDS)
    assert all(f["status"] == "ok" for f in out["fields"].values())
    assert out["note"] == NOTE == "settled means work may start; it is not buyer acceptance"
    assert out["fields"]["nodes_read"]["read"] == [NODE_A, NODE_B]
    assert out["fields"]["block_hash_confirmed"]["hash_recomputed_locally"] is True


def test_a_real_legacy_send_read_on_two_nodes_settles_against_its_own_terms():
    out = five_field_verdict(
        REAL_LEGACY_SEND_HASH, two_nodes(REAL_LEGACY_SEND),
        expect_raw=84428134000000000000000000000000, invoice_id="inv-real",
        issued_at=1575913000, payee=REAL_LEGACY_SEND["contents"]["destination"], exclude=(),
    )
    assert out["verdict"] == "settled"


# --- each field missing -> unknown ----------------------------------------------


def test_unconfirmed_block_leaves_block_hash_confirmed_missing():
    block_hash, reply = state_send(confirmed="false")
    out = verdict(block_hash, two_nodes(reply))
    assert out["verdict"] == "unknown"
    assert out["missing"] == ["block_hash_confirmed"]


def test_confirmed_on_one_node_only_is_still_missing():
    block_hash, reply = state_send()
    _, pending = state_send(confirmed="false")
    out = verdict(block_hash, two_nodes(reply, pending))
    assert out["verdict"] == "unknown"
    assert "block_hash_confirmed" in out["missing"]


def test_malformed_block_hash_is_unknown():
    _, reply = state_send()
    out = verdict("not-a-hash", two_nodes(reply))
    assert out["verdict"] == "unknown"
    assert "block_hash_confirmed" in out["missing"]


def test_contents_that_do_not_hash_to_the_hash_asked_are_not_a_read():
    block_hash, reply = state_send()
    _, other = state_send(destination=OTHER)
    out = verdict(block_hash, two_nodes(other))
    assert out["verdict"] == "unknown"
    assert out["missing"] == list(FIELDS)
    assert "hash to" in out["fields"]["nodes_read"]["failed"][NODE_A]


def test_no_exclusion_list_leaves_source_field_missing():
    block_hash, reply = state_send()
    out = verdict(block_hash, two_nodes(reply), exclude=None)
    assert out["verdict"] == "unknown"
    assert out["missing"] == ["source_not_destination_not_excluded"]


def test_an_exclusion_entry_that_is_not_an_account_is_missing_not_ignored():
    block_hash, reply = state_send()
    out = verdict(block_hash, two_nodes(reply), exclude=["nano_typo"])
    assert out["verdict"] == "unknown"
    assert out["missing"] == ["source_not_destination_not_excluded"]


def test_no_quoted_amount_leaves_exact_amount_missing():
    block_hash, reply = state_send()
    for bad in (None, 0, float(PRICE), True, str(PRICE)):
        out = verdict(block_hash, two_nodes(reply), expect_raw=bad)
        assert out["verdict"] == "unknown", bad
        assert out["missing"] == ["exact_amount"], bad


def test_no_invoice_id_leaves_invoice_field_missing():
    block_hash, reply = state_send()
    for bad in (None, "", "   "):
        out = verdict(block_hash, two_nodes(reply), invoice_id=bad)
        assert out["verdict"] == "unknown"
        assert out["missing"] == ["invoice_issued_before_block"]


def test_no_issue_time_leaves_invoice_field_missing():
    block_hash, reply = state_send()
    for bad in (None, "yesterday", "2026-10-13T12:00:00"):  # last one: no UTC offset
        out = verdict(block_hash, two_nodes(reply), issued_at=bad)
        assert out["verdict"] == "unknown", bad
        assert out["missing"] == ["invoice_issued_before_block"], bad


def test_no_block_timestamp_leaves_invoice_field_missing():
    block_hash, reply = state_send(seen=0)
    out = verdict(block_hash, two_nodes(reply))
    assert out["verdict"] == "unknown"
    assert out["missing"] == ["invoice_issued_before_block"]


def test_one_node_read_leaves_nodes_read_missing():
    block_hash, reply = state_send()
    out = verdict(block_hash, [(NODE_A, reply)])
    assert out["verdict"] == "unknown"
    assert out["missing"] == ["nodes_read"]


def test_the_same_node_twice_is_one_witness():
    block_hash, reply = state_send()
    out = verdict(block_hash, [(NODE_A, reply), (NODE_A + "/", copy.deepcopy(reply))])
    assert out["verdict"] == "unknown"
    assert out["missing"] == ["nodes_read"]


def test_a_failed_node_is_named_and_leaves_nodes_read_missing():
    block_hash, reply = state_send()
    out = verdict(block_hash, [(NODE_A, reply), (NODE_B, OSError("refused"))])
    assert out["verdict"] == "unknown"
    assert out["missing"] == ["nodes_read"]
    assert "refused" in out["fields"]["nodes_read"]["failed"][NODE_B]


def test_no_reads_at_all_is_unknown_with_every_field_missing():
    block_hash, _ = state_send()
    out = verdict(block_hash, [])
    assert out["verdict"] == "unknown"
    assert out["missing"] == list(FIELDS)


# --- nodes disagreeing -> unknown -----------------------------------------------


def test_nodes_disagreeing_on_the_amount_is_unknown_not_refused():
    block_hash, reply = state_send()
    lying = copy.deepcopy(reply)
    lying["amount"] = str(PRICE + 1)  # amount is node-asserted; the hash does not cover it
    out = verdict(block_hash, two_nodes(reply, lying))
    assert out["verdict"] == "unknown"
    assert "nodes_read" in out["missing"]
    assert "disagree" in out["fields"]["nodes_read"]["why"]
    assert out["fields"]["exact_amount"]["status"] == "missing"


# --- read, and wrong -> refused -------------------------------------------------


def test_overpayment_is_refused_not_settled():
    block_hash, reply = state_send(amount=PRICE + 1)
    out = verdict(block_hash, two_nodes(reply))
    assert out["verdict"] == "refused"
    assert out["missing"] == []
    assert out["fields"]["exact_amount"]["status"] == "fail"
    assert out["fields"]["exact_amount"]["why"].startswith("over")


def test_underpayment_is_refused():
    block_hash, reply = state_send(amount=PRICE - 1)
    out = verdict(block_hash, two_nodes(reply))
    assert out["verdict"] == "refused"
    assert out["fields"]["exact_amount"]["why"].startswith("under")


def test_invoice_issued_after_the_block_is_refused():
    block_hash, reply = state_send(seen=ISSUED - 1)
    out = verdict(block_hash, two_nodes(reply))
    assert out["verdict"] == "refused"
    assert out["fields"]["invoice_issued_before_block"]["status"] == "fail"


def test_invoice_issued_at_the_block_second_is_not_before_it():
    block_hash, reply = state_send(seen=ISSUED)
    assert verdict(block_hash, two_nodes(reply))["verdict"] == "refused"


def test_issue_time_is_compared_with_the_earliest_node_time():
    block_hash, reply = state_send(seen=ISSUED + 5)
    early = copy.deepcopy(reply)
    early["local_timestamp"] = str(ISSUED - 5)
    out = verdict(block_hash, two_nodes(reply, early))
    assert out["verdict"] == "refused"


def test_iso_issue_time_is_accepted():
    block_hash, reply = state_send()
    out = verdict(block_hash, two_nodes(reply), issued_at="2026-09-21T14:13:20Z")
    assert out["fields"]["invoice_issued_before_block"]["issued_at"] == ISSUED
    assert out["verdict"] == "settled"
    late = verdict(block_hash, two_nodes(reply), issued_at="2026-09-21T14:14:20Z")
    assert late["verdict"] == "refused"  # exactly SEEN: not before the block


def test_source_on_the_exclusion_list_is_refused():
    block_hash, reply = state_send()
    out = verdict(block_hash, two_nodes(reply), exclude=[OTHER, PAYER.replace("nano_", "xrb_")])
    assert out["verdict"] == "refused"
    assert out["fields"]["source_not_destination_not_excluded"]["why"] == \
        "source is on the exclusion list"


def test_source_that_is_the_destination_is_refused():
    block_hash, reply = state_send(source=PAYEE, destination=PAYEE)
    out = verdict(block_hash, two_nodes(reply))
    assert out["verdict"] == "refused"
    assert out["fields"]["source_not_destination_not_excluded"]["why"] == \
        "source is the destination"


def test_payment_to_another_account_is_refused():
    block_hash, reply = state_send(destination=OTHER)
    out = verdict(block_hash, two_nodes(reply))
    assert out["verdict"] == "refused"
    assert out["fields"]["source_not_destination_not_excluded"]["why"] == \
        "destination is not the payee"


def test_a_missing_field_outranks_a_failing_one():
    """Any missing field is unknown, as the agent's rule says - even beside a wrong amount."""
    block_hash, reply = state_send(amount=PRICE + 1)
    out = verdict(block_hash, two_nodes(reply), invoice_id=None)
    assert out["verdict"] == "unknown"
    assert out["missing"] == ["invoice_issued_before_block"]
    assert out["fields"]["exact_amount"]["status"] == "fail"


# --- read_nodes: injectable fetch, no network -----------------------------------


def test_read_nodes_asks_each_node_block_info_and_keeps_failures():
    block_hash, reply = state_send()
    asked = []

    def fetch(url, payload):
        asked.append((url, payload))
        if url == NODE_B:
            raise OSError("connection refused")
        return reply

    reads = read_nodes(block_hash, [NODE_A, NODE_B], fetch=fetch)

    assert asked == [(u, {"action": "block_info", "json_block": "true", "hash": block_hash})
                     for u in (NODE_A, NODE_B)]
    assert reads[0] == (NODE_A, reply)
    assert isinstance(reads[1][1], OSError)
    assert verdict(block_hash, reads)["missing"] == ["nodes_read"]


def test_read_nodes_refuses_one_url_string():
    with pytest.raises(TypeError):
        read_nodes("AB" * 32, NODE_A, fetch=lambda u, p: {})


def test_reads_may_be_a_mapping():
    block_hash, reply = state_send()
    out = verdict(block_hash, {NODE_A: reply, NODE_B: copy.deepcopy(reply)})
    assert out["verdict"] == "settled"


# --- FIVE_FIELDS.md asks what read_nodes asks, and runs nothing of ours ---------

DOC = pathlib.Path(__file__).resolve().parent.parent / "FIVE_FIELDS.md"


def test_doc_curl_bodies_are_the_payload_read_nodes_sends():
    text = DOC.read_text(encoding="utf-8")
    bodies = re.findall(r"-d '(\{.*?\})'", text, re.S)
    assert len(bodies) == 2, "FIVE_FIELDS.md should show exactly two curl calls"
    asked = []
    read_nodes("<HASH>", ["a", "b"], fetch=lambda u, p: asked.append(p) or {})
    for body in bodies:
        assert json.loads(body) == asked[0]


def test_doc_pipes_nothing_into_an_interpreter():
    text = DOC.read_text(encoding="utf-8")
    assert not re.search(r"\|\s*(sudo\s+)?(python\d?|sh|bash|zsh|node|perl|ruby)\b", text)
    for name in FIELDS:
        assert f"`{name}`" in text
    assert "It is not buyer acceptance" in text


# --- the destination is the hashed link, not the node's rendering of it ---------


def test_link_as_account_naming_the_payee_cannot_redirect_a_hashed_link():
    # The hashed block pays OTHER; the node's link_as_account claims the payee.
    block_hash, reply = state_send(destination=OTHER)
    reply["contents"]["link_as_account"] = PAYEE
    assert block_hash_of(reply["contents"]) == block_hash
    out = verdict(block_hash, two_nodes(reply))
    assert out["verdict"] != "settled"
    assert "not the account the hashed contents pay" in out["fields"]["nodes_read"]["failed"][NODE_A]


def test_a_reply_level_link_as_account_is_no_destination_either():
    block_hash, reply = state_send(destination=OTHER)
    del reply["contents"]["link_as_account"]
    reply["link_as_account"] = PAYEE
    assert verdict(block_hash, two_nodes(reply))["verdict"] != "settled"
    # With no rendering at all, the destination is read from the hashed link: OTHER.
    del reply["link_as_account"]
    out = verdict(block_hash, two_nodes(reply))
    assert out["verdict"] == "refused"
    assert out["fields"]["source_not_destination_not_excluded"]["destination"] == OTHER


def test_a_legacy_send_destination_is_contents_destination_not_link_as_account():
    reply = copy.deepcopy(REAL_LEGACY_SEND)
    reply["contents"]["link_as_account"] = PAYEE
    assert block_hash_of(reply["contents"]) == REAL_LEGACY_SEND_HASH
    out = verdict(REAL_LEGACY_SEND_HASH, two_nodes(reply),
                  expect_raw=int(reply["amount"]), issued_at=1575913000)
    assert out["verdict"] != "settled"


def test_a_state_destination_is_link_even_when_link_as_account_is_absent():
    block_hash, reply = state_send()
    del reply["contents"]["link_as_account"]
    out = verdict(block_hash, two_nodes(reply))
    assert out["verdict"] == "settled"
    assert out["fields"]["source_not_destination_not_excluded"]["destination"] == PAYEE


# --- an exclusion list given as an iterator is read once, before the check ------


def test_a_generator_exclusion_list_still_excludes():
    block_hash, reply = state_send()
    for exclude in ((w for w in [PAYER]), iter([PAYER]), map(str.strip, [PAYER + "\n"])):
        out = verdict(block_hash, two_nodes(reply), exclude=exclude)
        assert out["verdict"] == "refused"
        assert out["fields"]["source_not_destination_not_excluded"]["why"] == \
            "source is on the exclusion list"


def test_a_generator_with_a_bad_entry_is_still_missing():
    block_hash, reply = state_send()
    out = verdict(block_hash, two_nodes(reply), exclude=(w for w in ["nano_typo"]))
    assert out["missing"] == ["source_not_destination_not_excluded"]


# --- a legacy send's source is the node's word ----------------------------------


def test_a_legacy_send_source_cannot_clear_a_non_empty_exclusion_list():
    reply = copy.deepcopy(REAL_LEGACY_SEND)
    real_source = reply["block_account"]
    reply["block_account"] = OTHER  # the hash does not cover this; a node can say anything
    out = verdict(REAL_LEGACY_SEND_HASH, two_nodes(reply),
                  expect_raw=int(reply["amount"]), issued_at=1575913000,
                  payee=reply["contents"]["destination"], exclude=[real_source])
    assert out["verdict"] == "unknown"
    assert out["missing"] == ["source_not_destination_not_excluded"]
    field = out["fields"]["source_not_destination_not_excluded"]
    assert field["source_covered_by_hash"] is False
    assert "node's word" in field["why"]


# --- min_nodes below two is no quorum -------------------------------------------


def test_min_nodes_below_two_or_not_an_int_leaves_nodes_read_missing():
    block_hash, reply = state_send()
    for bad in (1, 0, -3, True, False, 2.0, "2", None):
        out = verdict(block_hash, [(NODE_A, reply)], min_nodes=bad)
        assert out["verdict"] == "unknown", bad
        assert "nodes_read" in out["missing"], bad
        out = verdict(block_hash, two_nodes(reply), min_nodes=bad)
        assert out["verdict"] == "unknown", bad
    assert verdict(block_hash, two_nodes(reply), min_nodes=2)["verdict"] == "settled"


# --- the node-asserted fields are named where a by-eye checker reads them ---------


def test_doc_and_docstring_name_subtype_and_legacy_block_account_as_node_asserted():
    import nano_five_fields
    text = DOC.read_text(encoding="utf-8")
    tail = text[text.index("What the hash does not cover"):]
    doc = nano_five_fields.__doc__
    asserted = doc[doc.index("What a node still asserts"):]
    for where in (tail, asserted):
        assert "`subtype`" in where
        assert "`block_account`" in where
        assert "legacy send" in where or "older send" in where
    steps = text[text.index("## What to compare, by eye"):text.index("## The local hash")]
    assert "`contents.link`" in steps
    assert "public key" in steps
