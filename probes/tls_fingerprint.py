"""TLS fingerprint is NOT why Google walls one client and not another. Kept as the proof.

`stdlib_like_context()` closes urllib3's two deviations from a stdlib context, making
`requests` present a JA4 byte-identical to urllib's. Against a walled address that client was
still served the interstitial. See `probes/README.md` for the cause and the ruled-out list.

Makes no Google requests, so it costs nothing against the article budget and runs from an
address that is currently refused.

    python probes/tls_fingerprint.py
"""

import os
import ssl
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import UA, emit

ENDPOINT = "https://tls.peet.ws/api/all"
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip"}


def stdlib_like_context():
    """A urllib3 context with urllib3's two fingerprint deviations undone.

    Mount on a Session via an HTTPAdapter that passes `ssl_context` to `init_poolmanager`.
    """
    from urllib3.util.ssl_ import create_urllib3_context

    ctx = create_urllib3_context()
    ctx.post_handshake_auth = False
    ctx.options &= ~ssl.OP_NO_TICKET
    return ctx


def _by_urllib():
    """Stdlib client, with ALPN set to match requests so ALPN is not the difference."""
    import gzip
    import json
    import urllib.request

    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    with opener.open(urllib.request.Request(ENDPOINT, headers=HEADERS), timeout=30) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw.decode())


def _by_requests(ctx=None):
    import requests
    from requests.adapters import HTTPAdapter

    session = requests.Session()
    if ctx is not None:

        class _Adapter(HTTPAdapter):
            def init_poolmanager(self, *a, **kw):
                kw["ssl_context"] = ctx
                return super().init_poolmanager(*a, **kw)

        session.mount("https://", _Adapter())
    return session.get(ENDPOINT, headers=HEADERS, timeout=30).json()


def _read(payload):
    tls = payload.get("tls", {}) or {}
    names = sorted(str(e.get("name", "?")).split(" ")[0] for e in (tls.get("extensions") or []))
    return tls.get("ja4"), names


try:
    urllib_ja4, urllib_exts = _read(_by_urllib())
    default_ja4, default_exts = _read(_by_requests())
    patched_ja4, patched_exts = _read(_by_requests(stdlib_like_context()))
except Exception as e:
    emit(error=f"{type(e).__name__}: {e}")
    raise SystemExit(1)

emit(
    urllib_ja4=urllib_ja4,
    requests_ja4=default_ja4,
    requests_patched_ja4=patched_ja4,
    default_matches_urllib=default_ja4 == urllib_ja4,
    patched_matches_urllib=patched_ja4 == urllib_ja4,
    only_in_requests=sorted(set(default_exts) - set(urllib_exts)),
    only_in_urllib=sorted(set(urllib_exts) - set(default_exts)),
    residual_delta=sorted(set(patched_exts) ^ set(urllib_exts)),
)
