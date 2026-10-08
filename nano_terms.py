"""Pay against terms both sides hold, not against a claim.

Two agents that trade once need three things settled before any XNO moves:
what is paid, to whom, and what counts as delivered. This module pins them.

- The terms are addressed by the sha256 of their own bytes. Each side re-derives
  the hash from the bytes it holds and compares it to the one cited, so checking
  a citation needs no registry, no lookup and nobody else online.
- The schema is fixed: `version`, `payee`, `amount_raw`, `task`, `acceptance`,
  and nothing more. A field this module does not know is refused rather than
  ignored, because an ignored field is a term one side thinks it agreed to.
  `payee` is checked against its own checksum, not only its prefix: a Nano send
  is irreversible, so an address that cannot be paid is refused here and not by
  money that leaves and never arrives.
- Acceptance is a machine check written into the terms before payment: either
  the deliverable's sha256, or a list of keys a JSON object must carry. There is
  no "the buyer decides" kind; a check a program cannot run is renegotiation.
- Settlement is verified against the payee and amount the terms pinned, through
  `nano_settlement_verify.verify` - never against what the seller says later.

It reads and hashes. It never signs, never sends and holds no key: paying is the
buyer's own wallet's job, and happens between `accept` and `settle`.

    terms = load_terms(terms_bytes, cited_sha256)   # refuses on any mismatch
    done = accept(terms, deliverable_bytes)         # offline; raises NotAccepted
    # ... the buyer pays terms.amount_raw to terms.payee, gets a block hash ...
    deal = settle(terms, done, block_hash, rpc_url)
    deal.to_json()                                  # cites both hashes and the block
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

import nano_settlement_verify

__all__ = [
    "Terms",
    "Acceptance",
    "Deal",
    "TermsRefused",
    "NotAccepted",
    "terms_sha256",
    "load_terms",
    "accept",
    "settle",
]

VERSION = "1"
FIELDS = frozenset({"version", "payee", "amount_raw", "task", "acceptance"})
TASK_MAX_CHARS = 2000

_HEX64 = re.compile(r"[0-9a-f]{64}")
# Raw is an integer string with no sign, no exponent and no padding: "1e24" or
# " 12" would parse somewhere and mean something else somewhere else.
_RAW = re.compile(r"[1-9][0-9]*")


class TermsRefused(Exception):
    """The terms cannot be relied on. `code` says why, in one word."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class NotAccepted(Exception):
    """The deliverable fails the acceptance check the terms carry."""


@dataclass(frozen=True)
class Terms:
    sha256: str
    payee: str
    amount_raw: int
    task: str
    acceptance: tuple  # ("sha256", hex) or ("json_keys", (key, ...))


@dataclass(frozen=True)
class Acceptance:
    """Proof that a deliverable passed the check of one set of terms."""

    terms_sha256: str
    deliverable_sha256: str


@dataclass(frozen=True)
class Deal:
    """The record both sides can publish: terms, deliverable and payment, by hash."""

    terms_sha256: str
    deliverable_sha256: str
    block_hash: str
    settled: bool
    amount_raw: int
    payee: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "terms_sha256": self.terms_sha256,
                "deliverable_sha256": self.deliverable_sha256,
                "block_hash": self.block_hash,
                "settled": self.settled,
                "amount_raw": self.amount_raw,
                "payee": self.payee,
            }
        )


def terms_sha256(terms_bytes: bytes) -> str:
    """The address of the terms: the sha256 of the exact bytes, lower-case hex."""
    return hashlib.sha256(terms_bytes).hexdigest()


def _acceptance(value: object) -> tuple:
    if not isinstance(value, dict) or len(value) != 1:
        raise TermsRefused("invalid_acceptance", "acceptance must name exactly one check")
    (kind, arg), = value.items()
    if kind == "sha256":
        if not isinstance(arg, str) or not _HEX64.fullmatch(arg.lower()):
            raise TermsRefused("invalid_acceptance", "sha256 must be 64 hex characters")
        return ("sha256", arg.lower())
    if kind == "json_keys":
        if not isinstance(arg, list) or not arg or not all(isinstance(k, str) and k for k in arg):
            raise TermsRefused("invalid_acceptance", "json_keys must be a non-empty list of names")
        return ("json_keys", tuple(arg))
    raise TermsRefused("invalid_acceptance", f"unknown check {kind!r}; known: sha256, json_keys")


def _no_duplicates(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        # Parsers disagree on which duplicate wins, so the two sides could read
        # different terms out of the very bytes whose hash they both checked.
        raise TermsRefused("duplicate_field", ", ".join(dup))
    return dict(pairs)


def load_terms(terms_bytes: bytes, cited_sha256: str) -> Terms:
    """Parse terms, refusing them unless they hash to the cited value and fit the schema."""
    actual = terms_sha256(terms_bytes)
    if not isinstance(cited_sha256, str) or cited_sha256.lower() != actual:
        raise TermsRefused("hash_mismatch", f"these bytes hash to {actual}, not {cited_sha256}")
    try:
        doc = json.loads(terms_bytes.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError):
        raise TermsRefused("not_json", "terms must be a UTF-8 JSON object") from None
    if not isinstance(doc, dict):
        raise TermsRefused("not_json", "terms must be a UTF-8 JSON object")
    unknown = sorted(set(doc) - FIELDS)
    if unknown:
        raise TermsRefused("unknown_field", f"not part of version {VERSION}: {', '.join(unknown)}")
    missing = sorted(FIELDS - set(doc))
    if missing:
        raise TermsRefused("missing_field", ", ".join(missing))
    if doc["version"] != VERSION:
        raise TermsRefused("unknown_version", f"only version {VERSION!r} is understood")
    payee = doc["payee"]
    # The prefix alone used to be the whole check, so `"nano_1a"` was accepted as
    # the pinned payee of terms both sides hash and rely on - and the buyer then
    # pays `terms.amount_raw` to it with its own wallet, irreversibly, to an
    # address that names no account. A Nano address carries a blake2b checksum of
    # its own public key precisely so that this can be caught without asking
    # anyone, so it is checked here rather than discovered by a send that never
    # arrives. This refuses a payee; it never accepts one it refused before.
    if not nano_settlement_verify.is_valid_account(payee):
        raise TermsRefused(
            "invalid_payee",
            "payee must be a nano_ (or xrb_) address whose checksum matches:"
            " XNO sent to an address that fails it does not arrive and cannot be recalled",
        )
    amount = doc["amount_raw"]
    if not isinstance(amount, str) or not _RAW.fullmatch(amount):
        raise TermsRefused("invalid_amount", "amount_raw must be a positive decimal string of raw")
    task = doc["task"]
    if not isinstance(task, str) or not task.strip() or len(task) > TASK_MAX_CHARS:
        raise TermsRefused("invalid_task", f"task must say in words what is bought (1-{TASK_MAX_CHARS} chars)")
    return Terms(
        sha256=actual,
        payee=payee,
        amount_raw=int(amount),
        task=task,
        acceptance=_acceptance(doc["acceptance"]),
    )


def accept(terms: Terms, deliverable: bytes) -> Acceptance:
    """Run the terms' own check on the deliverable. Offline; raises NotAccepted."""
    digest = hashlib.sha256(deliverable).hexdigest()
    kind, arg = terms.acceptance
    if kind == "sha256":
        if digest != arg:
            raise NotAccepted(f"deliverable hashes to {digest}, terms require {arg}")
    elif kind == "json_keys":
        try:
            doc = json.loads(deliverable.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise NotAccepted("deliverable is not JSON") from None
        if not isinstance(doc, dict):
            raise NotAccepted("deliverable is not a JSON object")
        empty = [k for k in arg if doc.get(k) in (None, "")]
        if empty:
            raise NotAccepted(f"deliverable lacks {', '.join(empty)}")
    else:  # load_terms admits no other kind; refuse rather than pass one by
        raise NotAccepted(f"unknown check {kind!r}")
    return Acceptance(terms_sha256=terms.sha256, deliverable_sha256=digest)


def settle(terms: Terms, acceptance: Acceptance, block_hash: str, rpc_url: str) -> Deal:
    """Verify the payment against the payee and amount the terms pinned.

    Raises TermsRefused when the acceptance belongs to other terms, and whatever
    `nano_settlement_verify.verify` raises (NotFound, Mismatch) for the payment.
    An unconfirmed block gives a Deal with settled=False: not yet, not failed.
    As with `verify`, spending one block hash once is the caller's to enforce.
    """
    if acceptance.terms_sha256 != terms.sha256:
        raise TermsRefused("acceptance_of_other_terms", "the acceptance was made against other terms")
    receipt = nano_settlement_verify.verify(block_hash, terms.amount_raw, terms.payee, rpc_url)
    return Deal(
        terms_sha256=terms.sha256,
        deliverable_sha256=acceptance.deliverable_sha256,
        block_hash=block_hash,
        settled=receipt.settled,
        amount_raw=receipt.amount_raw if receipt.settled else 0,
        payee=receipt.account if receipt.settled else terms.payee,
    )
