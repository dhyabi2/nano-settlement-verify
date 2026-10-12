#!/usr/bin/env python3
"""Command-line front end for nano_settlement_verify.verify: one JSON verdict on stdout.

usage: python3 verify_cli.py BLOCK_HASH EXPECT_RAW ACCOUNT [RPC_URL] [--not-after WHEN]
WHEN is unix seconds or ISO-8601 with an offset (2026-10-09T18:00:00Z); inclusive.
RPC_URL defaults to $NANO_RPC_URL, else https://rpc.nano.to; the argument wins over both.
RPC_URL must be http or https with no '@' anywhere in it (write an '@' in a path or query
as %40; credentials are never sent, and %40 or %3A in the host part counts as one), no
control character, a host of ASCII letters, digits, '-', '.' and '_' or an IP address (no
'%' and no unicode; write an international name in its xn-- form), and a port, if any, that
is a number from 0 to 65535; any other URL is refused (4), a configuration error that
retrying will not fix. A failure to reach the node prints only text that cannot hold the
URL: "HTTP Error <code>" without the server's reason phrase, "[Errno N] strerror", a
proxy's "Tunnel connection failed: <code>", an SSL reason, the library's own text about
the reply, or else the error's class.
exit 0 settled, 2 not confirmed yet, 3 mismatch, no such block or an amount that is not raw,
      4 node unreachable (nothing was checked), 5 late (seen after WHEN),
      6 unknown_time (node reports no time for the block; not on time), 64 usage
The time is the node's own local_timestamp, not signed by the sender: read it on two nodes
if seconds matter.
"""
import http.client
import ipaddress
import json
import os
import re
import ssl
import sys
import threading
import urllib.error
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
PORT_REFUSED = "the RPC URL's port must be a number from 0 to 65535"
NO_HOST_REFUSED = "the RPC URL has no host"
CONTROL_REFUSED = "the RPC URL contains a control character"
# urllib percent-decodes the host before it connects, and through a proxy a TLS
# hostname mismatch then names the decoded host: `localhost%2FSECRETKEY` came out
# as 'localhost/SECRETKEY', and %3F, %23, %20, %25 and %3B the same way; a
# unicode host is IDNA-mapped first (`%C2%AD`, a soft hyphen, simply vanished).
# So a host is a plain name of ASCII letters, digits, '-', '.' and '_', or an IP
# address, and nothing that decodes into anything else.
HOST_REFUSED = (
    "the RPC URL's host must be ASCII letters, digits, '-', '.' and '_',"
    " or an IP address"
)
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
# A TypeError raised while the request is made (http.client's "argument must be
# encoded string without null bytes" for a NUL in a tunnelled host) is not about
# the reply: it is reported as a failed request.
REQUEST_TYPE_ERROR = "the request to the RPC URL failed (TypeError)"


def _reply_not_checked(error):
    """The fixed detail for a reply failure no other arm names."""
    return "the node's reply could not be checked (%s)" % type(error).__name__


class _RequestTypeError(OSError):
    """post_json raised TypeError before any reply was read.

    An OSError, so a library user who calls verify while the CLI's check is in
    place still catches it with the `(OSError, ValueError)` handler.
    """


class _NotAnObject(ValueError):
    """The node answered with JSON that is not an object.

    A ValueError, so a library user who calls verify while the CLI's check is
    in place still catches it with the `(OSError, ValueError)` handler the
    README tells a seller to wrap verify in.
    """


def _objects_only(post_json):
    """post_json, refusing a reply that is not a JSON object.

    The library tests `"error" in reply` before anything else, and for a string
    or a list that is a substring or membership test: `"error"`, `"no error
    here"` and `["error"]` all came out as no_such_block (exit 3, "do not
    serve") - a verdict about the money from a reply that says nothing about
    it. The CLI sees the reply first, without changing the library.
    """
    def ask(rpc_url, payload):
        try:
            reply = post_json(rpc_url, payload)
        except TypeError as error:
            # post_json only sends and parses JSON text, which raises no
            # TypeError; one from here is the transport's, never the reply's.
            raise _RequestTypeError() from error
        if not isinstance(reply, dict):
            raise _NotAnObject()
        return reply
    return ask


# verify() looks post_json up in the library's module globals and takes no
# function to call instead, so the object check can only be put in place by
# swapping that global. The swap is held under this lock for the whole verify
# call, so two callers of main in one process never interleave a swap and a
# restore, and the second sees the library as the first found it. That also
# means in-process callers on different threads take turns for the whole
# network call; each run of the CLI as its own process is unaffected.
#
# It is re-entrant: a post_json that itself calls main on the same thread (a
# wrapper, a test double) used to block on a plain Lock this thread already
# held, forever. Each frame restores only the wrapper it installed itself, so
# nested frames unwind to exactly what each found.
_SWAP_LOCK = threading.RLock()


def _verify_objects_only(**arguments):
    """verify(**arguments) with post_json refusing a reply that is not an object."""
    with _SWAP_LOCK:
        asked = _library.post_json
        checked = _objects_only(asked)
        _library.post_json = checked
        try:
            return verify(**arguments)
        finally:
            # Restore only what we put there: if something else replaced the
            # global meanwhile, theirs stays.
            if _library.post_json is checked:
                _library.post_json = asked


# What a failed request prints. Every text below is built from parts of the
# error that cannot hold the URL - an errno and the OS's strerror, an HTTP status
# code, an SSL reason code and certificate message, or text the library builds
# from the node's reply - and never from the error's own message where that
# message could quote the URL back. urllib echoes the URL, a path segment or a
# query value in some messages, and a node may echo it in an HTTP reason phrase
# ("HTTP Error 401: bad key SECRETKEY"); matching the URL's pieces against those
# messages leaked a key whenever a piece was split differently than the check
# expected, and hid ordinary errors whenever a word of the URL turned up in them.
# So the class of the error decides what is shown, and the URL is not consulted.

# OSError texts with no errno that the standard library writes itself, word for
# word: socket and ssl timeouts, and http.client's closed-connection message.
# The ssl module's timeouts may carry an `_ssl.c:NNN: ` prefix (3.12 writes
# "_ssl.c:993: The handshake operation timed out"), so they are matched whole
# by a pattern that admits nothing else.
_FIXED_OS_TEXTS = frozenset({
    "timed out",
    "Remote end closed connection without response",
})
_SSL_TIMEOUT = re.compile(
    r"(?:_ssl\.c:[0-9]{1,6}: )?The (?:read|write|handshake) operation timed out")
# http.client's failed CONNECT through a proxy: "Tunnel connection failed: 407
# Proxy Authentication Required". The reason phrase is the proxy's own text, so
# only the three-digit status is kept.
_TUNNEL_FAILED = re.compile(r"Tunnel connection failed: ([1-9][0-9]{2})(?:[ \t].*)?",
                            re.DOTALL)


def _class_only(error):
    """The fixed detail for a failure whose text could carry the URL."""
    return "the request to the RPC URL failed (%s)" % type(error).__name__


def _ssl_text(error):
    """`[SSL: REASON] ...` from the SSL reason code and certificate message only.

    The certificate message may name the host asked for ("certificate is not
    valid for 'node.example'"), after urllib has percent-decoded it. That host
    is the one the URL names and is printed as such; it carries no credential
    and no other piece of the URL, because before anything is sent the CLI
    refuses userinfo, a percent-encoded '@' or ':', a non-numeric port, and any
    host that is not plain ASCII letters, digits, '-', '.' and '_' or an IP
    address - so no '%' is left in it to decode into a '/', '?', '#' or ' '.
    """
    reason = getattr(error, "reason", None)
    if not isinstance(reason, str) or not reason:
        return "[SSL] %s" % type(error).__name__
    if isinstance(error, ssl.SSLCertVerificationError):
        message = getattr(error, "verify_message", None)
        if isinstance(message, str) and message:
            return "[SSL: %s] certificate verify failed: %s" % (reason, message)
        return "[SSL: %s] certificate verify failed" % reason
    return "[SSL: %s]" % reason


def _os_text(error):
    """`[Errno N] strerror` for an OSError, or None when it has no OS text.

    A filename, which OSError's own text appends, is left out. An OSError with
    no errno is shown only when its text is one the standard library writes
    word for word (`timed out`).
    """
    if isinstance(error, ssl.SSLError):
        return _ssl_text(error)
    errno, strerror = error.errno, error.strerror
    if isinstance(errno, int) and isinstance(strerror, str):
        return "[Errno %d] %s" % (errno, strerror)
    if errno is None and len(error.args) == 1 and isinstance(error.args[0], str):
        text = error.args[0]
        if text in _FIXED_OS_TEXTS or _SSL_TIMEOUT.fullmatch(text):
            return text
        tunnel = _TUNNEL_FAILED.fullmatch(text)
        if tunnel:
            return "Tunnel connection failed: %s" % tunnel.group(1)
    return None


def _error_detail(error):
    """The detail printed for a failure to reach or read the node.

    Main printed `str(error)[:200]` for every one; that text is kept, cut at 200,
    where it is built from something other than the URL:

    - NotANodeReply, json.JSONDecodeError and UnicodeDecodeError: the library's
      own text about the node's reply, or the JSON parser's about its bytes.
    - urllib.error.HTTPError: `HTTP Error <code>`; the reason phrase is the
      server's own text, which can repeat the URL, so it is dropped.
    - urllib.error.URLError around an OSError, and a bare OSError (a timeout, a
      reset connection, a DNS failure): `[Errno N] strerror`, inside
      `<urlopen error ...>` when urllib wrapped it - main's exact text for
      connection refused, a DNS failure and a timeout.
    - ssl.SSLError: its reason code, and for a certificate its verify message.

    Anything else - http.client.InvalidURL, which quotes the URL, any other
    http.client.HTTPException, a URLError whose reason is not an OSError, any
    other ValueError - is named by its class only.
    """
    if isinstance(error, _library.NotANodeReply):
        return str(error)[:200]
    if isinstance(error, json.JSONDecodeError):
        return ("%s: line %d column %d (char %d)"
                % (error.msg, error.lineno, error.colno, error.pos))[:200]
    if isinstance(error, UnicodeDecodeError):
        return str(error)[:200]
    if isinstance(error, urllib.error.HTTPError):
        if isinstance(error.code, int):
            return "HTTP Error %d" % error.code
        return _class_only(error)
    if isinstance(error, urllib.error.URLError):
        if isinstance(error.reason, OSError):
            text = _os_text(error.reason)
            if text is not None:
                return ("<urlopen error %s>" % text)[:200]
        return _class_only(error)
    if isinstance(error, OSError):
        text = _os_text(error)
        if text is not None:
            return text[:200]
    return _class_only(error)


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
    if any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f for c in url):
        return CONTROL_REFUSED
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
    netloc = parts.netloc
    if not netloc or netloc.startswith(":"):
        # `http:///x` and `http://:80/`: urllib's "no host given".
        return NO_HOST_REFUSED
    return _refuse_port(netloc) or _refuse_host(netloc)


_NAME = re.compile(r"[A-Za-z0-9._-]+")
_ZONE = re.compile(r"25[A-Za-z0-9._~-]+")


def _refuse_host(netloc):
    """HOST_REFUSED unless netloc's host is a plain ASCII name or an IP address.

    Called after _refuse_port, so outside brackets the host is everything
    before the one colon. A bracketed IPv6 address may carry a zone id written
    `%25name` (`[fe80::1%25eth0]`), the only '%' allowed in the host.
    """
    if netloc.startswith("["):
        inside = netloc[1:netloc.find("]")]
        address, percent, zone = inside.partition("%")
        if percent and not _ZONE.fullmatch(zone):
            return HOST_REFUSED
        try:
            ipaddress.IPv6Address(address)
        except ValueError:
            return HOST_REFUSED
        return None
    if not _NAME.fullmatch(netloc.partition(":")[0]):
        return HOST_REFUSED
    return None


_DIGITS = frozenset("0123456789")


def _refuse_port(netloc):
    """PORT_REFUSED unless netloc's port, if any, is ASCII digits up to 65535.

    The check above parses the port after the FIRST colon and http.client
    after the LAST, so `localhost:SECRETKEY:443` is host `localhost:SECRETKEY`
    to the request - and through an HTTPS proxy, a TLS hostname-mismatch error
    then names it. A host is a name or a bracketed IPv6 address, so outside
    brackets at most one colon may appear, and what follows it must be a
    number (or nothing, which means the default port).
    """
    if netloc.startswith("["):
        host_end = netloc.find("]")
        if host_end < 0:
            return UNPARSEABLE_REFUSED
        rest = netloc[host_end + 1:]
        if rest and not rest.startswith(":"):
            return PORT_REFUSED
        port = rest[1:]
    else:
        if "[" in netloc or "]" in netloc:
            return UNPARSEABLE_REFUSED
        if netloc.count(":") > 1:
            return PORT_REFUSED
        port = netloc.partition(":")[2]
    if not set(port) <= _DIGITS:
        return PORT_REFUSED
    # urllib refuses a port above 65535 with a ValueError of its own; refused
    # here, it gets the same fixed detail as any other port that cannot work.
    if port and int(port) > 65535:
        return PORT_REFUSED
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
    # The argument is stripped like the variable, so `"http://127.0.0.1:7076 "`
    # is asked as written without the space; a blank argument stays blank and
    # is refused below, never replaced by the variable or the default.
    rpc_url = argv[4].strip() if len(argv) == 5 else _rpc_url_from_env()
    refused = _refuse_rpc_url(rpc_url)
    if refused is not None:
        print(json.dumps({"verdict": "node_unreachable", "detail": refused}))
        return 4
    try:
        receipt = _verify_objects_only(block_hash=block_hash, expect_raw=expect_raw,
                                       account=account, rpc_url=rpc_url, not_after=not_after)
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
    except _RequestTypeError:
        print(json.dumps({"verdict": "node_unreachable", "detail": REQUEST_TYPE_ERROR}))
        return 4
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
        # ValueError stays here: the library's NotANodeReply is one, and its
        # text - built from the node's reply - is printed as main printed it.
        # http.client.HTTPException covers InvalidURL, which urllib raises for
        # a URL it cannot even connect to; it is neither an OSError nor a
        # ValueError and used to escape as a traceback with exit 1. A URL the
        # node cannot be reached at is still "nothing was checked".
        # _error_detail decides the text by the error's class alone.
        print(json.dumps({"verdict": "node_unreachable", "detail": _error_detail(error)}))
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
    print(receipt.to_json())
    return 0 if receipt.settled else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
