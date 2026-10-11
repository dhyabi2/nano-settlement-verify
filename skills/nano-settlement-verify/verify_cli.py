#!/usr/bin/env python3
"""Command-line front end for nano_settlement_verify.verify: one JSON verdict on stdout.

usage: python3 verify_cli.py BLOCK_HASH EXPECT_RAW ACCOUNT [RPC_URL] [--not-after WHEN]
WHEN is unix seconds or ISO-8601 with an offset (2026-10-09T18:00:00Z); inclusive.
RPC_URL defaults to $NANO_RPC_URL, else https://rpc.nano.to; the argument wins over both.
exit 0 settled, 2 not confirmed yet, 3 mismatch, no such block or an amount that is not raw,
      4 node unreachable (nothing was checked), 5 late (seen after WHEN),
      6 unknown_time (node reports no time for the block; not on time), 64 usage
The time is the node's own local_timestamp, not signed by the sender: read it on two nodes
if seconds matter.
"""
import http.client
import json
import os
import re
import sys

from nano_settlement_verify import (
    TIME_SOURCE, Late, Mismatch, NotFound, UnknownTime, parse_not_after, verify,
)


def _take_not_after(argv):
    """argv without --not-after, and its value (None when absent)."""
    rest, value, i = [], None, 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--not-after" and i + 1 < len(argv):
            value, i = argv[i + 1], i + 2
            continue
        if arg.startswith("--not-after="):
            value = arg.split("=", 1)[1]
        else:
            rest.append(arg)
        i += 1
    return rest, value


DEFAULT_RPC_URL = "https://rpc.nano.to"

# `scheme://user:password@host` - the userinfo is a credential, and it must not
# reach stdout or stderr, where a seller's logs keep it. urllib's messages quote
# pieces of the URL back without its scheme (InvalidURL says "nonnumeric port:
# 'pw@127.0.0.1'" for `http://user:pw@127.0.0.1/`), so the URL's own userinfo is
# removed wherever it appears, as well as any `scheme://...@` in the text.
_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/?#\s]*@")


def _secrets_in(url):
    """The userinfo of url and its colon-separated parts, longest first."""
    authority = url.split("://", 1)[-1]
    for end in "/?#":
        authority = authority.split(end, 1)[0]
    userinfo = authority.rpartition("@")[0]
    if not userinfo:
        return []
    return sorted({userinfo, *userinfo.split(":")} - {""}, key=len, reverse=True)


def _redact(text, url):
    """text with url's userinfo, and any URL userinfo, replaced by `***`."""
    for secret in _secrets_in(url):
        text = text.replace(secret, "***")
    return _USERINFO.sub(r"\1***@", text)


def _rpc_url_from_env():
    """$NANO_RPC_URL stripped, or the default when it is unset, empty or blank."""
    return (os.environ.get("NANO_RPC_URL") or "").strip() or DEFAULT_RPC_URL


def main(argv):
    argv, not_after_text = _take_not_after(argv)
    not_after = None
    if not_after_text is not None:
        try:
            not_after = parse_not_after(not_after_text)
        except ValueError:
            print(json.dumps({"verdict": "invalid_not_after", "not_after": not_after_text[:80]}))
            return 64
    if len(argv) not in (4, 5):
        print(__doc__.strip(), file=sys.stderr)
        return 64
    block_hash, account = argv[1], argv[3]
    try:
        expect_raw = int(argv[2])
    except ValueError:
        # Parsed here rather than inside the try below, because an amount that
        # is not a whole number of raw is not a node problem and must not be
        # reported as one. `0.001` and `1e27` are the two mistakes this skill
        # warns about: raw has 30 digits, a float keeps about 15, and the low
        # digits are where an underpayment hides. Refuse with exit 3 - do not
        # serve - rather than exiting 1 with an empty stdout, which the
        # documented table has no row for.
        print(json.dumps({"verdict": "invalid_amount", "expect_raw": argv[2][:80]}))
        return 3
    # The node asked is the whole evidence that money arrived, so the operator
    # has to be able to choose it. SKILL.md's frontmatter declares NANO_RPC_URL
    # for exactly that ("Nano node RPC to ask. Defaults to https://rpc.nano.to")
    # and nothing read it, so every verification went to rpc.nano.to whatever
    # the operator exported - including the sellers who set it BECAUSE that
    # endpoint wants a key and rate-limits a burst, and were rate-limited into
    # exit 4 while holding calls a buyer had already paid for. An explicit
    # RPC_URL argument is the narrower instruction and still wins; an unset or
    # empty or blank variable leaves the default exactly as it was, and
    # surrounding whitespace (a padded `export`) is not part of the URL.
    rpc_url = argv[4] if len(argv) == 5 else _rpc_url_from_env()
    try:
        receipt = verify(block_hash=block_hash, expect_raw=expect_raw, account=account,
                         rpc_url=rpc_url, not_after=not_after)
    except Late as error:
        print(json.dumps({"verdict": "late", "seen_at": error.seen_at, "not_after": error.not_after,
                          "amount_raw": error.receipt.amount_raw, "time_source": TIME_SOURCE}))
        return 5
    except UnknownTime as error:
        print(json.dumps({"verdict": "unknown_time", "seen_at": None, "not_after": error.not_after,
                          "amount_raw": error.receipt.amount_raw, "time_source": TIME_SOURCE}))
        return 6
    except NotFound:
        print(json.dumps({"verdict": "no_such_block"}))
        return 3
    except Mismatch as error:
        print(json.dumps({"verdict": "mismatch", "expected": str(error.expected), "got": str(error.got)}))
        return 3
    except (OSError, ValueError, http.client.HTTPException) as error:
        # http.client.HTTPException covers InvalidURL, which urllib raises for
        # a URL it cannot even connect to (`http://user:pw@host/` among them);
        # it is neither an OSError nor a ValueError and used to escape as a
        # traceback with exit 1 - and the password in it. A URL the node
        # cannot be reached at is still "nothing was checked". The detail is
        # redacted because these messages quote the URL back.
        print(json.dumps({"verdict": "node_unreachable", "detail": _redact(str(error), rpc_url)[:200]}))
        return 4
    print(receipt.to_json())
    return 0 if receipt.settled else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
