"""Does the async transport have the consent bug the sync one just had?

`HttpxAsyncTransport` builds `httpx.AsyncClient(follow_redirects=True)`. httpx clients keep a
cookie jar and resolve redirects internally, which is the same shape as `requests.request()`.
If so, the fix landed on one of three transports and the async path is still walled.

Also checks urllib3's ProxyManager exists for the proxy question, since that is the stated
reason RequestsTransport is the default over urllib.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import UA, classify, emit, fresh_tokens

HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip"}

try:
    tokens = fresh_tokens()
except Exception as e:
    emit(error=f"token fetch failed: {type(e).__name__}: {e}")
    raise SystemExit(1)

out = {}

import requests

try:
    r = requests.get(f"https://news.google.com/articles/{tokens[0]}", headers=HEADERS, timeout=30)
    out["bare"] = classify(r.text)
except Exception as e:
    out["bare"] = f"error {type(e).__name__}"

try:
    import httpx

    out["httpx_installed"] = True

    async def _run():
        async with httpx.AsyncClient(follow_redirects=True) as c:
            r = await c.get(f"https://news.google.com/articles/{tokens[1]}", headers=HEADERS, timeout=30)
            return r.text

    body = asyncio.run(_run())
    out["httpx_client"] = classify(body)
    out["httpx_client_kb"] = round(len(body) / 1024)
except ImportError:
    out["httpx_installed"] = False
except Exception as e:
    out["httpx_client"] = f"error {type(e).__name__}"

# The shipped async transport, end to end.
try:
    from googlenewsdecoder import decode_flow
    from googlenewsdecoder.transports import HttpxAsyncTransport

    async def _decode():
        from googlenewsdecoder import drive_async

        return await drive_async(
            decode_flow(f"https://news.google.com/rss/articles/{tokens[2]}?oc=5"),
            HttpxAsyncTransport(),
            timeout=30,
        )

    res = asyncio.run(_decode())
    out["async_transport"] = "decoded" if res.get("status") else "failed"
    out["async_detail"] = str(res.get("message") or "")[:60]
except Exception as e:
    out["async_transport"] = f"error {type(e).__name__}: {e}"[:70]

# Sync control.
try:
    from googlenewsdecoder import drive
    from googlenewsdecoder.transports import RequestsTransport

    res = drive(
        decode_flow(f"https://news.google.com/rss/articles/{tokens[3]}?oc=5"),
        RequestsTransport(),
        timeout=30,
    )
    out["sync_transport"] = "decoded" if res.get("status") else "failed"
except Exception as e:
    out["sync_transport"] = f"error {type(e).__name__}"

try:
    import urllib3

    out["urllib3_proxymanager"] = hasattr(urllib3, "ProxyManager")
    from urllib3.contrib.socks import SOCKSProxyManager  # noqa: F401

    out["urllib3_socks"] = True
except ImportError:
    out["urllib3_socks"] = False

emit(**out)
