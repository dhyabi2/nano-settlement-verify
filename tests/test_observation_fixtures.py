"""Law: three per-node records round-trip byte for byte, and stay three.

A fixed fixture set under tests/fixtures/observations/ - one node that was never
asked, one asked node whose answer came back with its time, one asked node that
returned nothing - is replayed through the serializer: read the bytes, parse,
rebuild the `Observation`, write it again with `canonical_json`. The output must be
the same bytes as the file, and the three files must be three different records.

The files' SHA-256 are pinned here, so anyone can run

    sha256sum tests/fixtures/observations/*.json

on their own checkout and diff the result against these values.
"""

import hashlib
import json
import pathlib

import pytest

import nano_quorum
from nano_quorum import Observation, canonical_json

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "observations"

PINNED = {
    # Schema /5: each entry carries its "ask_index". (The /4 files, without it,
    # were 8a1e4e54..., 4ddec118... and 67dfe938... respectively.)
    "not_asked.json": "cc76895d3aefe7099ddfc942207de3dc42a5819f79220aa3306a7c0cd083b65c",
    "asked_returned.json": "fff2b1d2128c6c47b3061b439a4b8ce5f3c4d874ebf28c121be15623a20cb22b",
    "asked_no_return.json": "ac478d1cbe87ae20ecdf0429eb22d820a56358d93f25280354639588d83f9320",
}


def serialize(seen: Observation) -> bytes:
    return (canonical_json(seen.to_dict()) + "\n").encode("utf-8")


@pytest.mark.parametrize("name", sorted(PINNED))
def test_a_fixture_round_trips_byte_for_byte(name):
    raw = (FIXTURES / name).read_bytes()
    seen = Observation.from_dict(json.loads(raw.decode("utf-8")))
    assert serialize(seen) == raw
    # And once more from what was written: serialize -> parse -> serialize is stable.
    again = Observation.from_dict(json.loads(serialize(seen).decode("utf-8")))
    assert again == seen
    assert serialize(again) == raw


@pytest.mark.parametrize("name", sorted(PINNED))
def test_a_fixture_has_the_pinned_sha256(name):
    assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == PINNED[name]


def test_the_three_fixtures_are_three_distinct_records():
    """Not asked, asked with a return time, asked with none: three records that
    stay three after the round trip, and differ exactly where they should."""
    files = sorted(FIXTURES.glob("*.json"))
    assert sorted(path.name for path in files) == sorted(PINNED)
    out = {
        path.name: serialize(Observation.from_dict(json.loads(path.read_bytes().decode("utf-8"))))
        for path in files
    }
    assert len(set(out.values())) == 3
    parsed = {name: json.loads(text) for name, text in out.items()}
    times = {
        name: (entry["state"], entry["attempted_at"] is None, entry["returned_at"] is None)
        for name, entry in parsed.items()
    }
    assert times == {
        "not_asked.json": ("not-asked", True, True),
        "asked_returned.json": ("not-seen", False, False),
        "asked_no_return.json": ("unreadable", False, True),
    }
    assert parsed["not_asked.json"]["read_at"] is None
    # A not-asked node keeps its planned place in the ask order; it is not null.
    assert {name: entry["ask_index"] for name, entry in parsed.items()} == {
        "asked_returned.json": 0, "asked_no_return.json": 1, "not_asked.json": 2,
    }
    assert parsed["asked_returned.json"]["returned_at"] == parsed["asked_returned.json"]["read_at"]
    assert parsed["asked_no_return.json"]["read_at"] is not None


def test_from_dict_reads_back_what_a_live_check_wrote(monkeypatch):
    """Every observation of a real record (unreadable, not-seen, confirmed,
    not-asked) reads back to the same entry."""
    import nano_settlement_verify

    replies = {
        "http://127.0.0.1:7071": OSError("timed out"),
        "http://127.0.0.1:7072": {"error": "Block not found"},
    }

    def post_json(rpc_url, payload):
        reply = replies[rpc_url]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(nano_settlement_verify, "post_json", post_json)
    with pytest.raises(nano_quorum.NotCorroborated) as caught:
        nano_quorum.verify_corroborated(
            "B2EC1E2A1F2C3D4E5F60718293A4B5C6D7E8F9012345678901234567890ABCDE",
            10**30, "nano_1payee", list(replies),
        )
    for seen in caught.value.observations:
        entry = json.loads(json.dumps(seen.to_dict()))
        assert Observation.from_dict(entry).to_dict() == entry


@pytest.mark.parametrize("edit", ["unknown key", "missing key", "unreadable without error", "number time"])
def test_from_dict_refuses_an_entry_to_dict_would_not_write(edit):
    entry = json.loads((FIXTURES / "asked_no_return.json").read_text(encoding="utf-8"))
    if edit == "unknown key":
        entry["extra"] = 1
    elif edit == "missing key":
        del entry["attempted_at"]
    elif edit == "unreadable without error":
        del entry["error"]
    else:
        entry["read_at"] = 1762689606
    with pytest.raises(ValueError):
        Observation.from_dict(entry)
