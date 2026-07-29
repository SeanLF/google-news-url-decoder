"""Does the async transport carry the consent bug the sync one had?

`HttpxAsyncTransport` builds `httpx.AsyncClient(follow_redirects=True)`. httpx clients keep a
cookie jar and resolve redirects internally, which is the shape that walls `requests`. The
`httpx_client` arm is that shape on its own; `async_transport` is what the library ships.
Run it on a walled exit or it proves nothing -- `bare` is the control that says which you are on.

The trailing urllib3 arms are a separate question kept in this row because it is cheap: they
cost no request and record whether proxy support is installed at all.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from _common import HEADERS, article_url, classify, emit, library_decode, record_body, require_tokens, rss_url

tokens = require_tokens(4)
out = {}

try:
    r = requests.get(article_url(tokens[0]), headers=HEADERS, timeout=30)
    out["bare"] = classify(r.text)
except Exception as e:
    out["bare"] = f"error {type(e).__name__}"

try:
    import httpx

    out["httpx_installed"] = True

    async def _fetch():
        async with httpx.AsyncClient(follow_redirects=True) as c:
            r = await c.get(article_url(tokens[1]), headers=HEADERS, timeout=30)
            return r.text

    record_body(out, "httpx_client", asyncio.run(_fetch()))
except ImportError:
    out["httpx_installed"] = False
except Exception as e:
    out["httpx_client"] = f"error {type(e).__name__}"

try:
    from googlenewsdecoder import decode_flow, drive_async
    from googlenewsdecoder.transports import HttpxAsyncTransport

    async def _decode():
        return await drive_async(decode_flow(rss_url(tokens[2])), HttpxAsyncTransport(), timeout=30)

    res = asyncio.run(_decode())
    out["async_transport"] = "decoded" if res.get("status") else "failed"
    out["async_detail"] = str(res.get("message") or "")[:60]
except Exception as e:
    out["async_transport"] = f"error {type(e).__name__}: {e}"[:70]

try:
    out["sync_transport"], _ = library_decode(tokens[3])
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
