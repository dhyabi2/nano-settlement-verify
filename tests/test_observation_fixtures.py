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
    "not_asked.json": "8a1e4e541b8929905c84d50b31ed5c60bb0141e4400b6b7e45a2bf0998e82c57",
    "asked_returned.json": "4ddec1182376b272079daed48c5cef7544aa7a0d528bf18642e7c12eb401162b",
    "asked_no_return.json": "67dfe938968ecdd48ce43bb96662842616f66b2b70c753760c3d73d81bd71157",
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
