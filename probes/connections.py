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
from _common import HEADERS, article_url, classify, dedupe, emit, fresh_tokens, progress

LIMIT = int(os.environ.get("LIMIT", "120"))
QUERIES = os.environ.get("QUERIES", "world,business,science,sports,health,technology").split(",")
# ONE arm per address. Running both against the same exit cannot answer the question: they
# share that address's budget, so whichever runs second inherits what the first left and is
# refused early for reasons that have nothing to do with pooling. Measured: 8 of 11 pooled
# arms refused at request 1, immediately after the fresh arm had spent the budget.
ARM = os.environ.get("ARM", "both")

connects = {"n": 0}
hops = {"n": 0}


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
    # Round trips as well as connects. `*_requests` counts ARTICLE FETCHES, and each one follows
    # a redirect or two, so reading it as a request count understated the real load by about half
    # -- and "how many requests does one connection get" is the question this probe is asked.
    # urllib3 recurses through urlopen once per hop, and urllib's opener calls http_response per
    # response, so the two below are disjoint the same way the connect wrappers are.
    real_urlopen = urllib3.PoolManager.urlopen

    def counting_urlopen(self, method, url, *a, **kw):
        hops["n"] += 1
        return real_urlopen(self, method, url, *a, **kw)

    urllib3.PoolManager.urlopen = counting_urlopen

    real_http_response = urllib.request.HTTPErrorProcessor.http_response

    def counting_http_response(self, request, response):
        hops["n"] += 1
        return real_http_response(self, request, response)

    urllib.request.HTTPErrorProcessor.http_response = counting_http_response

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

# Several feeds, because one yields about a hundred tokens and that was the binding constraint
# on every single-connection run so far: `requests` came back equal to `tokens`, so `refused_at:
# null` meant "ran out of articles to ask for", not "found no ceiling". Answering how far one
# connection goes needs a supply larger than the answer.
tokens = []
for q in QUERIES:
    if len(tokens) >= LIMIT * 2:
        break
    try:
        tokens = dedupe(tokens + fresh_tokens(query=q.strip()))
    except Exception as e:
        print(f"query {q!r} failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
if len(tokens) < 2:
    emit(error=f"only {len(tokens)} tokens from {len(QUERIES)} queries")
    raise SystemExit(1)

# `limit` is what was asked for and `tokens` what was available: compare both against
# `*_requests` before reading `refused_at: null` as "never refused".
out = {"limit": LIMIT, "tokens": len(tokens)}


def run(label, fetch, supply):
    """Fetch until refused or the supply runs out; report requests, connections, outcome mix."""
    connects["n"] = 0
    hops["n"] = 0
    kinds = {"article": 0, "consent": 0, "unknown": 0}
    refused_at = None
    stalled_at = None
    errors = []
    consecutive = 0
    for i, tok in enumerate(supply, 1):
        try:
            body = fetch(article_url(tok))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                refused_at = i
                break
            # NOTE: a dead article and a body we could not classify both land in `unknown`.
            kinds["unknown"] += 1
            consecutive += 1
            errors.append(f"HTTP {e.code}")
        except Exception as e:
            kinds["unknown"] += 1
            consecutive += 1
            errors.append(type(e).__name__)
        else:
            if body is None:
                refused_at = i
                break
            kinds[classify(body)] += 1
            consecutive = 0
            progress(i, len(supply), connections=connects["n"], round_trips=hops["n"])
        # A refusal does not always arrive as a 429. One run stopped transferring and, before this
        # probe had a timeout, hung forever; with a timeout it would instead have burned 30s per
        # remaining token producing nothing. Five failures in a row means the run is over, and
        # saying WHERE it ended and with what is the difference between a result and a shrug.
        if consecutive >= 5:
            stalled_at = i
            break
    out[label + "_requests"] = sum(kinds.values()) + (1 if refused_at else 0)
    # Article fetches above, HTTP round trips here. The second is what a per-request budget is
    # spent in, and it is roughly double the first wherever a redirect is being followed.
    out[label + "_round_trips"] = hops["n"]
    out[label + "_connections"] = connects["n"]
    out[label + "_refused_at"] = refused_at
    # Distinguished from a refusal on purpose: a 429 is Google saying no, five consecutive
    # failures is the connection or the tunnel going quiet, and reading the second as the first
    # would put a ceiling in the notes that Google never stated.
    out[label + "_stalled_at"] = stalled_at
    out[label + "_last_errors"] = errors[-5:]
    out[label + "_ended"] = (
        "refused" if refused_at else "stalled" if stalled_at else "ran out of tokens"
    )
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
    # urllib3's default timeout is None, i.e. wait forever, and the fresh arm above passes 25
    # while this one passed nothing. A run of 400 stopped transferring at roughly 330 fetches and
    # then hung indefinitely: no row, no error, and "slow" indistinguishable from "stopped". A
    # refusal that arrives as a stalled socket rather than a 429 is a mode worth being able to
    # see, and without a timeout it is the one mode this probe cannot report.
    resp = pool.request("GET", url, headers=HEADERS, timeout=urllib3.Timeout(total=30))
    if resp.status == 429:
        return None
    return resp.data.decode("utf-8", "replace")


out["arm"] = ARM
if ARM == "both":
    # Both arms on one exit share that exit's budget: whichever runs second starts from what
    # the first left. Measured, 8 of 11 pooled arms were refused at request 1 immediately
    # after a fresh arm had spent it. Use ARM=fresh / ARM=pooled on SEPARATE exits to compare.
    half = min(LIMIT, len(tokens) // 2)
    run("fresh", fresh_fetch, tokens[:half])
    run("pooled", pooled_fetch, tokens[half : half * 2])
else:
    run(ARM, fresh_fetch if ARM == "fresh" else pooled_fetch, tokens[:LIMIT])

emit(**out)
