"""Shared helpers for probes. Kept deliberately small."""
import gzip
import json
import os
import re
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
# What every non-urllib arm sends. Identical across probes on purpose: the arms differ by
# client, and a header that drifted between them would be a second variable.
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip"}
# Optional labels for the address under test, echoed into every result line. Useful when
# running the same probe from several addresses and comparing; ignorable otherwise.
EXIT = os.environ.get("EXIT_NAME", "?")
IP = os.environ.get("EXIT_IP", "?")
# How this row was carried. Rows land in one file per probe, appended across runs, so two rows
# from the same exit under different tunnels or resolvers are otherwise indistinguishable.
VPN = os.environ.get("VPN_TYPE", "?")
DNS = os.environ.get("DNS_MODE", "?")


def article_url(token):
    """The page the raw-fetch arms request."""
    return f"https://news.google.com/articles/{token}"


def rss_url(token):
    """The form a caller actually holds, and the only one the library accepts."""
    return f"https://news.google.com/rss/articles/{token}?oc=5"


def get(url, cookie=None, gzip_ok=True):
    h = {"User-Agent": UA}
    if gzip_ok:
        h["Accept-Encoding"] = "gzip"
    if cookie is not None:
        h["Cookie"] = cookie
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace"), len(raw)


def dedupe(tokens):
    """Distinct tokens, order preserved.

    Every probe here spends a per-IP budget that is counted in article GETs, so re-requesting a
    token buys no information and costs the same as a new one. Feed every token list through
    this, including ones read from a file.
    """
    # dict preserves insertion order and dedupes, so fromkeys is the whole algorithm.
    return [t for t in dict.fromkeys(s.strip() for s in tokens) if t]


def fresh_tokens(query="world", limit=200):
    """Live tokens from a search feed -- roughly 200 per query, free of the article budget.

    NOT because stored tokens expire. That was claimed here and is false: tokens sampled from
    across a large corpus collected over several weeks all still decoded. The original
    conclusion was drawn while the request envelope being sent was malformed, so *everything*
    failed and token age took the blame. Use a feed when you want tokens you have not already
    spent, not because the old ones went stale.
    """
    body, _ = get(f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en")
    return dedupe(re.findall(r"/rss/articles/([A-Za-z0-9_\-]+)", body))[:limit]


def require_tokens(n=1, **kw):
    """Fresh tokens, or one JSON error line and exit. Never a traceback and no line at all.

    Every wall probe indexes a fixed slot per arm (`tokens[3]`), so a short feed raised
    IndexError after the harness had already paid for the run, and the row simply went missing.
    A missing row reads as "not run yet"; a row saying the feed was short reads as what happened.
    """
    try:
        tokens = fresh_tokens(**kw)
    except Exception as e:
        emit(error=f"token fetch failed: {type(e).__name__}: {e}")
        raise SystemExit(1)
    if len(tokens) < n:
        emit(error=f"feed returned {len(tokens)} tokens, need {n}")
        raise SystemExit(1)
    return tokens


def classify(body):
    """What did Google actually serve us?"""
    if re.search(r'data-n-a-sg="[^"]+"', body):
        return "article"
    if "Before you continue" in body or "consent.google" in body:
        return "consent"
    return "unknown"


def record_body(out, label, body, size=None):
    """Store what Google served under `label`, plus its size -- the pair every wall probe emits.

    `size` is for callers that already know the byte count. Left None it falls back to the
    length of the decoded text, which is characters, not bytes; the two differ on non-ASCII
    markup, so do not compare a `_kb` taken one way against one taken the other.
    """
    out[label] = classify(body)
    out[label + "_kb"] = round((len(body) if size is None else size) / 1024)


def library_decode(token, timeout=30):
    """One decode through whatever sync transport this build ships. Returns (status, detail).

    Five probes carry this arm, and it is the only line in any of them that speaks for
    production, so it is the last one that should be allowed to drift between them. Imported
    lazily and by its old alias so the probe still runs against a build predating the rename.
    """
    from googlenewsdecoder import decode_flow, drive
    from googlenewsdecoder.transports import RequestsTransport

    result = drive(decode_flow(rss_url(token)), RequestsTransport(), timeout=timeout)
    detail = str(result.get("message") or result.get("decoded_url"))[:80]
    return ("decoded" if result.get("status") else "failed"), detail


def spend_until_refused(tokens, gap=0.4, on_each=None):
    """Fetch article pages until Google returns 429. Returns (n_clean, refused).

    The one loop `degrade`, `mixed` and `recovery` all need. They kept private copies, and the
    copies had already drifted on what to do with a non-429 HTTP error.

    `on_each(index, token, body)` runs for every successful fetch -- that hook is where the three
    probes genuinely differ: one classifies the body, one tracks request shape, one just counts.
    Failures other than 429 are skipped rather than counted, since one dead article says nothing
    about the budget.
    """
    clean = 0
    for i, token in enumerate(tokens):
        try:
            body, _ = get(f"https://news.google.com/articles/{token}")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return clean, True
            continue
        except Exception:
            continue
        clean += 1
        if on_each is not None:
            on_each(i, token, body)
        time.sleep(gap)
    return clean, False


def emit(**fields):
    print(json.dumps({"exit": EXIT, "ip": IP, "vpn": VPN, "dns": DNS, **fields}), flush=True)
