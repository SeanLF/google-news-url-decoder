"""Is the budget counted per REQUEST, or per CONNECTION?

The open question. `connections.py` measures how far each kind of client gets, which cannot
separate the two: in every run so far requests and connections rose together, so a refusal is
consistent with either being what is counted.

This asks it directly, in one process on one address:

    1. Spend the address with an UNPOOLED client, a fresh connection per request, until it is
       refused. That is the precondition, not the measurement.
    2. Immediately, on the same address, try a POOLED client that reuses one connection.

Served in step 2 means the counter is not simply per request, since the requests are already spent
and only the connection behaviour changed. Refused in step 2 means requests are counted, or
something else is. Either answer is worth the handful of requests step 2 costs.

Both steps run HERE rather than across two `./probe` invocations, and that is the point. An earlier
version documented "run it straight after degrade.py on the same exit", which cannot work: every
invocation brings up its own tunnel and Proton assigns a different address each time, so both arms
ran on a fresh address and step 1's refusal never applied to step 2. Same exit is not same address.

If step 1 does not get refused, the row says so and stops. A comparison against an address that
still has budget answers nothing, and reporting it as though it did is how this probe previously
produced numbers that looked like findings.

    ./probes/runner/probe <exit> pooled_vs_fresh
    ./probes/runner/probe <exit> pooled_vs_fresh LIMIT=300 N=15

Earlier versions of this docstring cited "15,256 article GETs across nine exits without a single
429" against ~28 unpooled. That figure is retracted: it dates from a period when the request
envelope was malformed, which `_common.fresh_tokens` documents, and nothing has reproduced it. The
largest reproduced pooled figure is roughly 660 round trips on one connection, and that run ended
in a stall rather than a refusal.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (
    HEADERS,
    article_url,
    classify,
    dedupe,
    emit,
    fresh_tokens,
    progress,
    spend_until_refused,
)

# Enough to actually exhaust an address in step 1. The unpooled arm has been refused between 19
# and 88 article fetches across every exit measured, so this is generous headroom rather than a
# target, and the row reports where it actually stopped.
LIMIT = int(os.environ.get("LIMIT", "250"))
# Pacing buys nothing: the throttle is not a rate. 0 keeps step 2 close behind step 1, which is
# the only thing that matters here.
GAP = float(os.environ.get("GAP", "0"))
# Step 2 is a yes-or-no, so it wants to be small. Spending a lot here would turn a cheap decisive
# test into a second budget run.
N = int(os.environ.get("N", "12"))
QUERIES = os.environ.get("QUERIES", "world,business,science,sports,health,technology").split(",")

tokens = []
for q in QUERIES:
    if len(tokens) >= LIMIT + N:
        break
    try:
        tokens = dedupe(tokens + fresh_tokens(query=q.strip()))
    except Exception as e:
        print(f"query {q!r} failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

if len(tokens) < N + 2:
    emit(error=f"only {len(tokens)} tokens from {len(QUERIES)} queries; need {N + 2}")
    raise SystemExit(1)

out = {"limit": LIMIT, "n": N, "tokens": len(tokens)}

# --- step 1: spend it, unpooled -----------------------------------------------------------
spent, refused = spend_until_refused(tokens[:LIMIT], gap=GAP)
out["fresh_articles"] = spent
out["fresh_refused"] = refused

if not refused:
    # Not a failure, and not a finding either. Say which.
    out["verdict"] = "inconclusive: unpooled arm was never refused, so there is no spent address to test"
    emit(**out)
    raise SystemExit(0)

# --- step 2: same address, pooled ---------------------------------------------------------
import urllib3

# Redirects MUST be followed. `retries=False` yields redirect=0, which this probe used to pass:
# the pooled arm then collected 302 stubs, fetched no article at all, and "was still served" only
# because it was not doing the work. That is the same defect connections.py records invalidating
# its own first run.
pool = urllib3.PoolManager(
    maxsize=1,
    retries=urllib3.Retry(total=None, redirect=10, other=0, raise_on_status=False),
)

pooled_articles = pooled_429 = 0
pooled_errors = []
try:
    for i, tok in enumerate(tokens[LIMIT : LIMIT + N] or tokens[-N:], 1):
        try:
            resp = pool.request("GET", article_url(tok), headers=HEADERS, timeout=urllib3.Timeout(total=30))
        except Exception as e:
            pooled_errors.append(type(e).__name__)
            continue
        if resp.status == 429:
            pooled_429 += 1
        elif classify(resp.data.decode("utf-8", "replace")) == "article":
            pooled_articles += 1
        progress(i, N, articles=pooled_articles, refused=pooled_429)
finally:
    pool.clear()

out["pooled_articles"] = pooled_articles
out["pooled_429"] = pooled_429
if pooled_errors:
    out["pooled_errors"] = pooled_errors

# Stated in the row rather than left to whoever reads it, since the whole probe exists for this
# one inference and the row is what survives.
if pooled_articles and not pooled_429:
    out["verdict"] = "pooled served on a spent address: not counted per request alone"
elif pooled_429 and not pooled_articles:
    out["verdict"] = "pooled refused too: consistent with a per-request or per-address count"
else:
    out["verdict"] = f"mixed: {pooled_articles} served, {pooled_429} refused"

emit(**out)
