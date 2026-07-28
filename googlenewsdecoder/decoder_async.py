"""Asynchronous decoder.

Identical to the synchronous one except for which driver it calls. Before the algorithm
moved into `flow`, this class and `GoogleDecoder` were 88% the same code.
"""

import asyncio

from . import protocol
from .flow import decode_flow, drive_async
from .limits import DEFAULT_TIMEOUT, MAX_TOKEN_LENGTH, clamp_interval
from .transports import HttpxAsyncTransport


class GoogleDecoderAsync:
    def __init__(self, proxy: str | None = None, transport=None):
        """
        Parameters:
            proxy (str, optional): Proxy for all requests.
            transport (async callable, optional): Defaults to httpx.
        """
        self.proxy = proxy
        self.transport = transport or HttpxAsyncTransport(proxy=proxy)
        # Kept because callers reached into .client directly.
        self.client = getattr(self.transport, "_client", None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def get_base64_str(self, source_url: str) -> dict:
        """The article token from a Google News URL."""
        token = protocol.article_id(source_url, max_length=MAX_TOKEN_LENGTH)
        if token is None:
            return {"status": False, "message": "Invalid Google News URL format."}
        return {"status": True, "base64_str": token}

    async def decode_google_news_url(self, source_url: str, interval: int | None = None) -> dict:
        """Decode a Google News article URL into its original source URL."""
        try:
            result = await drive_async(
                decode_flow(source_url), self.transport, timeout=DEFAULT_TIMEOUT, proxy=self.proxy
            )
            if interval:
                await asyncio.sleep(clamp_interval(interval))
            return result
        except Exception as e:
            return {"status": False, "message": f"Error in decode_google_news_url: {str(e)}"}

    async def close(self):
        aclose = getattr(self.transport, "aclose", None)
        if aclose is not None:
            await aclose()
