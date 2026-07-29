"""What does one decode cost against the per-address budget, and how many do you get?

Two numbers. `article_gets_per_decode` is a ratio measured inside one run, so it needs no
second arm and survives a partly-spent budget. `first_429_at` is the ceiling, which does not.

Counting happens at `requests.Session.request`, not at the transport, because the transport
follows the redirect chain internally -- wrapping the transport would see one call where four
requests went out.

    ./probe SI-38 budget LIMIT=400 WORKERS=10
"""

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import emit, fresh_tokens

LIMIT = int(os.environ.get("LIMIT", "60"))
# The throttle is a per-address budget counted in requests, not a rate, and the one rate
# comparison on record went the other way (70 clean at 10/s, 33 throttled at 0.24/s). So
# pacing and serial execution buy wall-clock, not headroom.
GAP = float(os.environ.get("GAP", "0"))
WORKERS = int(os.environ.get("WORKERS", "10"))
QUERIES = os.environ.get("QUERIES", "world,business,science,sports,health").split(",")

counts = {"article_get": 0, "consent_get": 0, "other": 0, "post": 0}
_counts_lock = threading.Lock()


def _install_counter():
    import requests

    original = requests.Session.request

    def counting(self, method, url, *a, **kw):
        if method.upper() == "POST":
            key = "post"
        elif "news.google.com/articles" in url or "news.google.com/rss/articles" in url:
            key = "article_get"
        elif "consent.google.com" in url:
            key = "consent_get"
        else:
            key = "other"
        with _counts_lock:
            counts[key] += 1
        return original(self, method, url, *a, **kw)

    requests.Session.request = counting


tokens, seen = [], set()
for q in QUERIES:
    if len(tokens) >= LIMIT:
        break
    try:
        for t in fresh_tokens(query=q.strip()):
            if t not in seen:
                seen.add(t)
                tokens.append(t)
    except Exception as e:
        print(f"query {q!r} failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
tokens = tokens[:LIMIT]
if not tokens:
    emit(error="no tokens from any query")
    raise SystemExit(1)

_install_counter()

from googlenewsdecoder import decode_flow, drive, transports
from googlenewsdecoder.transports import RequestsTransport, TransportError

state = {"decoded": 0, "failed": 0, "first_429_at": None, "refusal": None, "done": 0}
_state_lock = threading.Lock()
_stop = threading.Event()


def _record(field, refusal=None):
    with _state_lock:
        state[field] += 1
        state["done"] += 1
        if refusal:
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
        result = drive(
            decode_flow(f"https://news.google.com/rss/articles/{tok}?oc=5"),
            RequestsTransport(),
            timeout=30,
        )
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

emit(
    has_consent_fix=hasattr(transports, "_follow_without_cookies"),
    workers=WORKERS,
    tokens=len(tokens),
    attempted=state["decoded"] + state["failed"],
    decoded=state["decoded"],
    failed=state["failed"],
    first_429_at=state["first_429_at"],
    refusal=state["refusal"],
    article_gets=counts["article_get"],
    consent_gets=counts["consent_get"],
    posts=counts["post"],
    article_gets_per_decode=(
        round(counts["article_get"] / state["decoded"], 2) if state["decoded"] else None
    ),
)
