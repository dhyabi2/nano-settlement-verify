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

import json
import math
import sys
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass

import nano_settlement_verify
from nano_settlement_verify import Mismatch, NotFound, Receipt

__all__ = [
    "AGREE_DEFAULT",
    "Corroboration",
    "Disagreement",
    "NotCorroborated",
    "balance_corroborated",
    "distinct_endpoints",
    "post_json_failover",
    "verify_corroborated",
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

    def __init__(self, answered: int, agree: int, failures: dict) -> None:
        detail = "; ".join(f"{url}: {why}" for url, why in failures.items())
        super().__init__(
            f"{answered} of {answered + len(failures)} endpoints answered, "
            f"{agree} needed to agree ({detail})"
        )
        self.answered = answered
        self.agree = agree
        self.failures = dict(failures)


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
    ) -> None:
        answers = _mapping("answers", answers)
        detail = "; ".join(f"{_text(url)}: {_text(answer)}" for url, answer in answers.items())
        super().__init__(f"endpoints disagree: {detail}")
        self.answers = answers
        self.observations = _mapping("observations", observations)
        self.observed_answers = _mapping("observed_answers", observed_answers)

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
        return "<unprintable %s>" % _cap(_type_name(value), "str")


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
            f"got {type(value).__name__}"
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
      raw is, so it stays exact) and finite floats are kept; NaN and infinity become their `repr` text.
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
        return float(value) if math.isfinite(value) else repr(value)
    if isinstance(value, str):
        return _cap(str.__str__(value), "str")
    if id(value) in active:
        return "<cycle %s>" % type(value).__name__
    if depth >= _MAX_DEPTH:
        return "<too deep %s>" % type(value).__name__
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
    try:
        return str(type(value).__name__)
    except Exception:
        return "object"


def _capped_repr(value) -> str:
    """`_repr(value)` cut to `_MAX_STR_CHARS` characters as kind `"repr"`; never raises."""
    try:
        return _cap(str.__str__(_repr(value)), "repr")
    except Exception:
        return "<unrepresentable %s>" % _cap(_type_name(value), "str")


def _repr(value) -> str:
    """`repr(value)`, or a placeholder when even that raises."""
    try:
        return str(repr(value))
    except Exception:
        try:
            name = _cap(str.__str__(type(value).__name__), "str")
        except Exception:
            name = "object"
        return "<unrepresentable %s>" % name


@dataclass(frozen=True)
class Corroboration:
    """A receipt, and which endpoints stood behind it.

    `receipt.settled` is True only when `agree` endpoints independently reported
    the same confirmed send. An unsettled receipt here means the endpoints that
    answered did not get there - the block is not confirmed yet, or not enough
    of them have seen it - and, exactly as in `verify`, that is "not yet", not
    "no".
    """

    receipt: Receipt
    agreed: tuple[str, ...]
    answered: tuple[str, ...]
    failed: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "settled": self.receipt.settled,
                "amount_raw": self.receipt.amount_raw,
                "height": self.receipt.height,
                "account": self.receipt.account,
                "agreed": list(self.agreed),
                "answered": list(self.answered),
                "failed": list(self.failed),
            }
        )


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


def _answer(block_hash: str, expect_raw: int, account: str, rpc_url: str):
    """One endpoint's answer, as something two answers can be compared by.

    A settled receipt becomes the facts it asserts; everything else becomes a
    tag. `Mismatch` is carried rather than raised here so the caller can decide
    what one node's refusal is worth.
    """
    verify = nano_settlement_verify.verify  # looked up per call so a stub takes effect
    try:
        receipt = verify(block_hash, expect_raw, account, rpc_url)
    except NotFound:
        return ("not-seen",), None
    except Mismatch as error:
        return ("mismatch", repr(error.got), repr(error.expected)), error
    if not receipt.settled:
        return ("not-yet",), None
    return ("settled", receipt.amount_raw, receipt.height, receipt.account), receipt


def verify_corroborated(
    block_hash: str,
    expect_raw: int,
    account: str,
    rpc_urls,
    agree: int = AGREE_DEFAULT,
) -> Corroboration:
    """Prove a send settled, believing it only when `agree` endpoints concur.

    Asks the endpoints in the order given and stops as soon as `agree` of them
    have reported the SAME confirmed send - so an endpoint listed after the
    quorum is not called at all, and the list should be ordered by how much the
    caller trusts it. Endpoints that cannot be read are skipped, not retried.

    Returns a `Corroboration`. Its receipt is settled only on agreement.

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
    endpoints = distinct_endpoints(rpc_urls, agree=agree)

    failures: dict[str, str] = {}
    answered: list[str] = []
    settled_by: dict[tuple, list[str]] = {}
    # Every endpoint that reported the send settled, in the order asked, with
    # its own answer and its own receipt. Kept per endpoint, not per answer:
    # keyed by answer, a later agreeing node's receipt overwrote an earlier
    # one's, and a Disagreement attributed it to the wrong endpoint.
    settled: list[tuple[str, tuple, Receipt]] = []
    # Every endpoint that answered at all, in the order asked: the settled ones
    # with their receipts, and those that have not seen the send or not yet
    # confirmed it with None, so a Disagreement records every answer before
    # the contradiction, not only the settled ones.
    heard: list[tuple[str, tuple, Receipt | None]] = []

    for rpc_url in endpoints:
        try:
            answer, payload = _answer(block_hash, expect_raw, account, rpc_url)
        except _UNREADABLE as error:
            failures[rpc_url] = f"{type(error).__name__}: {error}"
            continue
        answered.append(rpc_url)
        if answer[0] == "mismatch":
            # Fail closed, immediately. This endpoint has the block CONFIRMED
            # and reports it paid someone else or a different amount. Blocks do
            # not change after confirmation, so asking another node cannot make
            # this answer go away - it can only find a node that has not caught
            # up yet and read its silence as agreement.
            raise payload
        if answer[0] != "settled":
            heard.append((rpc_url, answer, payload))
            continue
        if settled_by and answer not in settled_by:
            # `answers` is as it always was - the first endpoint to settle and
            # the one that contradicts it - and every endpoint that answered so
            # far is kept beside it, each with what it itself said, then the
            # one that contradicts them.
            witnesses = heard + [(rpc_url, answer, payload)]
            raise Disagreement(
                {settled[0][0]: str(settled[0][1]), rpc_url: str(answer)},
                observations={url: receipt for url, _, receipt in witnesses},
                observed_answers={url: str(said) for url, said, _ in witnesses},
            )
        settled_by.setdefault(answer, []).append(rpc_url)
        settled.append((rpc_url, answer, payload))
        heard.append((rpc_url, answer, payload))
        if len(settled_by[answer]) >= agree:
            return Corroboration(
                # The receipt of the endpoint that completed the quorum, as
                # before; every agreeing receipt asserts the same facts.
                receipt=payload,
                agreed=tuple(settled_by[answer]),
                answered=tuple(answered),
                failed=tuple(failures),
            )

    if len(answered) < agree:
        # Not enough witnesses even spoke. This is NOT an unsettled receipt:
        # "nobody could tell us" and "they told us it is not confirmed yet" are
        # different facts, and only the second one is safe to act on by waiting
        # a little and asking again.
        raise NotCorroborated(answered=len(answered), agree=agree, failures=failures)

    # Enough endpoints answered, but fewer than `agree` of them reported the
    # send confirmed - the usual reason being that confirmation has not reached
    # all of them yet. As in `verify`, that is "not yet", not "no".
    return Corroboration(
        receipt=Receipt(settled=False, amount_raw=0, height=0, account=""),
        agreed=(),
        answered=tuple(answered),
        failed=tuple(failures),
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
