"""Can a response hook replace the hand-rolled redirect loop?

The jar policy failed because `Session.resolve_redirects` calls
`extract_cookies_to_jar(prepared_request._cookies, req, resp.raw)` -- it reads Set-Cookie off
the raw urllib3 response and merges it into the PreparedRequest, never consulting the session
jar's policy. Blocking storage therefore does not block sending.

But a `response` hook fires in `Session.send` BEFORE redirects are resolved. If the hook strips
Set-Cookie from `resp.raw.headers`, there is nothing for extract_cookies_to_jar to find, and
requests' own redirect handling (307/308 method preservation, cross-host header stripping)
survives -- roughly 20 lines lighter than the loop.

Four variants, one article GET each:

    bare       -- control
    hook_raw   -- strip Set-Cookie from resp.raw.headers  (the candidate)
    hook_both  -- also clear resp.cookies, in case one is not enough
    loop       -- the shipped manual loop, via the library
"""

import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import UA, classify, emit, fresh_tokens

HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip"}


def _strip_raw(r, *a, **kw):
    with contextlib.suppress(Exception):
        del r.raw.headers["Set-Cookie"]
    return r


def _strip_both(r, *a, **kw):
    _strip_raw(r, *a, **kw)
    with contextlib.suppress(Exception):
        r.cookies.clear()
    return r


try:
    tokens = fresh_tokens()
except Exception as e:
    emit(error=f"token fetch failed: {type(e).__name__}: {e}")
    raise SystemExit(1)

import requests

out = {}


def run(label, idx, hook):
    try:
        s = requests.Session()
        kw = {"headers": HEADERS, "timeout": 30}
        if hook:
            kw["hooks"] = {"response": hook}
        r = s.get(f"https://news.google.com/articles/{tokens[idx]}", **kw)
        out[label] = classify(r.text)
        out[label + "_kb"] = round(len(r.text) / 1024)
        out[label + "_sent"] = (r.request.headers.get("Cookie") or "")[:40] or None
    except Exception as e:
        out[label] = f"error {type(e).__name__}"


run("bare", 0, None)
run("hook_raw", 1, _strip_raw)
run("hook_both", 2, _strip_both)

try:
    from googlenewsdecoder import decode_flow, drive
    from googlenewsdecoder.transports import RequestsTransport

    res = drive(
        decode_flow(f"https://news.google.com/rss/articles/{tokens[3]}?oc=5"),
        RequestsTransport(),
        timeout=30,
    )
    out["loop"] = "decoded" if res.get("status") else "failed"
except Exception as e:
    out["loop"] = f"error {type(e).__name__}"

emit(**out)
