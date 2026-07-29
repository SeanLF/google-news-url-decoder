"""Is the budget counted per REQUEST, or per CONNECTION?

`degrade.py` (urllib, a fresh connection per GET) is refused at ~28 requests. A pooled
urllib3 run through the library made 15,256 article GETs across nine exits without a single
429. Same endpoint, same day, three orders of magnitude apart. The one thing that differs is
connection reuse.

If the throttle counts connections, a pooled client should still be served on an address that
has just refused an unpooled one -- which is the state every exit is in immediately after
`degrade`. That makes this a decisive test rather than a slow one, and it costs a handful of
requests.

Run it straight after degrade.py on the same exit.
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
