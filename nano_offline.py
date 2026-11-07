"""Check a Nano block offline: the payer's own signature, and the step from the block before.

What this proves, with no node asked:

1. A Nano block's signature is ed25519 - with blake2b-512 in place of sha512 -
   made by the account's own key over the block hash. The hash is recomputed
   here from the block's json contents (`block_hash_of`), so a valid signature
   means the holder of that account's key signed exactly these contents. Change
   one digit of the balance, the link or the representative and it fails.

2. `previous` is already inside the hashed contents, so carrying the previous
   hash next to a receipt adds nothing on its own. What the previous block buys
   is the AMOUNT: a state block only states the new balance, so the amount sent
   is the previous block's balance minus this one. Given the previous block's
   contents as well, `offline_check` verifies hash(previous contents) ==
   contents.previous, that the previous block belongs to the same account
   (state blocks name their account; a legacy previous names none and is
   refused - fail closed), and that this block's signature is valid. Only then
   does it report the balance change; otherwise amount_raw is null.

What this does not prove: that the block is on the ledger, or confirmed
(cemented). A key holder can sign a block that never lands, or one that loses a
fork. Inclusion still needs nodes - see nano_quorum.

    python3 nano_offline.py <block_hash> [rpc_url]

fetches block_info (json_block=true) for the block and its previous from one
node (default https://rpc.nano.to) and prints `offline_check` on the fetched
contents as JSON. The node is only used to fetch bytes; the check runs on them.
Exit 0 when the signature is valid and the previous block, if any, matches
and belongs to the same account. A previous block that is not a state block
(legacy chains) is refused rather than guessed at: fail closed.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys

from nano_five_fields import block_hash_of
from nano_settlement_verify import post_json, public_key_from_address

DEFAULT_RPC = "https://rpc.nano.to"
NOTE = "offline: proves the account signed these contents; inclusion on the ledger still needs nodes (nano_quorum)"
_HEX128 = re.compile(r"[0-9A-Fa-f]{128}")
_DEC = re.compile(r"[0-9]{1,39}")  # ASCII digits only; 2**128-1 has 39
_MAX_RAW = 2**128 - 1

# --- ed25519 verify (RFC 8032 section 5.1, extended coordinates), hash = blake2b-512 ---
P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, P - 2, P) % P
_SQRT_M1 = pow(2, (P - 1) // 4, P)


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A, B = (y1 - x1) * (y2 - x2) % P, (y1 + x1) * (y2 + x2) % P
    C, D = 2 * t1 * t2 * _D % P, 2 * z1 * z2 % P
    E, F, G, H = B - A, D - C, D + C, B + A
    return E * F % P, G * H % P, F * G % P, E * H % P


def _mul(s, pt):
    acc = (0, 1, 1, 0)
    while s:
        if s & 1:
            acc = _add(acc, pt)
        pt, s = _add(pt, pt), s >> 1
    return acc


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def _decode(raw: bytes):
    """A 32-byte encoded point, or None if it encodes no point on the curve."""
    y = int.from_bytes(raw, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, P - 2, P) % P
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * _SQRT_M1 % P
    if (x * x - x2) % P or (x == 0 and sign):
        return None
    if x & 1 != sign:
        x = P - x
    return x, y, 1, x * y % P


_GY = 4 * pow(5, P - 2, P) % P
_BASE = _decode(_GY.to_bytes(32, "little"))


_IDENTITY = (0, 1, 1, 0)


def _small_order(pt) -> bool:
    """8*pt is the identity: the identity itself or one of the 7 other torsion points."""
    return _equal(_mul(8, pt), _IDENTITY)


def _ed25519_blake2b_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    A, R = _decode(public_key), _decode(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if A is None or R is None or s >= L:  # non-canonical S is rejected, not reduced
        return False
    # A small-order key (e.g. the identity, account nano_11a1...) or R lets a
    # forged signature verify for any contents: refuse both. Non-canonical
    # encodings (y >= p, x = 0 with the sign bit set) are refused by _decode.
    if _small_order(A) or _small_order(R):
        return False
    h = int.from_bytes(hashlib.blake2b(signature[:32] + public_key + message, digest_size=64).digest(), "little") % L
    return _equal(_mul(s, _BASE), _add(R, _mul(h, A)))


# --- the block checks ---

def verify_block_signature(contents: object, signature_hex: object, account: str | None = None) -> bool:
    """Whether the account's own key signed exactly these block contents. Never raises.

    State blocks name their account; legacy send blocks do not, so pass
    `account=` for those (False without it). `account=` on a state block must
    agree with contents["account"].
    """
    try:
        if not isinstance(contents, dict) or not (isinstance(signature_hex, str) and _HEX128.fullmatch(signature_hex)):
            return False
        named = contents.get("account") if contents.get("type") == "state" else None
        if named is not None and account is not None and account != named:
            return False
        key = public_key_from_address(named if named is not None else account)
        block_hash = block_hash_of(contents)
        if key is None or block_hash is None:
            return False
        return _ed25519_blake2b_verify(key, bytes.fromhex(block_hash), bytes.fromhex(signature_hex))
    except Exception:  # garbage in is a failed check, not a crash
        return False


def _balance(contents: dict) -> int | None:
    raw = contents.get("balance")
    if contents.get("type") == "state" and isinstance(raw, str) and _DEC.fullmatch(raw):
        value = int(raw)
        return value if value <= _MAX_RAW else None
    if contents.get("type") == "send" and isinstance(raw, str) and re.fullmatch(r"[0-9A-Fa-f]{32}", raw):
        return int(raw, 16)
    return None


def _account_key(contents: object, account: str | None = None) -> bytes | None:
    """The public key a block belongs to: named by a state block, else `account=`, else None."""
    if not isinstance(contents, dict):
        return None
    named = contents.get("account") if contents.get("type") == "state" else account
    return public_key_from_address(named)


def offline_check(contents: object, signature_hex: object, previous_contents: object = None,
                  account: str | None = None) -> dict:
    """Everything the block's own bytes prove, without a node.

    previous_matches: None when no previous block was given; True when its
    recomputed hash equals contents["previous"]; False otherwise.
    previous_same_account: None when no previous block was given; True only
    when the previous block is a state block of the same account (a legacy
    previous names no account and is refused: fail closed).
    balance_change_raw: this balance minus the previous balance, only when
    signature_valid, previous_matches and previous_same_account are all True -
    negative for a send, positive for a receive.
    amount_raw: the size of that change (always >= 0), or None.
    Neither says the block is on the ledger: see NOTE.
    """
    out = {"block_hash": block_hash_of(contents),
           "signature_valid": verify_block_signature(contents, signature_hex, account),
           "previous_matches": None, "previous_same_account": None,
           "balance_change_raw": None, "amount_raw": None, "note": NOTE}
    if previous_contents is None:
        return out
    prev_hash = block_hash_of(previous_contents)
    out["previous_matches"] = bool(prev_hash and isinstance(contents, dict)
                                   and str(contents.get("previous", "")).upper() == prev_hash)
    own = _account_key(contents, account)
    before_key = _account_key(previous_contents) if isinstance(previous_contents, dict) \
        and previous_contents.get("type") == "state" else None
    out["previous_same_account"] = bool(own is not None and before_key is not None and own == before_key)
    if out["signature_valid"] and out["previous_matches"] and out["previous_same_account"]:
        now, before = _balance(contents), _balance(previous_contents)
        if now is not None and before is not None:
            out["balance_change_raw"] = now - before
            out["amount_raw"] = abs(now - before)
    return out


def _fetch(rpc_url: str, block_hash: str) -> dict:
    reply = post_json(rpc_url, {"action": "block_info", "json_block": "true", "hash": block_hash})
    contents = reply.get("contents") if isinstance(reply, dict) else None
    if not isinstance(contents, dict):
        raise ValueError(f"no block contents for {block_hash}: {reply!r}")
    return contents


def main(argv: list[str]) -> int:
    if not argv or len(argv) > 2:
        print(__doc__.strip().splitlines()[0], "\nusage: python3 nano_offline.py <block_hash> [rpc_url]", file=sys.stderr)
        return 2
    block_hash, rpc_url = argv[0].upper(), argv[1] if len(argv) > 1 else DEFAULT_RPC
    contents = _fetch(rpc_url, block_hash)
    previous = contents.get("previous", "")
    prev_contents = None if set(str(previous)) <= {"0"} else _fetch(rpc_url, previous)  # an open block has none
    result = offline_check(contents, contents.get("signature"), prev_contents)
    result["requested_hash_matches"] = result["block_hash"] == block_hash
    print(json.dumps(result, indent=2))
    ok = (result["signature_valid"] and result["previous_matches"] is not False
          and result["previous_same_account"] is not False and result["requested_hash_matches"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
