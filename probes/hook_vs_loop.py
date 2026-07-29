"""Can a response hook keep `requests` unwalled, instead of hand-following the redirects?

The jar policy failed because `Session.resolve_redirects` calls
`extract_cookies_to_jar(prepared_request._cookies, req, resp.raw)` -- it reads Set-Cookie off
the raw urllib3 response and merges it into the PreparedRequest, never consulting the session
jar's policy. Blocking storage therefore does not block sending.

A `response` hook fires in `Session.send` before redirects are resolved, so stripping
Set-Cookie from `resp.raw.headers` should leave extract_cookies_to_jar nothing to find while
requests' own 307/308 and cross-host behaviour survives.

Four variants, one article GET each:

    bare       -- control
    hook_raw   -- strip Set-Cookie from resp.raw.headers  (the candidate)
    hook_both  -- also clear resp.cookies, in case one is not enough
    loop       -- what the library actually ships, whatever that currently is. Named `loop`
                  for the hand-rolled redirect loop it compared against; the transport is
                  urllib3 now, and the field name is kept only so old rows still parse.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from _common import HEADERS, article_url, emit, library_decode, record_body, require_tokens


def _strip_raw(r, *a, **kw):
    # pop, not a suppressed `del`: a strip that silently no-ops would make "hook_raw ->
    # consent" mean either "stripping does not help" or "nothing was stripped", and this
    # probe exists to tell those apart. A real failure now surfaces as the arm's error.
    r.raw.headers.pop("Set-Cookie", None)
    return r


def _strip_both(r, *a, **kw):
    _strip_raw(r)
    r.cookies.clear()
    return r


tokens = require_tokens(4)
out = {}


def run(label, idx, hook):
    try:
        s = requests.Session()
        hooks = {"response": hook} if hook else {}
        r = s.get(article_url(tokens[idx]), headers=HEADERS, timeout=30, hooks=hooks)
        record_body(out, label, r.text)
        out[label + "_sent"] = (r.request.headers.get("Cookie") or "")[:40] or None
    except Exception as e:
        out[label] = f"error {type(e).__name__}"


run("bare", 0, None)
run("hook_raw", 1, _strip_raw)
run("hook_both", 2, _strip_both)

try:
    out["loop"], _ = library_decode(tokens[3])
except Exception as e:
    out["loop"] = f"error {type(e).__name__}"

emit(**out)
