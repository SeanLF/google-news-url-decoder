"""Synchronous decoder.

A thin wrapper over `flow.decode_flow`, which holds the algorithm. Kept so existing
callers keep working; new code can use `googlenewsdecoder.decode()` directly.
"""

import time
from typing import Optional

from . import protocol
from .flow import decode_flow, drive
from .limits import DEFAULT_TIMEOUT, MAX_TOKEN_LENGTH, clamp_interval
from .transports import RequestsTransport


class GoogleDecoder:
    def __init__(self, proxy: Optional[str] = None, transport=None):
        """
        Parameters:
            proxy (str, optional): Proxy for all requests. http(s):// or socks5://.
            transport (callable, optional): How requests are sent -- any callable taking
                (Request, timeout=, proxy=) and returning the response text. Defaults to
                the standard-library transport, so the package needs no dependencies. Pass
                RequestsTransport() (pip install googlenewsdecoder[requests]) for connection
                pooling or a SOCKS proxy. See `transports`.
        """
        self.proxy = proxy
        self.transport = transport or RequestsTransport()

    def get_base64_str(self, source_url: str) -> dict:
        """The article token from a Google News URL."""
        token = protocol.article_id(source_url, max_length=MAX_TOKEN_LENGTH)
        if token is None:
            return {"status": False, "message": "Invalid Google News URL format."}
        return {"status": True, "base64_str": token}

    def decode_google_news_url(self, source_url: str, interval: Optional[int] = None) -> dict:
        """Decode a Google News article URL into its original source URL."""
        try:
            result = drive(
                decode_flow(source_url), self.transport, timeout=DEFAULT_TIMEOUT, proxy=self.proxy
            )
            if interval:
                time.sleep(clamp_interval(interval))
            return result
        except Exception as e:  # a transport may raise something we do not know about
            return {"status": False, "message": f"Error in decode_google_news_url: {str(e)}"}
