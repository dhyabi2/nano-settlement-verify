#!/usr/bin/env python3
"""Command-line front end for nano_settlement_verify.verify: one JSON verdict on stdout.

usage: python3 verify_cli.py BLOCK_HASH EXPECT_RAW ACCOUNT [RPC_URL] [--not-after WHEN]
WHEN is unix seconds or ISO-8601 with an offset (2026-10-09T18:00:00Z); inclusive.
RPC_URL defaults to $NANO_RPC_URL, else https://rpc.nano.to; the argument wins over both.
RPC_URL must be http or https with no '@' anywhere in it (write an '@' in a path or query
as %40; credentials are never sent, and %40 or %3A in the host part counts as one); any
other URL is refused (4), a configuration error that retrying will not fix.
exit 0 settled, 2 not confirmed yet, 3 mismatch, no such block or an amount that is not raw,
      4 node unreachable (nothing was checked), 5 late (seen after WHEN),
      6 unknown_time (node reports no time for the block; not on time), 64 usage
The time is the node's own local_timestamp, not signed by the sender: read it on two nodes
if seconds matter.
"""
import http.client
import json
import os
import sys
import urllib.parse

import nano_settlement_verify as _library
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

# Three refusals for the RPC URL, made before anything is sent. Their details are
# fixed strings and never quote the URL: urllib's own messages echo pieces of it
# back - percent-decoded, and cut wherever a `/`, `?` or `#` falls - so no
# scrubbing of those messages can promise that a credential in the URL stays out
# of stdout, stderr and the logs that keep them. Refusing every URL that carries
# an `@` can.
AT_SIGN_REFUSED = (
    "the RPC URL contains '@': user credentials are never sent, so remove them;"
    " an '@' in a path or query must be written %40"
)
SCHEME_REFUSED = "the RPC URL must be http or https"
UNPARSEABLE_REFUSED = "the RPC URL could not be parsed"
# A reply that is JSON but not an object (`[]`, `"x"`, `null`, `1`) makes the
# library raise TypeError, and one nested deeper than the parser's recursion
# limit RecursionError. Neither is a verdict about the money: nothing was
# checked, so it is node_unreachable, with a fixed detail.
REPLY_NOT_AN_OBJECT = "the node's reply was not a JSON object"
REPLY_WRONG_TYPE = (
    "the node's reply was not a JSON object, or a field in it had the wrong type"
)
REPLY_TOO_DEEP = "the node's reply was nested too deeply to read"
# `1e999`, `Infinity` and `-Infinity` are numbers to json.loads, and the
# library's int() of one raises OverflowError: an ArithmeticError, neither an
# OSError nor a ValueError. A reply too large to hold is MemoryError.
REPLY_NUMBER_TOO_LARGE = "the node's reply carried a number too large to read"
REPLY_TOO_LARGE = "the node's reply was too large to read"


def _reply_not_checked(error):
    """The fixed detail for a reply failure no other arm names."""
    return "the node's reply could not be checked (%s)" % type(error).__name__


class _NotAnObject(Exception):
    """The node answered with JSON that is not an object."""


def _objects_only(post_json):
    """post_json, refusing a reply that is not a JSON object.

    The library tests `"error" in reply` before anything else, and for a string
    or a list that is a substring or membership test: `"error"`, `"no error
    here"` and `["error"]` all came out as no_such_block (exit 3, "do not
    serve") - a verdict about the money from a reply that says nothing about
    it. The CLI sees the reply first, without changing the library.
    """
    def ask(rpc_url, payload):
        reply = post_json(rpc_url, payload)
        if not isinstance(reply, dict):
            raise _NotAnObject()
        return reply
    return ask


def _url_pieces(url):
    """The parts of url an error message must not repeat: raw, decoded, escaped.

    The userinfo, path, query, fragment and a non-numeric port, each at least
    four characters long, and every query value whatever its length.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        parts = None
    netloc = parts.netloc if parts else ""
    userinfo, _, hostport = netloc.rpartition("@")
    port = hostport.rpartition("]")[2].partition(":")[2]
    long_pieces = [userinfo, port if not port.isdigit() else ""]
    values = []
    if parts:
        long_pieces += [parts.path, parts.query, parts.fragment]
        for field in parts.query.split("&"):
            values.append(field.partition("=")[2])
    found = set()
    for piece, shortest in [(p, 4) for p in long_pieces] + [(v, 1) for v in values]:
        for form in (piece, urllib.parse.unquote(piece), urllib.parse.unquote_plus(piece)):
            for spelling in (form, repr(form)[1:-1], ascii(form)[1:-1]):
                if len(spelling) >= shortest:
                    found.add(spelling)
    return found


def _error_detail(error, url):
    """main's `str(error)[:200]`, unless that text repeats a piece of url.

    urllib quotes the URL back in some of its errors - percent-decoded, or with
    control characters escaped - so a key in a query or a password that dodged
    the '@' check (`hunter2%40host`) would land in stdout and the logs that keep
    it. Such an error is named by its class alone.
    """
    text = str(error)
    if any(piece in text for piece in _url_pieces(url)):
        return (
            "the request to the RPC URL failed (%s); its text would repeat part of"
            " the URL, so it is not shown" % type(error).__name__
        )
    return text[:200]


def _refuse_rpc_url(url):
    """A fixed reason the CLI will not ask url, or None when it may.

    Any `@` is refused, wherever it falls: telling userinfo from a path apart is
    the parse this guards against getting wrong - an unencoded `/` in a password
    moves the split, and the password lands in the "path". An `@` that really
    belongs in a path or query is written `%40`, which is sent as is. Only http and https are asked;
    a `file://` URL would let whoever sets the URL hand `verify` a reply they
    wrote themselves, and `settled: true` with it.
    """
    if "@" in url:
        return AT_SIGN_REFUSED
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return UNPARSEABLE_REFUSED
    # `%40` is an '@' and `%3A` a ':' once urllib decodes the host: in the
    # netloc they can only be credentials (`user:hunter2%40host`), and urllib's
    # "nonnumeric port: 'hunter2@host'" would print them.
    netloc = parts.netloc.lower()
    if "%40" in netloc or "%3a" in netloc:
        return AT_SIGN_REFUSED
    scheme = parts.scheme
    if scheme.lower() not in ("http", "https"):
        return SCHEME_REFUSED
    return None


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
    refused = _refuse_rpc_url(rpc_url)
    if refused is not None:
        print(json.dumps({"verdict": "node_unreachable", "detail": refused}))
        return 4
    asked = _library.post_json
    _library.post_json = _objects_only(asked)
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
    except _NotAnObject:
        print(json.dumps({"verdict": "node_unreachable", "detail": REPLY_NOT_AN_OBJECT}))
        return 4
    except TypeError:
        print(json.dumps({"verdict": "node_unreachable", "detail": REPLY_WRONG_TYPE}))
        return 4
    except RecursionError:
        print(json.dumps({"verdict": "node_unreachable", "detail": REPLY_TOO_DEEP}))
        return 4
    except (OSError, ValueError, http.client.HTTPException) as error:
        # http.client.HTTPException covers InvalidURL, which urllib raises for
        # a URL it cannot even connect to; it is neither an OSError nor a
        # ValueError and used to escape as a traceback with exit 1. A URL the
        # node cannot be reached at is still "nothing was checked".
        print(json.dumps({"verdict": "node_unreachable", "detail": _error_detail(error, rpc_url)}))
        return 4
    except OverflowError:
        print(json.dumps({"verdict": "node_unreachable", "detail": REPLY_NUMBER_TOO_LARGE}))
        return 4
    except MemoryError:
        print(json.dumps({"verdict": "node_unreachable", "detail": REPLY_TOO_LARGE}))
        return 4
    except Exception as error:  # noqa: BLE001 - no reply may end in a traceback
        # Whatever else a node's reply makes the library raise, nothing was
        # checked: exit 4, never a traceback with exit 1, which SKILL.md's
        # table has no row for. The detail names the class only.
        print(json.dumps({"verdict": "node_unreachable", "detail": _reply_not_checked(error)}))
        return 4
    finally:
        _library.post_json = asked
    print(receipt.to_json())
    return 0 if receipt.settled else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
