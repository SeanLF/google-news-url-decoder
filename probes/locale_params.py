"""Does sending locale parameters up front save a round trip per decode?

`protocol.params_urls` emits `/articles/<token>` with no query, and Google answers 302 to the same
path plus `?hl=..&gl=..&ceid=..`. Measured on a clean exit, one decode costs three round trips:
that redirect, the article, and the RPC POST. If the redirect can be skipped by sending the
parameters ourselves, a third of the requests per decode go away -- against a throttle counted in
requests, that is a third more decodes per address.

Three arms per token, one article GET each, so the whole probe costs three GETs:

    bare        what the library sends today. The control: it must 302, or this exit is walled
                and the row says nothing about locale.
    assigned    the locale Google's own redirect asked for, read from the bare arm's Location.
                The best case for skipping.
    forced      a fixed en-US/US/US:en, which is what a library-wide default would send. Answers
                the separate question of whether a locale that disagrees with the exit's
                geography is accepted or redirected again.

`skipped` is the answer: 200 on the first request with a page that parses. A row where the bare
arm 302s to consent.google.com instead is a walled exit and `locale_redirect` is false --
attempting this from a walled address is how an earlier hand-run of it came back inconclusive.

    ./probes/runner/probe latvia locale SERVER_HOSTNAMES=node-lv-01.protonvpn.net
"""

import os
import sys
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import HEADERS, article_url, emit, require_tokens

from googlenewsdecoder import protocol

LOCALE_KEYS = ("hl", "gl", "ceid")
FORCED = "hl=en-US&gl=US&ceid=US:en"


def fetch(pool, url):
    """(status, Location, parses) for one article GET, following nothing."""
    import urllib3

    try:
        r = pool.request("GET", url, headers=HEADERS, redirect=False, preload_content=False)
    except urllib3.exceptions.HTTPError as e:
        return f"error {type(e).__name__}", None, None
    body = r.read(8_000_000) if r.status == 200 else b""
    r.drain_conn()
    parses = protocol.parse_params(body.decode("utf-8", "replace")) is not None if body else None
    return r.status, r.headers.get("Location"), parses


def main():
    import urllib3

    tokens = require_tokens(1)
    pool = urllib3.PoolManager()
    out = {}
    try:
        base = article_url(tokens[0])

        status, location, _ = fetch(pool, base)
        out["bare_status"] = status
        # A 302 whose target is the same path with a query is the locale redirect. One to another
        # host is the consent wall, and then this exit cannot answer the question at all.
        target = urlsplit(location or "")
        out["locale_redirect"] = bool(location) and target.netloc in ("", "news.google.com")
        out["walled"] = bool(location) and target.netloc == "consent.google.com"

        assigned = {k: v[0] for k, v in parse_qs(target.query).items() if k in LOCALE_KEYS}
        out["assigned_locale"] = "&".join(f"{k}={assigned[k]}" for k in LOCALE_KEYS if k in assigned)

        if out["assigned_locale"]:
            status, location, parses = fetch(pool, f"{base}?{out['assigned_locale']}")
            out["assigned_status"], out["assigned_parses"] = status, parses
            out["assigned_skipped"] = status == 200 and parses is True

        status, location, parses = fetch(pool, f"{base}?{FORCED}")
        out["forced_status"], out["forced_parses"] = status, parses
        out["forced_skipped"] = status == 200 and parses is True
        # Where a forced locale gets sent instead, when it is not accepted: a redirect back to a
        # different locale means Google overrides it, which a library-wide default cannot ignore.
        if location:
            out["forced_redirected_to"] = "&".join(
                f"{k}={v[0]}" for k, v in parse_qs(urlsplit(location).query).items() if k in LOCALE_KEYS
            )
    finally:
        pool.clear()

    emit(**out)


main()
