"""What does one decode cost against the per-address budget, and how many do you get?

Two numbers, and only the first is solid. `article_gets_per_decode` is a ratio measured inside
one run, so it needs no second arm and survives a partly-spent budget. `first_429_at` is a
LOWER BOUND on the ceiling, not the ceiling: with WORKERS in flight, later-indexed tokens
complete before the first refusal lands, so the address accepted at least that many and
usually more. Run WORKERS=1 if you want the ceiling itself.

Counting happens at the HTTP client, not at the transport, because the transport hands its
whole redirect chain to the client in one call -- wrapping the transport would see one request
where four went out. The patch below therefore has to name the client the transport actually
uses; if that changes, the counter silently reads zero and the run says so on stderr.

    ./probe SI-38 budget LIMIT=400 WORKERS=10
"""

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import dedupe, emit, fresh_tokens, rss_url

LIMIT = int(os.environ.get("LIMIT", "60"))
# The throttle is a per-address budget counted in requests, not a rate, and the one rate
# comparison on record went the other way (70 clean at 10/s, 33 throttled at 0.24/s). So
# pacing and serial execution buy wall-clock, not headroom.
GAP = float(os.environ.get("GAP", "0"))
WORKERS = int(os.environ.get("WORKERS", "10"))
QUERIES = os.environ.get("QUERIES", "world,business,science,sports,health").split(",")

counts = {"article_get": 0, "consent_get": 0, "other": 0, "post": 0}
_counts_lock = threading.Lock()


def _bump(method, url):
    """File one HTTP round trip, by HOST rather than by substring.

    The consent redirect carries the article URL inside its own query string --
    `consent.google.com/m?continue=https://news.google.com/articles/<token>` -- so a substring
    test for the article path matched the consent hop too, and an `article` test placed first
    won. That is why walled exits reported twice the article GETs of clean ones and
    `consent_gets: 0` everywhere: the interstitial was being counted as the article.
    """
    parts = urlsplit(str(url))
    host, path = parts.netloc.lower(), parts.path
    if method.upper() == "POST":
        key = "post"
    elif host == "consent.google.com":
        key = "consent_get"
    elif host == "news.google.com" and (path.startswith("/articles") or path.startswith("/rss/articles")):
        key = "article_get"
    else:
        key = "other"
    with _counts_lock:
        counts[key] += 1


def _install_counter():
    """Attach to whichever client is present, and return what was attached.

    This patched `requests.Session.request` only, and the library stopped going through requests
    in a73356a. Every row since reported `article_gets: 0` -- correctly refused as a null ratio
    by the guard at the bottom rather than published as 0.0, but the cost of a decode simply
    stopped being measured, on the probe whose first line is that this is the solid number.

    `PoolManager.urlopen` is the seam for urllib3, not `.request`: urllib3 recurses through
    urlopen once per redirect hop, so a chain costs what it actually costs. Which is the reason
    this counts at the client at all -- the transport hands the whole chain over in one call.

    THE TWO SEAMS COUNT DIFFERENT UNITS, so `counted_at` in the row is not decoration. urllib3
    yields one per HTTP round trip, redirect hops included, which is the unit a per-request
    budget is actually spent in. requests yields one per logical fetch: `HTTPAdapter.send` calls
    `urlopen` on the connection pool, never on the PoolManager, and its redirects go back through
    `Session.send` rather than `Session.request`. So a ratio from one seam must not be compared
    with a ratio from the other -- including across builds via `./build <checkout>`.

    One blind spot either way: a pool-level retry recurses inside `HTTPConnectionPool.urlopen`
    and is invisible here. The transport sets every Retry count to 0, so nothing retries today.
    """
    patched = []

    try:
        import urllib3

        original_urlopen = urllib3.PoolManager.urlopen

        def counting_urlopen(self, method, url, *a, **kw):
            _bump(method, url)
            return original_urlopen(self, method, url, *a, **kw)

        # ProxyManager overrides urlopen and calls super(), so patching the base class counts a
        # proxied request exactly once, at the inner call.
        urllib3.PoolManager.urlopen = counting_urlopen
        patched.append("urllib3.PoolManager.urlopen")
    except ImportError:
        pass

    try:
        import requests

        original_request = requests.Session.request

        def counting_request(self, method, url, *a, **kw):
            _bump(method, url)
            return original_request(self, method, url, *a, **kw)

        requests.Session.request = counting_request
        patched.append("requests.Session.request")
    except ImportError:
        pass

    return patched


tokens = []
for q in QUERIES:
    if len(tokens) >= LIMIT:
        break
    try:
        tokens = dedupe(tokens + fresh_tokens(query=q.strip()))
    except Exception as e:
        print(f"query {q!r} failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
tokens = tokens[:LIMIT]
if not tokens:
    emit(error="no tokens from any query")
    raise SystemExit(1)

counted_at = _install_counter()

from googlenewsdecoder import decode_flow, drive, transports
from googlenewsdecoder.transports import RequestsTransport, TransportError

state = {"decoded": 0, "failed": 0, "first_429_at": None, "refusal": None, "done": 0}
_state_lock = threading.Lock()
_stop = threading.Event()


def _record(field, refusal=None):
    with _state_lock:
        state[field] += 1
        state["done"] += 1
        # First refusal wins. A worker still in flight when the 429 lands would otherwise
        # overwrite "429" with its own error name, and the run would report the wrong cause.
        if refusal and state["refusal"] is None:
            state["refusal"] = refusal
        if state["done"] % 50 == 0:
            print(
                f"progress: {state['done']}/{len(tokens)} attempted, "
                f"{state['decoded']} decoded, {counts['article_get']} article GETs",
                file=sys.stderr,
                flush=True,
            )


def _decode(numbered):
    i, tok = numbered
    if _stop.is_set():
        return
    try:
        result = drive(decode_flow(rss_url(tok)), RequestsTransport(), timeout=30)
    except TransportError as e:
        if getattr(e, "status", None) == 429:
            # First refusal by TOKEN order, not completion order: with WORKERS in flight the
            # two differ, and the ceiling is about how many the address accepted.
            with _state_lock:
                if state["first_429_at"] is None or i < state["first_429_at"]:
                    state["first_429_at"] = i
                state["refusal"] = "429"
            _stop.set()
            return
        _record("failed")
        return
    except Exception as e:
        _record("failed", refusal=type(e).__name__)
        return
    _record("decoded" if result.get("status") else "failed")
    if GAP:
        time.sleep(GAP)


with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    list(pool.map(_decode, enumerate(tokens, 1)))

# A decode cannot cost zero article GETs, so zero means the counter is not attached to the
# code under test -- not a cheap decode. Report the ratio as null rather than as 0.0, which
# reads like a measurement and would be cited as one.
counter_attached = counts["article_get"] > 0
if state["decoded"] and not counter_attached:
    print(
        f"WARNING: counted 0 article GETs across {state['decoded']} decodes -- nothing reached "
        f"{', '.join(counted_at) or 'any seam (no client could be patched)'}. "
        "Check which client the transport uses before trusting this row.",
        file=sys.stderr,
        flush=True,
    )

emit(
    has_consent_fix=hasattr(transports, "Urllib3Transport"),
    workers=WORKERS,
    tokens=len(tokens),
    attempted=state["decoded"] + state["failed"],
    decoded=state["decoded"],
    failed=state["failed"],
    first_429_at=state["first_429_at"],
    refusal=state["refusal"],
    # Where the number came from, and in which unit -- see `_install_counter`. A ratio is only
    # comparable with another row counted at the same seam.
    counted_at=",".join(counted_at),
    article_gets=counts["article_get"],
    consent_gets=counts["consent_get"],
    posts=counts["post"],
    # Everything else the decode spent: with a per-hop seam this bucket is where a redirect to
    # somewhere unexpected lands, and it was counted but never reported.
    other_requests=counts["other"],
    article_gets_per_decode=(
        round(counts["article_get"] / state["decoded"], 2)
        if state["decoded"] and counter_attached
        else None
    ),
)
