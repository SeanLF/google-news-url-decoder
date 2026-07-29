"""Is the throttle counted per request, or per TCP connection?

`degrade.py` (urllib, a fresh connection per GET) is refused at ~28 requests. A pooled urllib3
run made 15,256 article GETs across nine exits with no 429 at all. The one thing that differs
is connection reuse, so this counts connections directly rather than inferring them.

`socket.create_connection` is wrapped to tally real TCP connects. Two arms on one exit, same
token supply, each stopping at the first 429:

    fresh   -- a new urllib opener per request, so connections == requests
    pooled  -- one urllib3 PoolManager(maxsize=1), so connections should stay near 1

If the budget is per request, both stop at a similar request count. If it is per connection,
the pooled arm should run far longer, and the ratio of requests-to-connections is also the
measurement of what pooling buys -- the claim the transport docstring makes and I never
checked.
"""

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import UA, classify, emit, fresh_tokens

HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip"}
LIMIT = int(os.environ.get("LIMIT", "120"))

connects = {"n": 0}


def _install_connection_counter():
    """Count real TCP connects. urllib3 does not go through socket.create_connection -- it has
    its own wrapper in urllib3.util.connection -- so patching only the stdlib undercounts it
    to zero, which is what the first version of this probe reported."""
    real = socket.create_connection

    def counting(*a, **kw):
        connects["n"] += 1
        return real(*a, **kw)

    socket.create_connection = counting

    import urllib3.util.connection as u3conn

    real_u3 = u3conn.create_connection

    def counting_u3(*a, **kw):
        connects["n"] += 1
        return real_u3(*a, **kw)

    u3conn.create_connection = counting_u3
    import urllib3.connection as u3c

    u3c.create_connection = counting_u3


_install_connection_counter()

try:
    tokens = fresh_tokens()
except Exception as e:
    emit(error=f"token fetch failed: {type(e).__name__}: {e}")
    raise SystemExit(1)

out = {"limit": LIMIT, "tokens": len(tokens)}


def run(label, fetch, supply):
    """Fetch until refused or the supply runs out; report requests, connections, outcome mix."""
    connects["n"] = 0
    kinds = {"article": 0, "consent": 0, "unknown": 0}
    refused_at = None
    for i, tok in enumerate(supply, 1):
        try:
            body = fetch(f"https://news.google.com/articles/{tok}")
        except Exception as e:
            if "429" in str(e):
                refused_at = i
                break
            kinds["unknown"] += 1
            continue
        if body is None:
            refused_at = i
            break
        kinds[classify(body)] += 1
    out[label + "_requests"] = sum(kinds.values()) + (1 if refused_at else 0)
    out[label + "_connections"] = connects["n"]
    out[label + "_refused_at"] = refused_at
    out[label + "_kinds"] = kinds


def fresh_fetch(url):
    import gzip
    import urllib.request

    # A new opener every call: no pooling, so one connection per request.
    opener = urllib.request.build_opener()
    with opener.open(urllib.request.Request(url, headers=HEADERS), timeout=25) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


import urllib3

pool = urllib3.PoolManager(maxsize=1, retries=False)


def pooled_fetch(url):
    resp = pool.request("GET", url, headers=HEADERS)
    if resp.status == 429:
        return None
    return resp.data.decode("utf-8", "replace")


half = min(LIMIT, len(tokens) // 2)
run("fresh", fresh_fetch, tokens[:half])
run("pooled", pooled_fetch, tokens[half : half * 2])

emit(**out)
