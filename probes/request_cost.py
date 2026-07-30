"""Is the budget counted in REQUESTS, or in articles fetched?

The question left open after connection count turned out not to matter. It decides whether the two
savings in this library buy headroom or only latency: sending the locale up front removes a
redirect, and `/rss/articles` is the smaller page, so both cut round trips per decode.

Two arms differing in exactly one thing, the number of round trips one article fetch costs:

    locale  ->  /articles/<token>?hl=..&gl=..&ceid=..   200 straight away.  1 round trip
    bare    ->  /articles/<token>                       302 then 200.       2 round trips

Same pooled client, same tokens, one arm per address. The prediction is sharp, which is why this
needs ~5 arms per side where the pooled-vs-unpooled question needed ~82:

    counted in requests       -> the locale arm reaches about TWICE the article fetches
    counted in article fetches -> the two arms land in the same place

`round_trips` is reported so the arms can be checked to have actually differed as intended. If the
locale arm shows 2.0 round trips per fetch, Google redirected it anyway and the row is void.

    ./probes/runner/probe <exit> request_cost ARM=locale
    ./probes/runner/probe <exit> request_cost ARM=bare

One arm per address, always. Two arms on one address means the second measures the remains of the
first, which is how the connection claim this probe replaces went wrong.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import urllib3
from _common import HEADERS, classify, dedupe, emit, fresh_tokens, progress

from googlenewsdecoder import protocol

ARM = os.environ.get("ARM", "locale")
LIMIT = int(os.environ.get("LIMIT", "400"))
QUERIES = os.environ.get("QUERIES", "world,business,science,sports,health,technology").split(",")

if ARM not in ("locale", "bare"):
    emit(error=f"ARM must be locale or bare, not {ARM!r}")
    raise SystemExit(2)

hops = {"n": 0}
_real_urlopen = urllib3.PoolManager.urlopen


def _counting_urlopen(self, method, url, *a, **kw):
    # Per HOP, which is the unit this probe exists to compare. urllib3 recurses through urlopen
    # once per redirect, so this counts what the address is actually being asked for.
    hops["n"] += 1
    return _real_urlopen(self, method, url, *a, **kw)


urllib3.PoolManager.urlopen = _counting_urlopen

tokens = []
for q in QUERIES:
    if len(tokens) >= LIMIT:
        break
    try:
        tokens = dedupe(tokens + fresh_tokens(query=q.strip()))
    except Exception as e:
        print(f"query {q!r} failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
tokens = tokens[:LIMIT]
if len(tokens) < 20:
    emit(error=f"only {len(tokens)} tokens from {len(QUERIES)} queries")
    raise SystemExit(1)


def url_for(token):
    """The article URL this arm asks for.

    The locale arm goes through `protocol.params_urls` rather than building its own, so what is
    measured is what the library actually sends. `[0]` is the cheaper `/rss/articles` candidate.
    """
    if ARM == "locale":
        return protocol.params_urls(token)[0]
    return protocol.params_urls(token, locale=None)[0]


pool = urllib3.PoolManager(
    maxsize=1,
    # Same as the other probes: connect and read pinned rather than left at None, which is
    # unbounded, and redirects followed because the bare arm's whole point is that it redirects.
    retries=urllib3.Retry(
        total=None, connect=0, read=0, redirect=10, other=0, raise_on_status=False
    ),
)

kinds = {"article": 0, "consent": 0, "unknown": 0}
refused_at = stalled_at = None
errors = []
consecutive = 0
try:
    for i, token in enumerate(tokens, 1):
        try:
            resp = pool.request(
                "GET", url_for(token), headers=HEADERS, timeout=urllib3.Timeout(total=30)
            )
        except Exception as e:
            kinds["unknown"] += 1
            errors.append(type(e).__name__)
            consecutive += 1
        else:
            if resp.status == 429:
                refused_at = i
                break
            kinds[classify(resp.data.decode("utf-8", "replace"))] += 1
            consecutive = 0
            progress(i, len(tokens), round_trips=hops["n"], arm=ARM)
        # A refusal does not always arrive as a 429; five failures running means the run is over,
        # and calling that a ceiling would invent a number Google never stated.
        if consecutive >= 5:
            stalled_at = i
            break
finally:
    pool.clear()

fetched = sum(kinds.values())
emit(
    arm=ARM,
    limit=LIMIT,
    tokens=len(tokens),
    fetches=fetched,
    round_trips=hops["n"],
    # The check that the arms differed as designed. locale should be ~1.0 and bare ~2.0; a locale
    # arm at 2.0 was redirected anyway and its row says nothing about request cost.
    round_trips_per_fetch=round(hops["n"] / fetched, 2) if fetched else None,
    refused_at=refused_at,
    stalled_at=stalled_at,
    ended="refused" if refused_at else "stalled" if stalled_at else "ran out of tokens",
    kinds=kinds,
    last_errors=errors[-5:],
)
