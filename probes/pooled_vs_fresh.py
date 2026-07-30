"""Is the budget counted per REQUEST, or per CONNECTION?

Still the open question, and this is the cheapest way at it. `connections.py` measures how far
each kind of client gets, which cannot separate the two: in every run so far requests and
connections rose together, so a refusal is consistent with either being the thing counted.

This asks a binary question instead. If the throttle counts connections, a pooled client should
still be served on an address that has just refused an unpooled one, which is the state an exit is
in immediately after `degrade.py`. Served means the counter is not simply per request; refused
means it is, or that something else is.

Running on a spent address is therefore the design, NOT the shared-budget confound that
`connections.py` and `docs/probe-harness.md` warn about. That warning is about comparing how long
two arms survive on one address, where the second inherits the remains. Here the answer is
yes-or-no on the first few requests, and a spent address is the precondition.

    ./probes/runner/probe <exit> degrade
    ./probes/runner/probe <exit> pooled_vs_fresh    # same exit, straight after

Earlier versions of this docstring cited "15,256 article GETs across nine exits without a single
429" against ~28 for the unpooled arm. That figure is retracted: it dates from a period when the
request envelope was malformed, which `_common.fresh_tokens` documents, and nothing has reproduced
it. Do not quote it. The largest reproduced pooled figure is roughly 660 round trips on one
connection, and that run ended in a stall rather than a refusal.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import UA, classify, emit, fresh_tokens, get

HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip"}
N = int(os.environ.get("N", "12"))

try:
    tokens = fresh_tokens()
except Exception as e:
    emit(error=f"token fetch failed: {type(e).__name__}: {e}")
    raise SystemExit(1)

out = {"n": N}

# Control: the unpooled path, which degrade.py just drove into a 429.
fresh_ok = fresh_refused = 0
for tok in tokens[:N]:
    try:
        body, _ = get(f"https://news.google.com/articles/{tok}")
        fresh_ok += 1 if classify(body) == "article" else 0
    except Exception as e:
        if "429" in str(e):
            fresh_refused += 1
        else:
            out.setdefault("fresh_errors", []).append(type(e).__name__)
out["fresh_ok"] = fresh_ok
out["fresh_429"] = fresh_refused

# The pooled path: one PoolManager, so the same connection is reused across requests.
pooled_ok = pooled_refused = 0
try:
    import urllib3

    http = urllib3.PoolManager(maxsize=1, retries=False)
    for tok in tokens[N : N * 2]:
        try:
            resp = http.request("GET", f"https://news.google.com/articles/{tok}", headers=HEADERS)
            if resp.status == 429:
                pooled_refused += 1
            elif classify(resp.data.decode("utf-8", "replace")) == "article":
                pooled_ok += 1
        except Exception as e:
            out.setdefault("pooled_errors", []).append(type(e).__name__)
    out["pooled_ok"] = pooled_ok
    out["pooled_429"] = pooled_refused
except Exception as e:
    out["pooled_ok"] = f"error {type(e).__name__}"

emit(**out)
