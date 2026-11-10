"""Believe a Nano payment only when more than one node says the same thing.

`nano_settlement_verify.verify` asks one node. That is the right default - it is
one call on a seller's request path - but it means the receipt is exactly as
good as that one endpoint. Public Nano RPC is thin: `rpc.nano.to` wants a key
and rate-limits a burst, Nanswap throttles, and a proxy in front of any of them
can answer 200 with something that is not a node reply at all. An agent with no
node of its own has no second opinion to fall back on, and "the node said so"
is the whole evidence that money arrived.

This module is that second opinion. It asks several endpoints and hands back a
receipt only for what they agree on.

Three things it deliberately does NOT do:

- **It does not break ties.** Two nodes that both answer and contradict each
  other about a confirmed block mean one of them is wrong, and nothing here can
  tell you which. A third vote would only make a guess look like a quorum. A
  contradiction raises.
- **It does not retry.** A failed endpoint is skipped, not re-asked. Waiting is
  the caller's to decide, as it is for `verify`.
- **It does not send, sign, broadcast or hold a key.** `process` is not here.
  This reads, like the rest of the package.

Amounts stay integers of raw throughout, as everywhere else in this package:
1 XNO is 10**30 raw and a float loses the low digits.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone

import nano_settlement_verify
from nano_settlement_verify import Mismatch, NotFound, Receipt

__all__ = [
    "AGREE_DEFAULT",
    "Corroboration",
    "Disagreement",
    "NotCorroborated",
    "NODE_STATES",
    "NODE_STATES_SHA256",
    "ORDER_RULES",
    "Observation",
    "RECEIPT_SCHEMA",
    "STOP_REASONS",
    "STOP_REASONS_SHA256",
    "Stop",
    "balance_corroborated",
    "canonical_json",
    "distinct_endpoints",
    "node_states_sha256",
    "post_json_failover",
    "redact_url",
    "replay_receipt",
    "verify_corroborated",
    "verify_receipt_vocabulary",
]

# How many endpoints must say the same thing before it is believed. Two is the
# smallest number that is more than one witness; it is a default, not a law.
AGREE_DEFAULT = 2


class NotCorroborated(Exception):
    """Too few endpoints answered to decide anything. NOTHING was checked.

    This is the multi-endpoint form of `verify`'s transport errors, and it means
    the same thing: not "the payment is bad" but "we could not look". A seller
    that refuses a call on this has refused a call the buyer may well have paid
    for, and XNO does not come back. Hold the call and ask again.
    """

    def __init__(self, answered: int, agree: int, failures: dict, observations=()) -> None:
        detail = "; ".join(f"{url}: {why}" for url, why in failures.items())
        super().__init__(
            f"{answered} of {answered + len(failures)} endpoints answered, "
            f"{agree} needed to agree ({detail})"
        )
        self.answered = answered
        self.agree = agree
        self.failures = dict(failures)
        # One `Observation` per endpoint asked, in the order asked. Empty when
        # the caller is not `verify_corroborated` (failover, balances).
        self.observations = tuple(observations)
        # The `Stop` ("exhausted") when raised by `verify_corroborated`, else None,
        # and when to ask again: `stop.at` plus the call's `retry_after`.
        self.stop = None
        self.not_before = None


class Disagreement(Exception):
    """Endpoints that both answered gave answers that cannot both be true.

    A Nano block's contents are fixed by its hash, so two honest nodes that have
    both confirmed it report the same amount, height and payee. Different
    answers mean one endpoint is broken or lying. Which one is not knowable from
    here, so nothing is served.

    Both sides are kept, not just the refusal. `answers` is exactly what it
    always was: the first endpoint that settled on an answer and the endpoint
    that contradicted it, each with its answer as text, as the message prints
    them. That holds for any `agree`; with `agree >= 3` the endpoints that
    agreed with the first one are not added to it.

    Everything else is additive. `observations` maps every endpoint that
    answered before the contradiction, and the one that contradicted, to the
    whole thing it returned - the `Receipt` a `Corroboration` would have
    carried, the `(balance_raw, receivable_raw)` figure, the payer report - in
    the order asked. In `verify_corroborated` an endpoint that answered that
    it has not seen the send, or has not confirmed it yet, is there with None
    (its answer text says which). An endpoint that could not be read did not
    answer and is not there. `observed_answers` maps those same endpoints to their
    answers as text. `record()` is all of it as a JSON-ready dict.

    `node_observations` is the per-node record `verify_corroborated` keeps
    everywhere else (`Corroboration.observations`, `NotCorroborated` and
    `Mismatch` `.observations`): one `Observation` per configured endpoint,
    unreadable ones included, each with its state and UTC `read_at`, and
    those after the contradiction as "not-asked". It is
    a separate attribute because `observations` here is the mapping above;
    it is empty when the raiser is not `verify_corroborated`.

    Raises `TypeError` when `answers`, `observations` or `observed_answers` is
    not a mapping (`observations` and `observed_answers` may be None). This is a
    change for direct callers: before `observations` existed, an `answers`
    without `.items()` failed with `AttributeError`. Nothing in this package
    builds one from a non-mapping, so no verification outcome changes.
    """

    def __init__(
        self,
        answers: dict,
        observations: dict | None = None,
        observed_answers: dict | None = None,
        node_observations=(),
    ) -> None:
        answers = _mapping("answers", answers)
        detail = "; ".join(f"{_text(url)}: {_text(answer)}" for url, answer in answers.items())
        super().__init__(f"endpoints disagree: {detail}")
        self.answers = answers
        self.observations = _mapping("observations", observations)
        self.observed_answers = _mapping("observed_answers", observed_answers)
        # One `Observation` per endpoint asked, unreadable ones included, with
        # the time each was read, when raised by `verify_corroborated`.
        self.node_observations = tuple(node_observations)
        # The `Stop` ("disagreement") when raised by `verify_corroborated`, else None.
        self.stop = None

    def record(self) -> dict:
        """Every observation as a strict-JSON dict, for a dispute log.

        `{"verdict": "disagreement", "observations": [{"rpc_url", "answer",
        "payload"}, ...], "served": None}`, one entry per endpoint: those in
        `observations` in their order, then any endpoint only `answers` names.
        `served` is always None: on a disagreement nothing is served.

        Each entry is written on its own budget (`_MAX_NODES` values for the
        answer and again for the payload), and its `rpc_url` on a walk of its
        own, so the endpoint that contradicted is always named with its answer,
        however long the first endpoint's answer was.

        Whatever the values hold, this does not raise and `json.dumps(...,
        allow_nan=False)` accepts the result: see `_jsonable` for how each
        value is written. The one refusal is structural: if `answers`,
        `observations` or `observed_answers` has been replaced by something that
        is not a mapping, it raises `TypeError`, as the constructor would have.
        """
        answers = _pairs("answers", self.answers)
        observations = _pairs("observations", self.observations)
        observed_answers = _pairs("observed_answers", self.observed_answers)
        answers_index, observations_index, observed_index = (
            _index(answers), _index(observations), _index(observed_answers)
        )
        # Each endpoint's answer and payload get a budget of their own, so a
        # long first answer (1500 payers) cannot crowd out the endpoint that
        # contradicted it, while shared substructures (`a = [a, a]` forty times
        # over) still cannot make the record, or json.dumps of it, exponential.
        # The rpc_url is written on a walk of its own too: it is always there.
        entries = []
        seen = [url for url, _ in observations]
        seen += [
            url for url, _ in answers
            if not _lookup(observations, observations_index, url)[0]
        ]
        for url in seen:
            try:
                found, answer = _lookup(observed_answers, observed_index, url)
                if not found:
                    answer = _lookup(answers, answers_index, url)[1]
                entries.append(
                    {
                        "rpc_url": _jsonable(url, _Walk()),
                        "answer": _jsonable(answer, _Walk()),
                        "payload": _jsonable(
                            _lookup(observations, observations_index, url)[1], _Walk()
                        ),
                    }
                )
            except Exception as error:  # belt and braces: _lookup and _jsonable do not raise
                entries.append({"rpc_url": None, "answer": None, "payload": _capped_repr(error)})
        return {"verdict": "disagreement", "observations": entries, "served": None}


def _text(value) -> str:
    """`f"{value}"`, cut like `record()` cuts a string, or a placeholder when that raises.

    Text over `_MAX_STR_CHARS` characters is cut to its first `_MAX_STR_CHARS`
    characters plus `"...<truncated str of N chars>"` (`text` in place of `str`
    for a value that is not a string), so one long answer cannot make the
    message large. An int over 4300 decimal digits (or the interpreter's lower limit) cannot be
    turned into text and raises `ValueError`; it is written `"<int 0x...>"` in
    hex, as `record()` writes it: cut, like a long string, to its first
    `_MAX_STR_CHARS` characters plus `"...<truncated int of N chars>"`, so the
    message stays bounded however large the int. Anything else whose text
    raises is `"<unprintable TYPE>"`.
    """
    try:
        return _cap(str.__str__(f"{value}"), "str" if isinstance(value, str) else "text")
    except Exception:
        try:
            if isinstance(value, int):
                return _int_hex(int(value))
        except Exception:
            pass
        return "<unprintable %s>" % _type_name(value)


def _mapping(name: str, value) -> dict:
    """`value` as a plain dict, or `TypeError` naming `name` when it is not a mapping.

    An exact dict is copied with `dict(value)`, which reuses the hashes it
    already holds, so a key whose `__hash__` raises when asked again is kept as
    it is. Any other mapping is rebuilt from its items; a key that cannot be
    hashed there is replaced by the placeholder `"<unhashable TYPE #i>"` (`i`
    its position) rather than raising, and its value is kept.
    """
    pairs = _pairs(name, value)
    if type(value) is dict:
        try:
            return dict(value)
        except Exception:
            pass
    out = {}
    for position, pair in enumerate(pairs):
        try:
            key, item = pair
        except Exception as error:
            raise TypeError(
                f"Disagreement {name} could not be read as a mapping: {error!r}"
            ) from None
        try:
            out[key] = item
        except Exception:
            placeholder = "<unhashable %s #%d>" % (_type_name(key), position)
            while placeholder in out:
                placeholder += "'"
            out[placeholder] = item
    return out


def _pairs(name: str, value) -> list:
    """`value`'s items as a list of pairs, without hashing a key again.

    `TypeError` naming `name` when it is not a mapping or its `items()` fails.
    """
    if value is None and name != "answers":
        return []
    if not isinstance(value, Mapping) and not callable(getattr(value, "items", None)):
        raise TypeError(
            f"Disagreement {name} must be a mapping of endpoint to value, "
            f"got {_type_name(value)}"
        )
    try:
        return [tuple(pair) for pair in value.items()]
    except Exception as error:
        raise TypeError(f"Disagreement {name} could not be read as a mapping: {error!r}") from None


def _index(pairs: list):
    """`pairs` as a dict for fast lookups, or None when a key cannot be hashed."""
    try:
        return dict(pairs)
    except Exception:
        return None


def _lookup(pairs: list, index, key) -> tuple:
    """`(True, value)` for `key` in `pairs`, else `(False, None)`; never raises.

    Uses `index` when it can; a key whose `__hash__` or `__eq__` raises falls
    back to a scan by identity, then by an `==` that is allowed to fail.
    """
    if index is not None:
        try:
            if key in index:
                return True, index[key]
            return False, None
        except Exception:
            pass
    for candidate, value in pairs:
        if candidate is key:
            return True, value
    for candidate, value in pairs:
        try:
            if candidate == key:
                return True, value
        except Exception:
            continue
    return False, None


# How deep record() follows nested containers before writing a placeholder, so
# a deeply nested value cannot exhaust the interpreter's recursion limit.
_MAX_DEPTH = 64

# How many values record() writes for one endpoint's answer, and again for its
# payload, before the rest become a placeholder. Each endpoint has its own, so
# one long answer cannot hide another endpoint's. A structure that shares a
# substructure (`a = [a, a]`, repeated) is a tree exponentially larger than the
# objects it is built from; this keeps both record() and json.dumps of its
# result bounded, at most linearly in the number of endpoints.
_MAX_NODES = 10_000

# Strings (and the `repr` written for a value with no JSON form) longer than
# this are cut to their first `_MAX_STR_CHARS` characters plus a marker that
# gives the original length (see `_cap`), so one long string cannot make the
# record large either. Each value counted against `_MAX_NODES` is at most an
# exact int of `_MAX_INT_DIGITS` digits or a cut string of about
# `_MAX_STR_CHARS` characters, and an object entry also writes its key's text
# (cut the same way, but not counted), so each walk is at most about
# `_MAX_NODES * (_MAX_INT_DIGITS + 2 * _MAX_STR_CHARS)` characters: some 60 MB.
# Bounded, not small; real answers are far below it.
_MAX_STR_CHARS = 1_000

# Ints with more decimal digits than this are written as a hex string (see
# `_jsonable`): Python refuses str() of an int over 4300 digits by default.
_MAX_INT_DIGITS = 4000


def _int_digits() -> int:
    """`_MAX_INT_DIGITS`, or less if the interpreter's own limit is set lower."""
    try:
        limit = int(sys.get_int_max_str_digits())
    except Exception:
        limit = 0
    return min(_MAX_INT_DIGITS, limit) if limit > 0 else _MAX_INT_DIGITS


class _Walk:
    """State shared by one walk: the containers being written, and the budget."""

    def __init__(self) -> None:
        self.active: set = set()
        self.left = _MAX_NODES


def _jsonable(value, _walk=None, _depth=0):
    """`value` as plain, strict JSON types (`allow_nan=False`), never raising.

    - None, bools, ints (up to `_MAX_INT_DIGITS` digits, which any amount of
      raw is, so it stays exact) and finite floats are kept; NaN and infinity become their `repr` text, cut like any `repr` below.
    - A dataclass - a `Receipt`, a payer report - is an object of every one of
      its fields under the field's own name, `seen_at` and `not_after`
      included even where its `to_json` leaves them out, plus whatever else its
      `to_json` adds (a derived `repeat_rate`, a `time_source`). The field's
      value wins where both give one.
    - Another object with a `to_json` is what that returns.
    - A mapping whose keys are all strings stays an object keyed by each
      key's own text (not its `__str__`), as long as no two keys share a text;
      two distinct keys that do (str subclasses with their own `__eq__`) make
      it a list of pairs instead, so neither is lost. A mapping with any
      other key becomes a list of `[key, value]` pairs in its own order, each key written by these same rules, so an int
      key stays an int and `1` and `"1"` are never merged into one.
    - Lists and tuples are lists; a set is a list in a stable order.
    - Anything else - bytes, a Decimal, an object of some caller's - is its
      `repr`; one whose `repr` raises is a placeholder naming its type. So is a
      container that contains itself, one nested deeper than `_MAX_DEPTH`, and
      any value whose inspection raises.
    - An int too long for `str()` (over `_MAX_INT_DIGITS` decimal digits;
      Python refuses over 4300) is the string `"<int 0x...>"` in hex, which
      has no such limit. Like a long string it is cut to its first
      `_MAX_STR_CHARS` characters followed by `"...<truncated int of N
      chars>"`, N the length of the whole hex text, so its leading digits and
      its size are kept and the record stays bounded. Any amount of raw is far
      below this and is written as an exact int.
    - After `_MAX_NODES` values in one walk (record() gives each endpoint's
      answer and payload a walk of their own), every further value is the
      placeholder `"<omitted TYPE>"`, so a structure that shares substructures
      cannot make the record exponentially large. A container met once the
      budget is spent writes ONE marker for everything it did not write - a
      list `"<omitted N of M items>"`, an object the key `"<omitted>"` with
      `"N of M entries"` - not one per element, so a three-million-element
      list costs one string, and the record stays bounded by the budget. A
      dataclass's scalar fields are written before its containers, so they are
      not the ones omitted.
    - A string (or a `repr`) over `_MAX_STR_CHARS` characters is cut to its
      first `_MAX_STR_CHARS` characters followed by `"...<truncated str of N
      chars>"`, N its original length; so is a mapping key's text.
    """
    walk = _Walk() if _walk is None else _walk
    try:
        return _jsonable_inner(value, walk, _depth)
    except Exception:
        return _capped_repr(value)


def _jsonable_inner(value, walk, depth):
    if walk.left <= 0:
        return "<omitted %s>" % _type_name(value)
    walk.left -= 1
    active = walk.active
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        value = int(value)
        if value.bit_length() > _int_digits() * 3.32:  # log2(10) ~ 3.3219
            return _int_hex(value)
        return value
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else _capped_repr(value)
    if isinstance(value, str):
        return _cap(str.__str__(value), "str")
    if id(value) in active:
        return "<cycle %s>" % _type_name(value)
    if depth >= _MAX_DEPTH:
        return "<too deep %s>" % _type_name(value)
    active.add(id(value))
    try:
        nested = depth + 1
        if is_dataclass(value) and not isinstance(value, type):
            out = {}
            extra = _to_json(value)
            if isinstance(extra, dict):
                out.update((_cap(str(key), "str"), item) for key, item in extra.items())
            for spec in fields(value):
                out[spec.name] = getattr(value, spec.name)
            # Scalar fields (an amount, a height, a count) are written before the
            # containers, so a long list in one field cannot use up the budget
            # they need; the object keeps its own field order.
            scalars = [
                key for key, item in out.items()
                if item is None or isinstance(item, (bool, int, float, str))
            ]
            order = scalars + [key for key in out if key not in set(scalars)]
            written = {}
            for key in order:
                if walk.left <= 0:
                    break
                written[key] = _jsonable(out[key], walk, nested)
            result = {key: written[key] for key in out if key in written}
            return _omitted_entries(result, len(out) - len(written))
        if hasattr(value, "to_json"):
            loaded = _to_json(value)
            return _capped_repr(value) if loaded is _NO_JSON else _jsonable(loaded, walk, nested)
        if isinstance(value, Mapping):
            pairs = list(value.items())
            if all(isinstance(key, str) for key, _ in pairs):
                # A key's own text, not its `__str__`; two keys that are distinct
                # to the mapping but share a text (str subclasses with their own
                # `__eq__`) fall through to pairs below rather than merge.
                # A capped text that collides with another key's makes it pairs too.
                texts = [_cap(str.__str__(key), "str") for key, _ in pairs]
                if len(set(texts)) == len(texts):
                    result = {}
                    for text, (_, item) in zip(texts, pairs):
                        if walk.left <= 0:
                            break
                        result[text] = _jsonable(item, walk, nested)
                    return _omitted_entries(result, len(pairs) - len(result))
            items = []
            for key, item in pairs:
                if walk.left < 2:  # a pair is at least two values: do not write half of one
                    break
                items.append([_jsonable(key, walk, nested), _jsonable(item, walk, nested)])
            return _omitted_items(items, len(pairs))
        if isinstance(value, (list, tuple, set, frozenset)):
            items = []
            for item in value:
                if walk.left <= 0:
                    break
                items.append(_jsonable(item, walk, nested))
            if isinstance(value, (set, frozenset)):
                # A set has no order of its own; give the record one.
                items.sort(key=lambda item: json.dumps(item, sort_keys=True))
            return _omitted_items(items, len(value))
        return _capped_repr(value)
    finally:
        active.discard(id(value))


def _int_hex(value: int) -> str:
    """`"<int 0x...>"` for `value`, passed through `_cap` as kind `"int"`.

    Only the leading hex digits that survive the cap are computed, so a
    200,000-digit int costs no more than a short one: the result is what
    `_cap("<int %s>" % hex(value), "int")` would be, without building the
    whole hex text first.
    """
    sign = "-" if value < 0 else ""
    magnitude = -value if value < 0 else value
    digits = max(1, (magnitude.bit_length() + 3) // 4)
    head = "<int %s0x" % sign
    total = len(head) + digits + 1
    if total <= _MAX_STR_CHARS:
        return "%s%x>" % (head, magnitude)
    keep = max(0, _MAX_STR_CHARS - len(head))
    leading = "%x" % (magnitude >> (4 * (digits - keep))) if keep else ""
    return "%s...<truncated int of %d chars>" % ((head + leading)[:_MAX_STR_CHARS], total)


def _cap(text: str, kind: str) -> str:
    """`text`, or its first `_MAX_STR_CHARS` characters and a marker with its length."""
    if len(text) <= _MAX_STR_CHARS:
        return text
    return "%s...<truncated %s of %d chars>" % (text[:_MAX_STR_CHARS], kind, len(text))


def _omitted_items(items: list, total: int) -> list:
    """`items`, plus one `"<omitted N of M items>"` if fewer than `total` were written."""
    if len(items) < total:
        items.append("<omitted %d of %d items>" % (total - len(items), total))
    return items


def _omitted_entries(result: dict, missing: int) -> dict:
    """`result`, plus one `"<omitted>"` entry counting the `missing` entries not written."""
    if missing > 0:
        key = "<omitted>"
        while key in result:  # a real key of that text was written; do not overwrite it
            key = "<" + key + ">"
        result[key] = "%d of %d entries" % (missing, len(result) + missing)
    return result


_NO_JSON = object()


def _to_json(value):
    """`json.loads(value.to_json())`, or `_NO_JSON` when there is none or it fails."""
    try:
        method = getattr(value, "to_json", None)
        if method is None:
            return _NO_JSON
        return json.loads(method())
    except Exception:
        return _NO_JSON


def _type_name(value) -> str:
    """`value`'s type name cut like a string (`_cap`, kind `"str"`), or `"object"`."""
    try:
        return _cap(str.__str__(type(value).__name__), "str")
    except Exception:
        return "object"


def _capped_repr(value) -> str:
    """`_repr(value)` cut to `_MAX_STR_CHARS` characters as kind `"repr"`; never raises."""
    try:
        return _cap(str.__str__(_repr(value)), "repr")
    except Exception:
        return "<unrepresentable %s>" % _type_name(value)


def _repr(value) -> str:
    """`repr(value)`, or a placeholder when even that raises."""
    try:
        return str(repr(value))
    except Exception:
        return "<unrepresentable %s>" % _type_name(value)

# The version of the `Corroboration.to_json()` record. It is written into every
# record as "schema", so a stored receipt says which vocabulary it was written in.
#
# /3 added the "not-asked" state (a configured node the check never queried) and
# the receipt-level "refusal" / "ask_again" fields; /2 had neither, and listed only
# the nodes it asked.
#
# /4 adds, per observation, "attempted_at" and "returned_at" (null where there is
# none), and the receipt-level "stop" (why the loop stopped, and the k of n in force)
# with its vocabulary "stop_reasons" pinned as STOP_REASONS_SHA256. The node-state
# vocabulary, and so NODE_STATES_SHA256, is the one /3 pinned.
#
# /5 records the order the nodes were asked in, because once the loop can stop
# early, who went first decides which nodes made up the k and which ended up
# "not-asked". Each observation gains "ask_index" (its 0-based place in that
# order; a not-asked node keeps its planned place), and the receipt gains
# "configured_order", "ask_order", "order_rule" and "order_seed". Neither
# vocabulary changed, so both pinned hashes are the ones /4 pinned.
RECEIPT_SCHEMA = "nano_quorum.corroboration/5"

# The only states an `Observation` can carry, in a fixed order, each with what it
# means. This exact mapping is written into every `to_json()` record as
# "node_states", so a reader of a stored receipt needs no code to tell the states
# apart - above all "not-seen" (the node ANSWERED, and has no such block) from
# "unreadable" (the node gave NO answer about the block at all).
NODE_STATES = {
    "confirmed": "the node answered: it has the block, confirmed, and it matches the expected payment",
    "unconfirmed": "the node answered: it has the block, but has not confirmed it yet",
    "not-seen": "the node answered, and its answer is that it does not have the block",
    "unreadable": (
        "the node gave no answer about the block: it errored, timed out, refused, or "
        "returned something that is not a node reply; 'error' says which. "
        "This is not an empty answer"
    ),
    "mismatch": "the node answered: it has the block confirmed, and it does not match the expected payment",
    "not-asked": (
        "the node is in the configured set but was never queried: the check ended (quorum "
        "reached, or a refusal) before its turn. It said nothing; this is not a no, not an "
        "error, and not a silence"
    ),
}


def node_states_sha256(table: dict) -> str:
    """SHA-256 (lowercase hex) of a node-state table, as written into a receipt.

    The canonical form, which is all a stranger needs to recompute it from a
    stored receipt alone:

        json.dumps(table, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    encoded as UTF-8, then hashed with SHA-256. Keys are sorted, there is no
    whitespace, and non-ASCII text is kept as UTF-8 rather than escaped.

    The hash covers the inline "node_states" table of the record, nothing
    else. A reader can therefore check that the table in a record was not
    edited after it was written (recompute and compare with the record's
    "node_states_sha256"), and that two records carrying the same "schema"
    string mean the same vocabulary (their hashes are equal, and equal to the
    value pinned for that schema in `NODE_STATES_SHA256`).
    """
    return hashlib.sha256(canonical_json(table).encode("utf-8")).hexdigest()


def canonical_json(value) -> str:
    """`value` as canonical JSON text: sorted keys, no whitespace, UTF-8 kept.

    `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`,
    the form `node_states_sha256` hashes. It is also the form the per-node
    fixtures under tests/fixtures/observations/ are written in (plus one
    trailing newline), so the same record always serializes to the same bytes.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# The hash of `NODE_STATES` that RECEIPT_SCHEMA names, pinned. Editing any state
# or its meaning changes the hash, and the check below then refuses to import
# the module until RECEIPT_SCHEMA is bumped and this value re-pinned with it -
# so "nano_quorum.corroboration/5" cannot quietly come to mean something else.
# (/2, without "not-asked", pinned f2d10f78a2b6aa1c705bfdfcf28092261d1f0d837e50ec292c80668d6d9f9c77;
# /3 and /4 pinned this same value.)
NODE_STATES_SHA256 = "3db25e35737038c4d04d16d1306184d7b70746ab726794e6473bf4c51ad4448c"


def _check_vocabulary_pin(table: dict, pinned: str, name: str = "NODE_STATES") -> None:
    """Raise unless `table` hashes to `pinned`; run on each pinned table at import."""
    actual = node_states_sha256(table)
    if actual != pinned:
        raise AssertionError(
            f"{name} hashes to {actual}, but {RECEIPT_SCHEMA} pins {pinned}: "
            f"a changed vocabulary needs a new RECEIPT_SCHEMA and a new {name}_SHA256"
        )


_check_vocabulary_pin(NODE_STATES, NODE_STATES_SHA256)


# Why `verify_corroborated` stopped asking, in a fixed order, each with what it
# means. These are the only ways its loop over the endpoints ends; it has no
# deadline, no time budget and no call budget, so none is listed. A
# `Corroboration` can only carry "quorum-reached" (settled) or "exhausted"
# (unsettled); "mismatch" and "disagreement" end in a raise, and the exception
# carries the same `Stop` as `.stop`. Like NODE_STATES, this exact table is
# written into every record ("stop_reasons") with its hash, computed the same
# way (`node_states_sha256`) and pinned below.
STOP_REASONS = {
    "quorum-reached": (
        "k of the n configured endpoints reported the same confirmed send: the receipt is "
        "settled, and the endpoints after the k-th agreeing one were not asked"
    ),
    "exhausted": (
        "every configured endpoint was asked and fewer than k reported the same confirmed "
        "send: the receipt is unsettled (ask again), or NotCorroborated is raised when fewer "
        "than k answered at all"
    ),
    "mismatch": (
        "an endpoint reported the block confirmed and not the expected payment: Mismatch is "
        "raised, and the endpoints after it were not asked"
    ),
    "disagreement": (
        "two endpoints gave answers that cannot both be true (a different confirmed send, a "
        "different payer chain, or contents that hash to another block): Disagreement is "
        "raised, and the endpoints after it were not asked"
    ),
}

# The hash of `STOP_REASONS` that RECEIPT_SCHEMA names, pinned as NODE_STATES_SHA256 is.
STOP_REASONS_SHA256 = "10cd1fcb6b1e1d6d8c3669184a2be0d47697364bd6b13b246ad33cae8c1e1342"

_check_vocabulary_pin(STOP_REASONS, STOP_REASONS_SHA256, "STOP_REASONS")


# How the ask order is decided from the configured endpoints, written into every
# record as "order_rule". Nothing else decides it: there is no other shuffle.
ORDER_RULES = {
    "as-given": (
        "the endpoints are asked in the order the caller gave them, after duplicates are "
        "folded (the first spelling kept); order_seed is null"
    ),
    "seed-sha256": (
        "the endpoints are asked in ascending order of the lowercase hex SHA-256 of the "
        "UTF-8 text str(order_seed) + newline + the endpoint as written in the record "
        "(credentials redacted), ties kept in configured order"
    ),
}


def _order_seed(value):
    """`value` as an order seed (None or an int from 0 to 2**64 - 1), or TypeError / ValueError."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"order_seed must be an int or None, got {type(value).__name__}")
    if not 0 <= value < 2**64:
        raise ValueError(f"order_seed must be from 0 to 2**64 - 1, got {value!r}")
    return int(value)


def _ask_order(endpoints, seed) -> tuple:
    """The order `endpoints` are asked in under `seed` (see `ORDER_RULES`)."""
    endpoints = tuple(endpoints)
    if seed is None:
        return endpoints
    prefix = "%d\n" % seed

    def key(rpc_url):
        return hashlib.sha256((prefix + redact_url(rpc_url)).encode("utf-8")).hexdigest()

    return tuple(sorted(endpoints, key=key))  # sorted is stable: ties keep configured order


# The stored schemas `verify_receipt_vocabulary` reads, each with the vocabulary
# tables a record of it carries. All three use the node-state vocabulary /3
# pinned (NODE_STATES_SHA256 is unchanged since /3); /4 added "stop_reasons",
# unchanged in /5. Any other schema is refused.
_VOCABULARY_TABLES_OF_SCHEMA = {
    "nano_quorum.corroboration/3": ("node_states",),
    "nano_quorum.corroboration/4": ("node_states", "stop_reasons"),
    RECEIPT_SCHEMA: ("node_states", "stop_reasons"),
}


def verify_receipt_vocabulary(record) -> bool:
    """Whether a stored receipt's state vocabulary is the one its schema names.

    `record` is a `to_json()` string or the dict it parses to. True only when
    the record's inline "node_states" table hashes (see `node_states_sha256`)
    to the record's own "node_states_sha256" AND to `NODE_STATES_SHA256`, its
    inline "stop_reasons" table likewise hashes to its "stop_reasons_sha256"
    AND to `STOP_REASONS_SHA256`, and its "schema" is one of
    "nano_quorum.corroboration/3", "nano_quorum.corroboration/4" or
    `RECEIPT_SCHEMA` ("nano_quorum.corroboration/5"). All three carry the
    node-state vocabulary /3 pinned; a /3 record predates "stop_reasons", so
    for /3 only "node_states" and its hash are checked. False for an edited
    table, an edited or missing hash, or a record of any other schema (/2, a
    later /6, a missing or non-string "schema"). Nothing is raised for a
    well-formed JSON object; a string that is not JSON raises ValueError.
    """
    if isinstance(record, (str, bytes)):
        record = json.loads(record)
    if not isinstance(record, dict) or not isinstance(record.get("schema"), str):
        return False
    tables = _VOCABULARY_TABLES_OF_SCHEMA.get(record["schema"])
    if tables is None:
        return False
    pins = {"node_states": NODE_STATES_SHA256, "stop_reasons": STOP_REASONS_SHA256}
    for key, pinned in ((name, pins[name]) for name in tables):
        table = record.get(key)
        if not isinstance(table, dict):
            return False
        if not node_states_sha256(table) == record.get(key + "_sha256") == pinned:
            return False
    return True


# The userinfo of a URL: after `scheme://` or a scheme-relative `//`, up to the
# last `@` before the path. Leading whitespace is allowed, as `_endpoint_key`
# strips it before a URL is used.
_CREDENTIALS = re.compile(r"^(\s*(?:[A-Za-z][A-Za-z0-9+.-]*:)?//)[^/?#]*@")
# The same userinfo inside free text - an error message that quotes the URL. A
# URL ends at whitespace there, so the userinfo does too.
_CREDENTIALS_IN_TEXT = re.compile(r"((?:[A-Za-z][A-Za-z0-9+.-]*:)?//)[^/?#\s]*@")


def redact_url(rpc_url: object) -> object:
    """`rpc_url` with any `user:pass@` (or `token@`) replaced by `***@`.

    Used wherever a receipt writes an endpoint down, so a key carried in the
    URL is not stored or shown with the record. Only the userinfo is touched;
    a key in the path or query string is not recognised. A URL with leading
    whitespace, or a scheme-relative `//user:pass@host`, is redacted too. A
    non-string is returned as it is.
    """
    if not isinstance(rpc_url, str):
        return rpc_url
    return _CREDENTIALS.sub(r"\1***@", rpc_url)


def _redact_text(text: object) -> object:
    """`text` with the userinfo of every URL in it replaced by `***@`.

    For the free text a record writes beside an endpoint - an error message or
    a detail that may quote the URL it failed on. A non-string is returned as
    it is.
    """
    if not isinstance(text, str):
        return text
    return _CREDENTIALS_IN_TEXT.sub(r"\1***@", text)


# The one shape of UTC time a record carries: extended calendar date, "T",
# hours, minutes and seconds, an optional fraction of 1 to 6 digits, and "Z"
# or "+00:00". Week dates, ordinal dates, the basic (no-separator) form, a time
# without seconds and longer fractions are refused.
_UTC_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", re.ASCII)


def _is_utc_iso(text: object) -> bool:
    """Whether `text` is a full ISO-8601 UTC date and time, ending in Z or +00:00 (see `_UTC_ISO`)."""
    if not isinstance(text, str) or not _UTC_ISO.fullmatch(text):
        return False
    try:
        _moment(text)
    except ValueError:
        return False
    return True


def _moment(text: str) -> datetime:
    """A UTC ISO-8601 text ending in Z or +00:00 (already checked by `_is_utc_iso`) as a datetime."""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


@dataclass(frozen=True)
class Observation:
    """What one endpoint said about the block, and when its answer was read.

    `state` is one of the keys of `NODE_STATES`, which says what each means;
    any other value is refused when the `Observation` is made.

    "not-seen" and "unreadable" are different facts: a node that answered "no
    such block" told us something; a node that errored told us nothing. An
    "unreadable" observation therefore always carries `error_kind` (the
    exception's type name) and `error_message`, and to_dict() writes them as
    "error"; no other state carries them.

    "not-seen" and "unconfirmed" are kept apart on purpose: both leave the
    receipt unsettled, but they are different facts about different nodes, and
    a record that folds them loses which node said which and when.
    `read_at` is UTC ISO-8601 ending in Z or +00:00, and every state but "not-asked"
    must carry one: an asked node with no read time, or one that is not UTC
    ISO-8601, is refused when the `Observation` is made.

    `payer_account` is the payer chain (`block_account`) that a "confirmed"
    node's reply named, as a nano_ address, so the per-node record says which
    chain each node reported - two nodes on different chains raise
    `Disagreement`, and this is how its `node_observations` tell them apart.
    It is empty when the reply named no valid payer chain, and to_dict()
    writes it as "payer_account" only when it is set, so a record with no
    payer named is unchanged. Only a "confirmed" observation carries one, and
    it must be a valid address.

    "not-asked" is a node in the configured set that was never queried,
    because the check ended (quorum reached, or a refusal) before its turn. It
    was not read, so its `read_at` is empty in memory and null in to_dict();
    `detail` says why it was not asked. It carries no error and no payer.

    `attempted_at` is the UTC moment the request to the node was started, and
    `returned_at` the UTC moment its answer came back; both are written by
    to_dict(), null when empty. How they relate to `read_at`:

    - an answered node (any state but "unreadable" and "not-asked"):
      `returned_at` IS `read_at` - the answer was read the moment it came
      back - so when it is set it must equal `read_at`, and
      `attempted_at <= read_at`.
    - "unreadable": no answer came back, so `returned_at` is always empty
      (null). `read_at` is then the moment the failure was recorded, and
      `attempted_at <= read_at`; their difference is how long the node was
      waited on.
    - "not-asked": no request was made, so both are empty (null).

    Both are optional for an asked node built by hand, so an `Observation`
    made as before (`read_at` only) is still accepted and writes both as
    null; but an answered node with an `attempted_at` must also carry its
    `returned_at`. `verify_corroborated` fills both for every node it asks;
    when its clock stepped backwards between the two reads it records
    `attempted_at` as `read_at` (clamped), so a clock never makes a check raise.
    Neither is part of `Corroboration` equality, as `read_at` is not; all
    three are part of an `Observation`'s own `==` and hash (it is a plain
    frozen dataclass).

    `ask_index` is the node's 0-based place in the order the check planned to
    ask the nodes in (the receipt's "ask_order"). A "not-asked" node keeps the
    place it had in that plan: it is never null for a node `verify_corroborated`
    listed, asked or not. It is None (null in to_dict()) only for an
    `Observation` built by hand without one.
    """

    rpc_url: str
    state: str
    read_at: str
    detail: str = ""
    error_kind: str = ""
    error_message: str = ""
    payer_account: str = ""
    attempted_at: str = ""
    returned_at: str = ""
    ask_index: int | None = None

    def __post_init__(self) -> None:
        if self.ask_index is not None and (
            isinstance(self.ask_index, bool) or not isinstance(self.ask_index, int) or self.ask_index < 0
        ):
            raise ValueError(f"ask_index must be a non-negative int or None, got {self.ask_index!r}")
        if self.state not in NODE_STATES:
            raise ValueError(f"unknown node state {self.state!r}; expected one of {tuple(NODE_STATES)}")
        if self.state == "unreadable" and not self.error_kind:
            raise ValueError("an unreadable observation must say what the error was")
        if self.state != "unreadable" and (self.error_kind or self.error_message):
            raise ValueError(f"a {self.state!r} observation is an answer and carries no error")
        if self.payer_account and self.state != "confirmed":
            raise ValueError(f"a {self.state!r} observation names no payer chain; only a confirmed one does")
        if self.payer_account and not nano_settlement_verify.is_valid_account(self.payer_account):
            raise ValueError(f"payer_account {self.payer_account!r} is not a Nano address")
        if self.state == "not-asked" and self.read_at:
            raise ValueError("a not-asked node was never read, so it has no read_at")
        if self.state != "not-asked" and not _is_utc_iso(self.read_at):
            # A node that was asked was read at some moment. Without it, a stored
            # record cannot say how old the answer is: "not seen" at 12:00 and
            # "not seen" at 12:30 would read the same.
            raise ValueError(
                f"a {self.state!r} observation was read, so its read_at must be the UTC "
                f"ISO-8601 moment of that read ending in Z (or +00:00), got {self.read_at!r}"
            )
        self._check_timing()

    def _check_timing(self) -> None:
        """Refuse an `attempted_at` / `returned_at` that contradicts the state or `read_at`."""
        for name in ("attempted_at", "returned_at"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string, got {type(value).__name__}")
            if self.state == "not-asked" and value:
                raise ValueError(f"a not-asked node was never asked, so it has no {name}")
            if value and not _is_utc_iso(value):
                raise ValueError(f"{name} must be UTC ISO-8601 ending in Z (or +00:00), got {value!r}")
        if self.state == "not-asked":
            return
        if self.state == "unreadable" and self.returned_at:
            raise ValueError("an unreadable node returned no answer, so it has no returned_at")
        if self.returned_at and self.returned_at != self.read_at:
            raise ValueError(
                f"returned_at {self.returned_at!r} must equal read_at {self.read_at!r}: "
                "an answer is read the moment it returns"
            )
        if self.attempted_at:
            if _moment(self.attempted_at) > _moment(self.read_at):
                raise ValueError(
                    f"attempted_at {self.attempted_at!r} is after read_at {self.read_at!r}"
                )
            if self.state != "unreadable" and not self.returned_at:
                raise ValueError(
                    f"a {self.state!r} observation is an answer: with an attempted_at it "
                    "must carry the returned_at of that answer"
                )

    def to_dict(self) -> dict:
        out = {
            "ask_index": self.ask_index,
            "rpc_url": redact_url(self.rpc_url),
            "state": self.state,
            "read_at": None if self.state == "not-asked" else self.read_at,
            "attempted_at": self.attempted_at or None,
            "returned_at": self.returned_at or None,
            "detail": _redact_text(self.detail),
        }
        if self.state == "unreadable":
            out["error"] = {"kind": self.error_kind, "message": _redact_text(self.error_message)}
        if self.payer_account:
            out["payer_account"] = self.payer_account
        return out

    @classmethod
    def from_dict(cls, entry) -> "Observation":
        """The `Observation` a `to_dict()` entry was written from, read back.

        The inverse of to_dict() up to redaction: a URL written with `***@` is
        read back as that text, and writing it again redacts nothing more, so
        `to_dict(from_dict(entry)) == entry` for any entry to_dict() wrote.
        Every key to_dict() always writes is required, "error" exactly for an
        "unreadable" entry and "payer_account" only when it was written;
        anything else - an unknown key, a missing one, a value of the wrong
        type, an "error" on any other state, or "" where to_dict() writes null
        for an empty time ("read_at", "attempted_at", "returned_at") - raises
        ValueError, as does any entry the constructor refuses. Nothing is
        dropped silently: an entry is read back only if to_dict() could have
        written it.
        """
        if not isinstance(entry, Mapping):
            raise ValueError(f"an observation entry must be a JSON object, got {_type_name(entry)}")
        required = {"ask_index", "rpc_url", "state", "read_at", "attempted_at", "returned_at", "detail"}
        optional = {"error", "payer_account"}
        keys = set(entry)
        if not required <= keys or not keys <= required | optional:
            raise ValueError(
                f"an observation entry has the keys {sorted(required)} and optionally "
                f"{sorted(optional)}, got {sorted(map(str, keys))}"
            )

        def text(name, nullable=False):
            value = entry[name]
            if value is None and nullable:
                return ""
            if not isinstance(value, str):
                raise ValueError(f"observation {name} must be a string, got {_type_name(value)}")
            if nullable and not value:
                # to_dict() writes an empty time as null, never as "".
                raise ValueError(f'observation {name} is null when empty, never ""')
            return value

        error_kind = error_message = ""
        if "error" in entry and entry["state"] != "unreadable":
            raise ValueError('only an unreadable entry carries an "error"')
        if "error" in entry:
            error = entry["error"]
            if not isinstance(error, Mapping) or set(error) != {"kind", "message"}:
                raise ValueError('an observation "error" is {"kind", "message"}')
            error_kind, error_message = error["kind"], error["message"]
            if not isinstance(error_kind, str) or not isinstance(error_message, str):
                raise ValueError("an observation error's kind and message are strings")
        elif entry["state"] == "unreadable":
            raise ValueError('an unreadable entry carries its "error"')
        payer = text("payer_account") if "payer_account" in entry else ""
        if "payer_account" in entry and not payer:
            raise ValueError("payer_account is written only when it is set")
        ask_index = entry["ask_index"]
        if ask_index is not None and (isinstance(ask_index, bool) or not isinstance(ask_index, int)):
            raise ValueError(f"observation ask_index must be an int or null, got {_type_name(ask_index)}")
        return cls(
            text("rpc_url"),
            text("state"),
            text("read_at", nullable=True),
            text("detail"),
            error_kind=error_kind,
            error_message=error_message,
            payer_account=payer,
            attempted_at=text("attempted_at", nullable=True),
            returned_at=text("returned_at", nullable=True),
            ask_index=ask_index,
        )


@dataclass(frozen=True)
class Stop:
    """Why `verify_corroborated` stopped asking, and the quorum rule in force.

    `reason` is one of the keys of `STOP_REASONS`. `agree` is k, the number of
    endpoints that had to report the same confirmed send, and `endpoints` is n,
    the number of distinct endpoints configured (after `distinct_endpoints`
    folds duplicates). `at` is the UTC moment the loop stopped: the `read_at`
    of the last endpoint asked.

    With the per-node observations beside it, this is enough to re-run the
    decision from the record alone: count the "confirmed" observations that
    agree, compare with k, and check that every observation after the one at
    which the loop stopped is "not-asked".

    to_dict() writes `{"reason", "quorum": {"k", "n"}, "at"}`.
    """

    reason: str
    agree: int
    endpoints: int
    at: str

    def __post_init__(self) -> None:
        if self.reason not in STOP_REASONS:
            raise ValueError(f"unknown stop reason {self.reason!r}; expected one of {tuple(STOP_REASONS)}")
        for name in ("agree", "endpoints"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive int, got {value!r}")
        if self.agree > self.endpoints:
            raise ValueError(f"a quorum of {self.agree} cannot be met by {self.endpoints} endpoints")
        if not _is_utc_iso(self.at):
            raise ValueError(f"at must be UTC ISO-8601 ending in Z (or +00:00), got {self.at!r}")

    def to_dict(self) -> dict:
        return {"reason": self.reason, "quorum": {"k": self.agree, "n": self.endpoints}, "at": self.at}


_STATE_OF_ANSWER = {
    "settled": "confirmed",
    "not-yet": "unconfirmed",
    "not-seen": "not-seen",
    "mismatch": "mismatch",
    # A confirmed send whose contents hash to another block than the one asked
    # for. The vocabulary (and its pinned hash) is unchanged: it is written as
    # "mismatch" - the node has a block confirmed and it is not the expected
    # payment - with `detail` naming both hashes.
    "other-block": "mismatch",
}
if not set(_STATE_OF_ANSWER.values()) <= set(NODE_STATES):
    raise AssertionError("_STATE_OF_ANSWER names a state outside NODE_STATES")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(moment: datetime) -> str:
    """`moment` as UTC ISO-8601 with a trailing Z; a naive datetime is taken as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    text = moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return text.replace("+00:00", "Z")


@dataclass(frozen=True)
class Corroboration:
    """A receipt, and which endpoints stood behind it.

    `receipt.settled` is True only when `agree` endpoints independently reported
    the same confirmed send. An unsettled receipt here means the endpoints that
    answered did not get there - the block is not confirmed yet, or not enough
    of them have seen it - and, exactly as in `verify`, that is "not yet", not
    "no".

    `observations` has one `Observation` per configured endpoint (after
    `distinct_endpoints` folds duplicates), in the order given: which node said
    what, and when. An endpoint after the quorum was not asked and is listed
    as "not-asked", so "never contacted" is never mistaken for "silent".

    A `Corroboration` is never a refusal - a refusal raises (`Mismatch`,
    `Disagreement`). `to_json()` says so in the record itself: `"refusal":
    false` always, and `"ask_again": true` exactly when the receipt is
    unsettled, so a cached unsettled receipt cannot be read as "rejected".
    Beside it, `"ask_again_not_before"` (see `not_before`) says from when, and
    `"retry_after_s"` the delay it was computed with. `retry_after` is not
    part of equality.

    `stop` is the `Stop` that ended the check: "quorum-reached" for a settled
    receipt, "exhausted" for an unsettled one, with the k of n in force and
    the moment it stopped. `to_json()` writes it as "stop" (null when none was
    recorded, as for one built by hand), beside the full "stop_reasons" table
    and its pinned "stop_reasons_sha256".

    Equality and hash() are over what was decided and what each node said -
    the receipt, the endpoint tuples, and each observation's endpoint, state,
    detail, error and payer chain - and NOT over each observation's `read_at`,
    `attempted_at` or `returned_at`, nor over `stop` (which a settled or
    unsettled receipt already implies, and whose `at` is a time).
    Two calls that got the same answers from the same nodes a second apart are equal and
    hash alike, as they were before `read_at` existed; `to_json()` still
    differs between them, because the record says when each answer was read.

    `to_json()` writes every endpoint URL with any credentials (`user:pass@`)
    replaced by `***@`, and does the same to any URL quoted in an
    observation's `detail` or error message; the tuples and observations in
    memory keep the text exactly as given.
    """

    receipt: Receipt
    agreed: tuple[str, ...]
    answered: tuple[str, ...]
    failed: tuple[str, ...]
    observations: tuple[Observation, ...] = ()
    stop: Stop | None = None
    retry_after: int | float = 0
    # The endpoints as configured (after duplicates are folded), the order they
    # were planned to be asked in - every one, the not-asked ones included - and
    # the seed that order came from (None: as given). See `ORDER_RULES`. Not
    # part of equality: the observations, in ask order, already are.
    configured_order: tuple[str, ...] = ()
    ask_order: tuple[str, ...] = ()
    order_seed: int | None = None

    def __post_init__(self) -> None:
        # Checked here as `verify_corroborated` checks its keyword, so a receipt
        # built by hand cannot carry a NaN, infinite, negative or out-of-range
        # delay into `to_json()` (not strict JSON) or `not_before` (overflow).
        _retry_after(self.retry_after)
        if self.stop is not None and not isinstance(self.stop, Stop):
            raise TypeError(f"stop must be a Stop or None, got {type(self.stop).__name__}")

    @property
    def order_rule(self) -> str | None:
        """The `ORDER_RULES` key that produced `ask_order`, or None when none was recorded."""
        if not self.ask_order:
            return None
        return "as-given" if self.order_seed is None else "seed-sha256"

    @property
    def not_before(self) -> str | None:
        """The UTC moment from which asking again makes sense, or None.

        None for a settled receipt (there is nothing to ask again) and for one
        with no `stop` recorded. Otherwise `stop.at + retry_after` seconds,
        UTC ISO-8601 to the millisecond ending in Z: the check stopped at
        `stop.at`, and `retry_after` is the caller's configured delay
        (`verify_corroborated(retry_after=...)`, default 0). This module knows
        nothing about when a node will confirm a block, so with the default the
        earliest time is simply when this check stopped; any longer wait is the
        caller's, stated in the record as "retry_after_s".
        """
        if self.receipt.settled or self.stop is None:
            return None
        return _not_before(self.stop.at, self.retry_after)

    def _identity(self) -> tuple:
        return (
            self.receipt,
            self.agreed,
            self.answered,
            self.failed,
            tuple(
                (
                    seen.rpc_url, seen.state, seen.detail, seen.error_kind,
                    seen.error_message, seen.payer_account,
                )
                for seen in self.observations
            ),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Corroboration):
            return NotImplemented
        return self._identity() == other._identity()

    def __hash__(self) -> int:
        return hash(self._identity())

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": RECEIPT_SCHEMA,
                "settled": self.receipt.settled,
                # Unsettled means "not yet / could not decide", never "no":
                # whoever reads a cached record must ask again, not refuse.
                "refusal": False,
                "ask_again": not self.receipt.settled,
                # When to ask again: the moment the check stopped plus the
                # configured delay. `ask_again` stays the boolean it was, for
                # readers that test it; null here exactly when it is false (or
                # when no stop was recorded).
                "ask_again_not_before": self.not_before,
                "retry_after_s": self.retry_after,
                # Why the loop stopped and the k-of-n rule in force; null for a
                # Corroboration built by hand without one.
                "stop": self.stop.to_dict() if self.stop is not None else None,
                # Who was to be asked, in what order, and why that order: with
                # each observation's "ask_index", enough to replay which nodes
                # made the k and which were left not-asked (`replay_receipt`).
                "configured_order": [redact_url(url) for url in self.configured_order],
                "ask_order": [redact_url(url) for url in self.ask_order],
                "order_rule": self.order_rule,
                "order_seed": self.order_seed,
                "amount_raw": self.receipt.amount_raw,
                "height": self.receipt.height,
                "account": self.receipt.account,
                "agreed": [redact_url(url) for url in self.agreed],
                "answered": [redact_url(url) for url in self.answered],
                "failed": [redact_url(url) for url in self.failed],
                "observations": [seen.to_dict() for seen in self.observations],
                "node_states": dict(NODE_STATES),
                "node_states_sha256": NODE_STATES_SHA256,
                "stop_reasons": dict(STOP_REASONS),
                "stop_reasons_sha256": STOP_REASONS_SHA256,
            }
        )


# The longest `retry_after` accepted, in seconds (about 31 years): it keeps
# `stop.at + retry_after` inside the range a datetime can hold.
_MAX_RETRY_AFTER = 10**9


def _retry_after(value) -> int | float:
    """`value` as a delay in seconds, or TypeError / ValueError before any call is made."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"retry_after must be a number of seconds, got {type(value).__name__}")
    if not math.isfinite(value) or value < 0 or value > _MAX_RETRY_AFTER:
        raise ValueError(
            f"retry_after must be a finite number of seconds from 0 to {_MAX_RETRY_AFTER}, got {value!r}"
        )
    return int(value) if isinstance(value, int) else float(value)


# The latest moment `_not_before` writes: the last millisecond a datetime holds.
_LATEST = datetime.max.replace(microsecond=999000, tzinfo=timezone.utc)


def _not_before(stopped_at: str, retry_after) -> str:
    """`stopped_at` (UTC ISO-8601) plus `retry_after` seconds, to the millisecond, ending in Z.

    A sub-millisecond remainder rounds UP to the next millisecond, so a nonzero
    delay never writes a time equal to `stopped_at`. A sum past the year 9999
    is written as the latest moment a datetime holds (9999-12-31T23:59:59.999Z)
    instead of raising OverflowError.
    """
    try:
        moment = _moment(stopped_at) + timedelta(seconds=retry_after)
        spare = moment.microsecond % 1000
        if spare:
            moment += timedelta(microseconds=1000 - spare)
    except OverflowError:
        return _iso_utc(_LATEST)
    return _iso_utc(moment)


def _attempt_no_later(attempted_at: str, read_at: str) -> str:
    """`attempted_at`, or `read_at` when a clock that stepped back put the attempt after it."""
    return read_at if _moment(attempted_at) > _moment(read_at) else attempted_at


def _endpoint_key(rpc_url: object) -> str:
    """How two endpoint URLs are told apart as witnesses.

    Only the obvious spelling differences are folded - case and a trailing
    slash. Nothing here resolves DNS or follows redirects, so two different
    names for one node still count as two; that is the caller's to know, and
    `distinct_endpoints` says so.
    """
    if not isinstance(rpc_url, str):
        raise TypeError(f"an endpoint must be a URL string, got {type(rpc_url).__name__}")
    return rpc_url.strip().rstrip("/").lower()


def distinct_endpoints(rpc_urls, agree: int = AGREE_DEFAULT) -> tuple[str, ...]:
    """The endpoints to ask, refusing a list that cannot support `agree`.

    One endpoint written twice is one witness, not two. Silently folding the
    duplicate would leave the caller believing a payment was corroborated by two
    nodes when one node said it twice, so a list with too few distinct entries
    is refused here, before any call is made.

    Being distinct URLs does not make endpoints independent: several public
    Nano RPCs are proxies in front of the same node, and this cannot see that.
    Agreement is only worth what the endpoint list is worth.
    """
    if isinstance(rpc_urls, str):
        raise TypeError("rpc_urls must be a sequence of URLs, not one URL string")
    if isinstance(agree, bool) or not isinstance(agree, int):
        raise TypeError(f"agree must be an int, got {type(agree).__name__}")
    if agree < 2:
        raise ValueError(f"agree must be at least 2; for one endpoint use verify(), got {agree}")
    ordered: list[str] = []
    seen: set[str] = set()
    for rpc_url in rpc_urls:
        key = _endpoint_key(rpc_url)
        if key not in seen:
            seen.add(key)
            ordered.append(rpc_url)
    if len(ordered) < agree:
        raise ValueError(
            f"{len(ordered)} distinct endpoint(s) given, {agree} needed to agree"
        )
    return tuple(ordered)


# What counts as "this endpoint did not answer", as opposed to "it answered and
# the answer was no". OSError covers a refused connection, DNS and the timeout;
# ValueError covers a body that is not JSON, and also `verify`'s NotANodeReply -
# a body that IS JSON but is not a node reply, such as a proxy's status page or
# an error envelope with no `confirmed`, which `verify` refuses by design rather
# than read as settled or unsettled. KeyError is kept because
# `balance_corroborated` below still reads a balance reply directly, and
# TypeError for a reply whose fields are the wrong type. All of them mean the
# same thing here: this endpoint told us nothing, ask the next one.
_UNREADABLE = (OSError, ValueError, KeyError, TypeError)


def post_json_failover(rpc_urls, payload: dict):
    """POST `payload` to each endpoint in turn and return the first real answer.

    Returns `(reply, rpc_url)` - the reply, and which endpoint gave it.

    **This is failover, not corroboration.** It returns ONE endpoint's word,
    whichever answered first, and it is for calls where that is enough: reading
    a representative, listing receivables to decide what to pocket, checking
    whether an account is opened. Do not decide that money arrived with it -
    `verify_corroborated` is that function, and it is a different thing.

    Raises `NotCorroborated` when no endpoint answered readably, carrying what
    each one did instead; that is "we could not look", not "the answer is no".
    """
    if isinstance(rpc_urls, str):
        raise TypeError("rpc_urls must be a sequence of URLs, not one URL string")
    # Duplicates are harmless here - this wants one answer, not two witnesses -
    # but asking the same dead endpoint twice is pure latency on a request path.
    endpoints = tuple(dict.fromkeys(rpc_urls))
    if not endpoints:
        raise ValueError("no endpoints given")
    post = nano_settlement_verify.post_json  # looked up per call so a stub takes effect
    failures: dict[str, str] = {}
    for rpc_url in endpoints:
        try:
            return post(rpc_url, payload), rpc_url
        except _UNREADABLE as error:
            failures[rpc_url] = f"{type(error).__name__}: {error}"
    raise NotCorroborated(answered=0, agree=1, failures=failures)


def _payer_of(reply: object):
    """The chain a block sits on (`block_account`), as its nano_ address, or None.

    The address is rebuilt from the account's public key, so two spellings of
    one account (`nano_`/`xrb_`) are one payer and compare equal. A reply that
    names none, or names something that is not a valid address, has named no
    payer chain at all: it is None, and None is never compared - it neither
    makes two answers disagree nor helps one settle.
    """
    named = reply.get("block_account") if isinstance(reply, dict) else None
    key = nano_settlement_verify.public_key_from_address(named)
    if key is None:
        return None
    from nano_five_fields import _address_of_key  # here, not at the top: it imports this module

    return _address_of_key(key)


# `verify` as this module was imported with it. `_answer` reads the reply itself
# (to check which block it is about); when `nano_settlement_verify.verify` has
# been replaced - stubbed in a test, or wrapped by a caller - it is called
# instead, as it always was, so the replacement still takes effect.
_ORIGINAL_VERIFY = nano_settlement_verify.verify


def _answer(block_hash: str, expect_raw: int, account: str, rpc_url: str):
    """One endpoint's answer, as something two answers can be compared by.

    A settled receipt becomes the facts it asserts - amount, height and payee -
    plus, as a fifth element, the chain the block sits on (`_payer_of`, None
    when the reply names none); everything else becomes a tag. Only the first
    four are compared for agreement; the payer is compared separately, and only
    when two answers both name one. `Mismatch` is carried rather than raised
    here so the caller can decide what one node's refusal is worth.

    The reply is read once, and `verify`'s own verdict is reached on it. When
    the reply's contents are complete enough to recompute a block hash from
    (`nano_five_fields.block_hash_of`) and that hash is not the one asked for,
    the node has answered about a DIFFERENT block: that is tagged "other-block",
    never "settled", however well its amount and payee match. Contents too
    sparse to hash are not a hash checked, and are read as before.

    If `nano_settlement_verify.verify` is no longer the original function, it
    is called as before this module read replies itself, and the answer is
    built from the Receipt it returns; there is then no reply in hand, so no
    block hash and no payer are checked.
    """
    verify = nano_settlement_verify.verify  # looked up per call so a stub takes effect
    if verify is not _ORIGINAL_VERIFY:
        reply = None
        receipt_of = lambda: verify(block_hash, expect_raw, account, rpc_url)  # noqa: E731
    else:
        post = nano_settlement_verify.post_json  # looked up per call so a stub takes effect
        reply = post(rpc_url, {"action": "block_info", "json_block": "true", "hash": block_hash})
        receipt_of = lambda: nano_settlement_verify._receipt_of_reply(  # noqa: E731
            block_hash, expect_raw, account, reply
        )
    try:
        receipt = receipt_of()
    except NotFound:
        return ("not-seen",), None
    except Mismatch as error:
        return ("mismatch", repr(error.got), repr(error.expected)), error
    if not receipt.settled:
        return ("not-yet",), None
    if reply is None:
        return ("settled", receipt.amount_raw, receipt.height, receipt.account, None), receipt
    from nano_five_fields import block_hash_of  # here, not at the top: it imports this module

    answered_for = block_hash_of(reply.get("contents"))
    if answered_for is not None and answered_for != str(block_hash).strip().upper():
        return ("other-block", answered_for, str(block_hash)), None
    return (
        "settled", receipt.amount_raw, receipt.height, receipt.account, _payer_of(reply)
    ), receipt


def verify_corroborated(
    block_hash: str,
    expect_raw: int,
    account: str,
    rpc_urls,
    agree: int = AGREE_DEFAULT,
    *,
    clock=None,
    retry_after=0,
    order_seed=None,
) -> Corroboration:
    """Prove a send settled, believing it only when `agree` endpoints concur.

    Asks the endpoints in the order given and stops as soon as `agree` of them
    have reported the SAME confirmed send - so an endpoint listed after the
    quorum is not called at all, and the list should be ordered by how much the
    caller trusts it. Endpoints that cannot be read are skipped, not retried.

    Returns a `Corroboration`. Its receipt is settled only on agreement. Its
    `observations` name every configured endpoint, the state it reported, and
    the UTC time its answer was read - an endpoint the check ended before is
    there as "not-asked", with no read time; `NotCorroborated` and `Disagreement` raised
    here carry the same list, and a `Mismatch` raised here is given it as an
    `observations` attribute. Each outcome also says why the check stopped
    and the quorum rule in force, as a `Stop`: `Corroboration.stop`
    ("quorum-reached" or "exhausted"), and `.stop` on a raised `Mismatch`
    ("mismatch"), `Disagreement` ("disagreement") or `NotCorroborated`
    ("exhausted"). `retry_after` (seconds, default 0; a finite int or float
    from 0 to 10**9, refused before any call otherwise) sets when to ask
    again: an unsettled `Corroboration`'s `not_before`, and a raised
    `NotCorroborated`'s `.not_before`, are the moment the check stopped plus
    `retry_after`. Nothing here waits or retries. `clock`, if given, is a no-argument callable
    returning a datetime (naive is read as UTC); it exists so tests can fix
    the times recorded, and changes no decision. It is called twice for each
    endpoint asked, in order: just before the request (`attempted_at`) and
    once it has returned or failed (`read_at`, and `returned_at` for an
    answer).

    `order_seed` (default None) decides the ask order. None asks the endpoints
    in the order given ("as-given"); an int from 0 to 2**64 - 1 asks them in
    the order `ORDER_RULES["seed-sha256"]` describes, which anyone can
    recompute from the record, so a replay with the same seed and the same
    endpoints asks in the same order. There is no other randomness. The order,
    the rule and the seed are on the `Corroboration` (`ask_order`,
    `order_rule`, `order_seed`), every observation carries its `ask_index`,
    and the observations are listed in ask order.

    Raises:
      `Mismatch` - an endpoint has the block confirmed and says it paid a
        different amount, a different account, or was not a send at all. One
        such answer is enough to refuse: a confirmed block's contents are fixed,
        so this is not something a later endpoint can overturn.
      `Disagreement` - two endpoints each reported a confirmed send, and their
        receipts differ. Impossible between honest nodes; nothing is served.
      `NotCorroborated` - fewer than `agree` endpoints answered at all. Nothing
        was checked; hold the call and ask again rather than refuse it.
      `TypeError` - `expect_raw` is not an int, the same refusal `verify` makes,
        made here before any endpoint is called.
    """
    if isinstance(expect_raw, bool) or not isinstance(expect_raw, int):
        # Checked before the first call, not inside the loop: a float
        # expectation is the caller's bug, and no number of nodes repairs it.
        raise TypeError(f"expect_raw must be an int of raw, got {type(expect_raw).__name__}")
    configured = distinct_endpoints(rpc_urls, agree=agree)
    retry_after = _retry_after(retry_after)
    order_seed = _order_seed(order_seed)
    # The planned order, decided once before any call: every endpoint is in it,
    # and one the check ends before keeps its place as "not-asked".
    endpoints = _ask_order(configured, order_seed)
    now = _utc_now if clock is None else clock
    last_moment: list[str] = []  # the most recent moment the clock gave, as written

    def read_clock() -> str:
        # One clock read, as written in the record. A clock that raises
        # StopIteration once it has given a moment - on the return read, the
        # attempt read is reused; on a later node's attempt read, the previous
        # node's last moment - is not an error: the times repeat, the verdict
        # is the one a fresh clock gives. A clock that gives no moment at all
        # still raises.
        try:
            moment = _iso_utc(now())
        except StopIteration:
            if not last_moment:
                raise
            return last_moment[0]
        last_moment[:] = [moment]
        return moment
    ordering = {"configured_order": configured, "ask_order": endpoints, "order_seed": order_seed}

    failures: dict[str, str] = {}
    answered: list[str] = []
    settled_by: dict[tuple, list[str]] = {}
    # Every endpoint that reported the send settled, in the order asked, with
    # its own answer and its own receipt. Kept per endpoint, not per answer:
    # keyed by answer, a later agreeing node's receipt overwrote an earlier
    # one's, and a Disagreement attributed it to the wrong endpoint.
    settled: list[tuple[str, tuple, Receipt]] = []
    # Every endpoint that answered at all, in the order asked, with its answer
    # as text: the settled ones with their receipts, and those that have not
    # seen the send or not yet confirmed it with None, so a Disagreement
    # records every answer before the contradiction, not only the settled ones.
    heard: list[tuple[str, str, Receipt | None]] = []
    # One `Observation` per endpoint asked, unreadable ones included, with the
    # time each answer was read.
    observations: list[Observation] = []
    named_payer = None  # (payer address, endpoint) of the first settled answer naming one

    def stopped(reason: str) -> Stop:
        # The moment the loop stopped is when the last endpoint asked was read.
        last = [seen.read_at for seen in observations if seen.state != "not-asked"][-1]
        return Stop(reason, agree, len(endpoints), last)

    def not_asked(why: str) -> None:
        # Every configured endpoint the check ends before is listed too, as
        # "not-asked", so the record names each configured node exactly once:
        # "never contacted" must not read as "asked and silent". Observations
        # are appended one per endpoint in order, so the rest are not asked.
        for index in range(len(observations), len(endpoints)):
            observations.append(
                Observation(endpoints[index], "not-asked", "", f"not asked: {why}", ask_index=index)
            )

    def disagreement(answers: dict, rpc_url: str, said: str, payload) -> Disagreement:
        not_asked("the check stopped on a disagreement")
        # `answers` names the two sides; `observations` / `observed_answers`
        # keep every endpoint that answered so far, then the one that
        # contradicts them; `node_observations` is the per-node record, with
        # unreadable endpoints and read times.
        witnesses = heard + [(rpc_url, said, payload)]
        observed = {url: text for url, text, _ in witnesses}
        for url, text in answers.items():
            if url in observed:
                observed[url] = text
        raised = Disagreement(
            answers,
            observations={url: receipt for url, _, receipt in witnesses},
            observed_answers=observed,
            node_observations=observations,
        )
        raised.stop = stopped("disagreement")
        return raised

    for index, rpc_url in enumerate(endpoints):
        # The clock is read twice per node asked: before the request is made
        # (`attempted_at`) and once it has returned or failed (`read_at`, and
        # for an answer `returned_at`, the same moment). A wall clock can step
        # backwards between the two reads (NTP, a test clock): the attempt then
        # reads later than the return, and is clamped to it, so no clock ever
        # changes a verdict or makes a valid payment raise. A clock that runs
        # out (raises StopIteration, as a test clock written for one read per
        # node does) after it has given at least one moment does not raise
        # either: each read it cannot answer reuses the last moment it gave.
        attempted_at = read_clock()
        try:
            answer, payload = _answer(block_hash, expect_raw, account, rpc_url)
        except _UNREADABLE as error:
            failures[rpc_url] = f"{type(error).__name__}: {error}"
            failed_at = read_clock()
            observations.append(
                Observation(
                    rpc_url,
                    "unreadable",
                    failed_at,
                    failures[rpc_url],
                    error_kind=type(error).__name__,
                    error_message=str(error),
                    attempted_at=_attempt_no_later(attempted_at, failed_at),
                    ask_index=index,
                )
            )
            continue
        answered.append(rpc_url)
        returned_at = read_clock()
        observations.append(
            Observation(
                rpc_url,
                _STATE_OF_ANSWER[answer[0]],
                returned_at,
                attempted_at=_attempt_no_later(attempted_at, returned_at),
                returned_at=returned_at,
                ask_index=index,
                detail=(
                    f"got {answer[1]}, expected {answer[2]}" if answer[0] == "mismatch"
                    else f"answered for block {answer[1]}, not the requested {answer[2]}"
                    if answer[0] == "other-block" else ""
                ),
                # The payer chain this node's reply named, so the per-node
                # record says which chain each node reported.
                payer_account=(answer[4] or "") if answer[0] == "settled" else "",
            )
        )
        if answer[0] == "other-block":
            # Fail closed, immediately, as for a mismatch. The node reports a
            # confirmed send, but the contents it sent hash to another block:
            # whatever it says about amount and payee is about that block, not
            # this one. Counting it toward agreement would let two nodes
            # "corroborate" a payment by describing two different blocks.
            raise disagreement(
                {"requested": f"block {answer[2]}", rpc_url: f"contents that hash to {answer[1]}"},
                rpc_url, f"contents that hash to {answer[1]}", None,
            )
        if answer[0] == "mismatch":
            # Fail closed, immediately. This endpoint has the block CONFIRMED
            # and reports it paid someone else or a different amount. Blocks do
            # not change after confirmation, so asking another node cannot make
            # this answer go away - it can only find a node that has not caught
            # up yet and read its silence as agreement.
            not_asked("the check stopped on a mismatch")
            payload.observations = tuple(observations)
            payload.stop = stopped("mismatch")
            raise payload
        if answer[0] != "settled":
            heard.append((rpc_url, str(answer), payload))
            continue
        # The payer chain is set apart from what is compared: an answer that
        # names no parseable payer has said nothing about it, so it can neither
        # contradict an answer that does nor be what lets one settle.
        facts, payer = answer[:4], answer[4]
        if settled_by and facts not in settled_by:
            # `answers` is as it always was - the first endpoint to settle and
            # the one that contradicts it - and every endpoint that answered so
            # far is kept beside it, each with what it itself said.
            raise disagreement(
                {settled[0][0]: str(settled[0][1]), rpc_url: str(facts)},
                rpc_url, str(facts), payload,
            )
        if payer is not None and named_payer is not None and payer != named_payer[0]:
            raise disagreement(
                {
                    named_payer[1]: f"{facts} on payer chain {named_payer[0]}",
                    rpc_url: f"{facts} on payer chain {payer}",
                },
                rpc_url, str(facts), payload,
            )
        if payer is not None and named_payer is None:
            named_payer = (payer, rpc_url)
        settled_by.setdefault(facts, []).append(rpc_url)
        settled.append((rpc_url, facts, payload))
        heard.append((rpc_url, str(facts), payload))
        if len(settled_by[facts]) >= agree:
            not_asked(f"quorum of {agree} was reached before its turn")
            return Corroboration(
                # The receipt of the endpoint that completed the quorum, as
                # before; every agreeing receipt asserts the same facts.
                receipt=payload,
                agreed=tuple(settled_by[facts]),
                answered=tuple(answered),
                failed=tuple(failures),
                observations=tuple(observations),
                stop=stopped("quorum-reached"),
                retry_after=retry_after,
                **ordering,
            )

    if len(answered) < agree:
        # Not enough witnesses even spoke. This is NOT an unsettled receipt:
        # "nobody could tell us" and "they told us it is not confirmed yet" are
        # different facts, and only the second one is safe to act on by waiting
        # a little and asking again.
        short = NotCorroborated(
            answered=len(answered), agree=agree, failures=failures, observations=observations
        )
        short.stop = stopped("exhausted")
        short.not_before = _not_before(short.stop.at, retry_after)
        raise short

    # Enough endpoints answered, but fewer than `agree` of them reported the
    # send confirmed - the usual reason being that confirmation has not reached
    # all of them yet. As in `verify`, that is "not yet", not "no". Which node
    # had not seen the block and which had seen it unconfirmed is in
    # `observations`, each with the time it was read.
    return Corroboration(
        receipt=Receipt(settled=False, amount_raw=0, height=0, account=""),
        agreed=(),
        answered=tuple(answered),
        failed=tuple(failures),
        observations=tuple(observations),
        stop=stopped("exhausted"),
        retry_after=retry_after,
        **ordering,
    )


def _figure_of_raw(reply: dict, field: str) -> int:
    """`reply[field]` as an int of raw, or NotANodeReply.

    The rule `nano_payers._int_field` aims at for a history row's `amount`,
    made ASCII-only and applied here to a balance: a string of ASCII digits,
    or an int that is not a bool and not negative. (`_int_field` tests
    `isdigit()` alone, which also passes an Arabic-Indic digit; this does not.) It is stricter than
    `nano_settlement_verify._raw_amount`, which lets an infinite float through
    to int() and its OverflowError; here every float is refused, so that one
    is a failed endpoint like the rest. A node sends raw as a decimal string.
    `int()` of the JSON float 1e30 is 1000000000000000019884624838656 - the
    float cannot represent 10**30, so the figure comes back 19884624838656 raw
    ABOVE the real balance, and a balance read too high is the dangerous
    direction: it says an account can cover a price it cannot. `int()` of the
    JSON `true` is 1. Neither is raw, so neither is a figure to corroborate.
    `int()` also reads "-5", "1_000", " 7 ", "+9" and an Arabic-Indic
    digit; a node sends none of them, so none is read.

    The test next to this one is named "a balance comes back as integers of raw
    not as strings" and its comment already names the 19884624838656-raw error,
    but it feeds the balance as a string, so this path was never exercised.

    A KeyError for a missing field is raised as before and caught by the caller
    as this endpoint not having answered.
    """
    value = reply[field]
    if isinstance(value, bool) or isinstance(value, float):
        raise nano_settlement_verify.NotANodeReply(
            "the reply's %s is %s (%r), not raw: a node sends raw as an integer "
            "string, and int() would read it as another number"
            % (field, type(value).__name__, value)
        )
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise nano_settlement_verify.NotANodeReply(
        "the reply's %s is %r, not raw: a node sends raw as a string of digits"
        % (field, value)
    )


def balance_corroborated(account: str, rpc_urls, agree: int = AGREE_DEFAULT):
    """The confirmed balance and receivable of an account, agreed by `agree` nodes.

    Returns `(balance_raw, receivable_raw)` as integers of raw.

    Only confirmed figures are asked for and compared, because an unconfirmed
    balance is the thing that differs between nodes by design. An endpoint that
    answers without them - a proxy that drops `include_confirmed`, say - counts
    as not having answered rather than being read for its unconfirmed `balance`.
    An account no endpoint has seen opened reads as `(0, 0)`: that is a real
    answer, and the agreement rule applies to it like any other.

    Confirmed figures still move, so two honest nodes a block apart disagree
    here. That is the intended outcome and the remedy is to ask again, not to
    take the larger number.

    Any node error other than "Account not found" counts as a failed endpoint,
    a figure that is not an ASCII digit string or a plain non-negative JSON
    integer is refused, and a malformed address raises `ValueError`.

    Raises `Disagreement` when two endpoints report different confirmed figures,
    and `NotCorroborated` when fewer than `agree` endpoints answer.
    Raises `ValueError`, before any endpoint is called, when `account` is not a
    Nano address with a valid checksum.
    """
    if not nano_settlement_verify.is_valid_account(account):
        # Checked before the first call, and raised as the caller's own error
        # rather than as NotCorroborated. A node answers a mistyped address
        # with "Bad account number", which the loop below would count as a
        # failed endpoint; NotCorroborated tells the caller to ask again, and
        # asking again about an address that cannot exist never ends.
        raise ValueError(
            "account %r is not a Nano address: it needs a nano_ or xrb_ prefix "
            "and a valid checksum" % (account,)
        )
    endpoints = distinct_endpoints(rpc_urls, agree=agree)
    post = nano_settlement_verify.post_json  # looked up per call so a stub takes effect
    payload = {
        "action": "account_info",
        "account": account,
        "receivable": "true",
        "include_confirmed": "true",
    }
    failures: dict[str, str] = {}
    figures: dict[tuple, list[str]] = {}
    reported: list[tuple[str, tuple]] = []  # every endpoint's figure, in order asked
    for rpc_url in endpoints:
        try:
            reply = post(rpc_url, payload)
            if not isinstance(reply, dict):
                # The same refusal `verify` makes, for the same reason. This
                # function reads a reply directly rather than through `verify`,
                # so it never got the guard PR #27 added there, and
                # `"error" in reply` on a string is a SUBSTRING test: any
                # proxy body with the word "error" in it took the branch below
                # and counted as an endpoint answering a confirmed balance of
                # nothing. Two such endpoints corroborated a balance of
                # nothing for an account that may hold a fortune. A list did
                # the same by membership. README.md already promises the
                # opposite: "An endpoint whose reply is not a JSON object (a
                # proxy's `"error"`, a bare number) is silence too: it counts
                # as failed, not as answered."
                raise nano_settlement_verify.NotANodeReply(
                    "the endpoint's reply is %s, not a JSON object, so it is not a "
                    "node's account_info reply and says nothing about this balance"
                    % type(reply).__name__
                )
            if reply.get("error") == "Account not found":
                # An unopened account is not a failure to read; it is a balance
                # of nothing, and two nodes can agree on that. The check has to
                # come first: a node answering "Account not found" sends a
                # `balance` of "0" alongside the error, so reading the figure
                # before the error would turn an unknown account into a real
                # zero without saying so.
                figure = (0, 0)
            elif "error" in reply:
                # Any other error - "Rate limit exceeded", "Invalid API key" -
                # is an endpoint that did not answer, not an unopened account:
                # read as (0, 0), two of them corroborated a balance of nothing
                # for an account that may hold a fortune. `nano_claim._opened`
                # draws the same line. An `error` key that is null or empty
                # next to figures is refused too: a node's account_info reply
                # carries no `error` key at all, so this one is not a node's.
                raise nano_settlement_verify.NotANodeReply(
                    "the endpoint answered error %r, so it said nothing about "
                    "this balance" % (reply["error"],)
                )
            else:
                # Only the CONFIRMED figures, and no falling back to `balance`.
                # A node that ignores `include_confirmed` answers with `balance`
                # alone - the unconfirmed figure, which is precisely the number
                # that differs between nodes and must not be trusted. Falling
                # back to it would answer a question nobody asked while looking
                # like agreement, so its absence is read as this endpoint not
                # having answered: KeyError here is caught just below.
                figure = (
                    _figure_of_raw(reply, "confirmed_balance"),
                    _figure_of_raw(reply, "confirmed_receivable"),
                )
        except _UNREADABLE as error:
            failures[rpc_url] = f"{type(error).__name__}: {error}"
            continue
        if figures and figure not in figures:
            witnesses = reported + [(rpc_url, figure)]
            raise Disagreement(
                {reported[0][0]: str(reported[0][1]), rpc_url: str(figure)},
                observations={url: said for url, said in witnesses},
                observed_answers={url: str(said) for url, said in witnesses},
            )
        figures.setdefault(figure, []).append(rpc_url)
        reported.append((rpc_url, figure))
        if len(figures[figure]) >= agree:
            return figure
    raise NotCorroborated(
        answered=sum(len(urls) for urls in figures.values()), agree=agree, failures=failures
    )


def replay_receipt(record) -> dict:
    """Re-run, from a stored `to_json()` record alone, who was asked and why it stopped.

    `record` is a `to_json()` string or the dict it parses to. From its
    "configured_order", "order_rule" and "order_seed" this recomputes the ask
    order and compares it with "ask_order"; then it walks the observations in
    that order with the quorum rule in "stop" (k of n), exactly as
    `verify_corroborated` does: a "mismatch" stops it (contents for another
    block, or two confirmed nodes naming different payer chains, stop it as
    "disagreement"), the k-th "confirmed" stops it as "quorum-reached", and
    running out is "exhausted". Every node after the stop must be "not-asked",
    every node up to it must not be, and the record's "stop" reason and
    moment, "settled" and "agreed" must be what that walk gives.

    Returns `{"verdict": "match" | "mismatch", "problems": [...],
    "expected_stop": reason or None, "expected_not_asked": [...]}`. It checks
    order and stopping only - not amounts, not signatures, not whether any
    node told the truth. It does not raise on a well-formed JSON object; a
    string that is not JSON raises ValueError.
    """
    if isinstance(record, (str, bytes)):
        record = json.loads(record)
    problems: list[str] = []

    def result(reason=None, not_asked=()):
        return {
            "verdict": "mismatch" if problems else "match",
            "problems": problems,
            "expected_stop": reason,
            "expected_not_asked": list(not_asked),
        }

    if not isinstance(record, dict):
        problems.append("the record is not a JSON object")
        return result()
    configured, order = record.get("configured_order"), record.get("ask_order")
    seen, stop = record.get("observations"), record.get("stop")
    quorum = stop.get("quorum") if isinstance(stop, dict) else None
    if not (
        isinstance(configured, list) and isinstance(order, list) and isinstance(seen, list)
        and isinstance(quorum, dict) and all(isinstance(entry, dict) for entry in seen)
    ):
        problems.append("the record lacks configured_order, ask_order, observations or stop")
        return result()
    k, n = quorum.get("k"), quorum.get("n")
    if isinstance(k, bool) or not isinstance(k, int) or isinstance(n, bool) or not isinstance(n, int):
        problems.append("stop.quorum k and n are not ints")
        return result()

    # 1. The ask order follows from the configured order, the rule and the seed.
    rule, seed = record.get("order_rule"), record.get("order_seed")
    if rule not in ORDER_RULES or (rule == "as-given") != (seed is None):
        problems.append(f"order_rule {rule!r} with order_seed {seed!r} is not a rule this schema has")
    else:
        try:
            planned = list(_ask_order(configured, _order_seed(seed)))
        except (TypeError, ValueError) as error:
            planned = None
            problems.append(f"the ask order cannot be recomputed: {error}")
        if planned is not None and planned != order:
            problems.append(f"ask_order is not the order {rule!r} gives from configured_order")
    if sorted(map(str, order)) != sorted(map(str, configured)):
        problems.append("ask_order and configured_order do not name the same nodes")
    if not len(order) == len(seen) == n:
        problems.append(f"{len(order)} in ask_order, {len(seen)} observations, n is {n}")

    # 2. Each observation sits at its ask_index and names the node at that place.
    for position, entry in enumerate(seen):
        index = entry.get("ask_index")
        if index != position or position >= len(order) or entry.get("rpc_url") != order[position]:
            problems.append(
                f"observation {position} ({entry.get('rpc_url')!r}, ask_index {index!r}) "
                "is not the node at that place in ask_order"
            )

    # 3. Walk the answers in ask order with the quorum rule, as the check did.
    reason, at, agreed, payers = "exhausted", len(seen) - 1, [], set()
    for position, entry in enumerate(seen):
        state = entry.get("state")
        if state == "mismatch":
            other = str(entry.get("detail", "")).startswith("answered for block")
            reason, at = ("disagreement" if other else "mismatch"), position
            break
        if state == "confirmed":
            if entry.get("payer_account"):
                payers.add(entry["payer_account"])
            if len(payers) > 1:
                reason, at = "disagreement", position
                break
            agreed.append(entry.get("rpc_url"))
            if len(agreed) >= k:
                reason, at = "quorum-reached", position
                break
    expected_not_asked = [entry.get("rpc_url") for entry in seen[at + 1:]]
    for position, entry in enumerate(seen):
        asked = entry.get("state") != "not-asked"
        if asked != (position <= at):
            problems.append(
                f"observation {position} ({entry.get('rpc_url')!r}) is {entry.get('state')!r}, "
                f"but the replay {'asks' if position <= at else 'does not ask'} it"
            )
    if stop.get("reason") != reason:
        problems.append(f"stop.reason is {stop.get('reason')!r}, the replay stops on {reason!r}")
    if seen and stop.get("at") != seen[at].get("read_at"):
        problems.append(f"stop.at is {stop.get('at')!r}, the last node asked was read at {seen[at].get('read_at')!r}")
    if "settled" in record and record["settled"] is not (reason == "quorum-reached"):
        problems.append(f"settled is {record['settled']!r}, the replay stops on {reason!r}")
    if "agreed" in record and record["agreed"] != (agreed if reason == "quorum-reached" else []):
        problems.append("agreed is not the confirmed nodes the replay counts toward the quorum")
    return result(reason, expected_not_asked)


def _main(argv=None) -> int:
    """`python3 nano_quorum.py --replay RECEIPT.json`: exit 0 on a match, 1 on a mismatch."""
    import argparse

    parser = argparse.ArgumentParser(prog="nano_quorum.py", description=_main.__doc__)
    parser.add_argument("--replay", metavar="RECEIPT", required=True,
                        help="a stored Corroboration.to_json() record ('-' reads stdin)")
    args = parser.parse_args(argv)
    if args.replay == "-":
        text = sys.stdin.read()
    else:
        with open(args.replay, encoding="utf-8") as handle:
            text = handle.read()
    out = replay_receipt(text)
    print(json.dumps(out, indent=2))
    return 0 if out["verdict"] == "match" else 1


if __name__ == "__main__":
    sys.exit(_main())
