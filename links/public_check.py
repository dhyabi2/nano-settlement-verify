"""Refuse to send a link a stranger cannot open.

    python3 -m links.public_check --text FILE|-  [--json]
    python3 -m links.public_check --url URL [URL ...] [--json]

Every http(s) URL is fetched once with NO credentials (no Authorization, no cookie, no token), at most 5
redirects, 15 s, a plain browser User-Agent. `public` is true only for a final 200 with a non-empty body that is
not a login wall. A 200 from here does not prove the recipient can reach it; a non-200 from here is enough to
stop us sending it. `unknown` is never public.

Exit code: 0 when the message verdict is `send`, 1 otherwise.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import datetime as dt

TIMEOUT_S = 15
MAX_REDIRECTS = 5
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CAVEAT = ("a 200 here does not prove the recipient can fetch it; "
          "a non-200 here proves we should not send it")
AUTH_WALL = re.compile(r"sign in to continue|log in to continue|please (?:sign|log) in|login required|"
                       r"<input[^>]+type=[\"']password[\"']", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]+", re.I)
TRAIL = ".,;:!?"


class TlsError(Exception):
    pass


class DnsError(Exception):
    pass


class EgressBlocked(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def default_fetch(url, timeout=TIMEOUT_S):
    """One GET, redirects followed by hand (<= 5). Returns (status, final_url, body, content_type).
    Verification of TLS is never disabled. No Authorization header, no cookie jar."""
    opener = urllib.request.build_opener(_NoRedirect)
    cur = url
    for _ in range(MAX_REDIRECTS + 1):
        req = urllib.request.Request(cur, headers={"User-Agent": BROWSER_UA, "Accept": "*/*",
                                                   "Range": "bytes=0-65535"})
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, cur, r.read(65536), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                cur = urllib.parse.urljoin(cur, e.headers["Location"])
                continue
            body = b""
            try:
                body = e.read(65536)
            except Exception:
                pass
            if e.code == 403 and "proxy" in (e.headers.get("Server", "") + e.headers.get("Via", "")).lower():
                raise EgressBlocked(cur)
            return e.code, cur, body, e.headers.get("Content-Type", "")
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, Exception) and "CERTIFICATE" in str(reason).upper():
                raise TlsError(str(reason))
            if "ssl" in type(reason).__name__.lower():
                raise TlsError(str(reason))
            if "name resolution" in str(reason).lower() or "nodename" in str(reason).lower() or "getaddrinfo" in str(reason).lower():
                raise DnsError(str(reason))
            raise
    return 310, cur, b"", ""


def extract_urls(text):
    seen, out = set(), []
    for m in URL_RE.finditer(text):
        u = m.group(0).rstrip(TRAIL)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def probe(url, fetch=default_fetch):
    row = {"url": url, "public": False, "status": None, "bytes": 0, "do_not_send": True}
    t0 = time.time()
    last = None
    for attempt in (0, 1):  # never more than one retry, and only for a timeout
        try:
            status, final, body, ctype = fetch(url)
            break
        except TlsError:
            row.update(reason="tls", detail="TLS verification failed; never retried with verification off")
            return row
        except DnsError:
            row.update(reason="dns")
            return row
        except EgressBlocked:
            row.update(public="unknown", reason="our_egress_blocked",
                       detail="OUR egress refused the host; this is our limit, not the host's. "
                              "We may not cite a URL we could not read")
            return row
        except Exception as e:  # timeout, reset
            last = e
    else:
        row.update(public="unknown", reason="timeout" if "timed out" in str(last).lower() or isinstance(last, TimeoutError) else "unreachable")
        return row
    row.update(status=status, bytes=len(body), final_url=final, content_type=ctype,
               elapsed_ms=int((time.time() - t0) * 1000))
    if status in (200, 206) and len(body) > 0:
        if AUTH_WALL.search(body[:20000].decode("utf-8", "replace")):
            row["reason"] = "auth_wall"
            return row
        row.update(public=True, do_not_send=False)
        return row
    if status == 200:
        row["reason"] = "empty_body"
    elif status == 404 and urllib.parse.urlparse(url).hostname == "raw.githubusercontent.com":
        row["reason"] = "private_repo_or_missing_path"
        row["suggest"] = "cite the pull request or a tagged release instead, and say if it is unmerged"
    else:
        row["reason"] = f"http_{status}"
    return row


def check_text(text, fetch=default_fetch):
    urls = extract_urls(text)
    rows = [probe(u, fetch) for u in urls]
    bad = [r for r in rows if r["public"] is not True]
    if not bad:
        verdict = "send"
    elif len(bad) < len(rows):
        verdict = "strip"      # dead links can be removed and evidence (a public URL) remains
    else:
        verdict = "refuse"     # the message's only evidence is non-public
    return {"v": "public-link-check-v1",
            "checked_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "all_public": not bad, "urls": rows, "verdict_for_the_message": verdict,
            "from_this_vantage_only": CAVEAT}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="links.public_check")
    ap.add_argument("--text")
    ap.add_argument("--url", nargs="+")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.url:
        text = "\n".join(a.url)
    elif a.text:
        text = sys.stdin.read() if a.text == "-" else open(a.text, encoding="utf-8").read()
    else:
        ap.error("give --text or --url")
    res = check_text(text)
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        for r in res["urls"]:
            print(("public " if r["public"] is True else "NOT-PUBLIC ") + r["url"] + f" {r['status']} {r.get('reason','')}".rstrip())
        print("verdict:", res["verdict_for_the_message"], "|", CAVEAT)
    return 0 if res["verdict_for_the_message"] == "send" else 1


if __name__ == "__main__":
    sys.exit(main())
