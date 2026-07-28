"""Shared helpers for probes. Kept deliberately small."""
import gzip
import json
import os
import re
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
# Optional labels for the address under test, echoed into every result line. Useful when
# running the same probe from several addresses and comparing; ignorable otherwise.
EXIT = os.environ.get("EXIT_NAME", "?")
IP = os.environ.get("EXIT_IP", "?")


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
    seen, out = set(), []
    for t in tokens:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


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


def classify(body):
    """What did Google actually serve us?"""
    if re.search(r'data-n-a-sg="[^"]+"', body):
        return "article"
    if "Before you continue" in body or "consent.google" in body:
        return "consent"
    return "unknown"


def emit(**fields):
    print(json.dumps({"exit": EXIT, "ip": IP, **fields}), flush=True)
