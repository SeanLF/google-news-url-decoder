"""Is the throttle counted per request, or per TCP connection?

`degrade.py` (urllib, a fresh connection per GET) is refused around 28 requests, where pooled
runs recorded elsewhere went far further. Connection reuse is the difference, so this counts
connects directly instead of inferring them: both `create_connection` implementations are
wrapped, and two arms run on one exit off one token supply, each stopping at the first 429.

    fresh   -- a new urllib opener per request, so one connection per redirect hop
    pooled  -- one urllib3 PoolManager(maxsize=1), so connections should stay near 1

Per request, both stop at a similar request count; per connection, pooled runs longer and
`*_requests / *_connections` is what pooling buys. Each arm is capped at half the token
supply, so this measures the ratio, not the ceiling -- a `pooled_refused_at: null` may only
mean the tokens ran out.
"""

import gzip
import os
import socket
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import urllib3
from _common import HEADERS, article_url, classify, emit, require_tokens

LIMIT = int(os.environ.get("LIMIT", "120"))

connects = {"n": 0}


def _install_connection_counter():
    """Count real TCP connects, on both paths and without double-counting either.

    urllib3 does not call `socket.create_connection`; it has its own copy in
    `urllib3.util.connection` that goes straight to `socket.socket()` + `connect()`. So the
    two wrappers below are disjoint, and patching only the stdlib one undercounted urllib3 to
    zero -- which is what the first version of this probe reported.

    `urllib3.connection` reaches its copy as a module attribute (`connection.create_connection`
    at call time), so patching the module it lives in is enough and is the only patch that
    takes effect.
    """
    real_socket = socket.create_connection
    real_u3 = urllib3.util.connection.create_connection

    def counting_socket(*a, **kw):
        connects["n"] += 1
        return real_socket(*a, **kw)

    def counting_u3(*a, **kw):
        connects["n"] += 1
        return real_u3(*a, **kw)

    socket.create_connection = counting_socket
    urllib3.util.connection.create_connection = counting_u3


_install_connection_counter()

tokens = require_tokens(2)

# Each arm gets half the supply, so LIMIT above len(tokens)//2 cannot be reached. Recorded as
# asked for, not as spent: compare `*_requests` against it before reading a `refused_at: null`
# as "never refused" rather than "ran out of tokens".
out = {"limit": LIMIT, "tokens": len(tokens)}


def run(label, fetch, supply):
    """Fetch until refused or the supply runs out; report requests, connections, outcome mix."""
    connects["n"] = 0
    kinds = {"article": 0, "consent": 0, "unknown": 0}
    refused_at = None
    for i, tok in enumerate(supply, 1):
        try:
            body = fetch(article_url(tok))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                refused_at = i
                break
            # NOTE: a dead article and a body we could not classify both land in `unknown`.
            kinds["unknown"] += 1
            continue
        except Exception:
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
    # A new opener every call: no pooling, so one connection per HOP. Note that this opener
    # follows redirects and the pooled arm below does not, so on an exit that 302s to the
    # consent host the two arms are not doing equal work. Read `*_kinds` before comparing.
    opener = urllib.request.build_opener()
    with opener.open(urllib.request.Request(url, headers=HEADERS), timeout=25) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


# retries=False also disables redirect following, so a 3xx comes back as the stub body and
# classifies as "unknown" rather than raising.
# Redirects MUST be followed here. `retries=False` yields redirect=0, so the pooled arm
# returned 302 stubs and fetched no article at all -- it then "survived longer" only by
# doing a third of the work, which invalidated the first run of this probe.
pool = urllib3.PoolManager(
    maxsize=1, retries=urllib3.Retry(total=None, redirect=10, other=0, raise_on_status=False)
)


def pooled_fetch(url):
    resp = pool.request("GET", url, headers=HEADERS)
    if resp.status == 429:
        return None
    return resp.data.decode("utf-8", "replace")


half = min(LIMIT, len(tokens) // 2)
# fresh runs first and spends budget the pooled arm then starts from, which biases against
# pooling. That is the safe direction for the conclusion this probe is used to support.
run("fresh", fresh_fetch, tokens[:half])
run("pooled", pooled_fetch, tokens[half : half * 2])

emit(**out)
