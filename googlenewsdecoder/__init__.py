"""Decode Google News article URLs into their original publisher URLs.

    from googlenewsdecoder import decode
    decode("https://news.google.com/rss/articles/CBMi...")
    # {"status": True, "decoded_url": "https://www.reuters.com/..."}

The package is three layers, and you can enter at any of them:

    protocol    pure functions over strings; no I/O, standard library only
    flow        the algorithm as a generator that yields requests
    transports  how requests actually get sent -- swappable, defaults to `requests`

Bring your own HTTP client by passing anything callable::

    from googlenewsdecoder import decode
    from googlenewsdecoder.transports import UrllibTransport

    decode(url, transport=UrllibTransport())            # no third-party dependency
    decode(url, transport=my_session_backed_callable)   # your pooling, retries, tracing

Because a transport is just a callable, rate limiting, caching, retries and tracing are
ordinary wrappers around one -- see `transports` for worked examples. Note that Google
throttles per IP, so a limiter must be *shared* across decoders rather than held per
instance, and heavy concurrency against this endpoint is self-defeating.
"""

from typing import Optional

from .__version__ import __version__
from .flow import decode_batch_flow, decode_flow, drive, drive_async
from .new_decoderv2 import GoogleDecoder

try:  # the pre-1.0 decoders still import `requests` directly at module scope
    from .decoderv1 import decode_google_news_url as decoderv1
    from .decoderv2 import decode_google_news_url as decoderv2
    from .decoderv3 import decode_google_news_url as decoderv3
    from .decoderv4 import decode_google_news_url as decoderv4
    from .new_decoderv1 import decode_google_news_url as new_decoderv1
except ImportError:  # pragma: no cover - only when the `requests` extra is absent
    decoderv1 = decoderv2 = decoderv3 = decoderv4 = new_decoderv1 = None
from .protocol import Request
from .limits import DEFAULT_TIMEOUT
from .transports import RequestsTransport, Transport, TransportError, UrllibTransport

try:  # async support is optional: pip install googlenewsdecoder[async]
    from .new_decoderv3 import GoogleDecoderAsync
except ImportError:  # pragma: no cover - depends on httpx being installed
    GoogleDecoderAsync = None


def decode(source_url: str, *, transport=None, proxy: Optional[str] = None, interval: Optional[int] = None) -> dict:
    """Decode one Google News URL.

    Parameters:
        source_url: The Google News article URL.
        transport:  How to send requests. Defaults to `requests`.
        proxy:      Proxy for all requests.
        interval:   Seconds to wait after decoding, to pace a batch.

    Returns:
        {"status": True, "decoded_url": ...} or {"status": False, "message": ...}
    """
    return GoogleDecoder(proxy=proxy, transport=transport).decode_google_news_url(source_url, interval=interval)


async def decode_async(
    source_url: str, *, transport=None, proxy: Optional[str] = None, interval: Optional[int] = None
) -> dict:
    """Decode one Google News URL asynchronously. Needs httpx unless you pass a transport."""
    if GoogleDecoderAsync is None:
        raise ImportError("async decoding requires httpx: pip install googlenewsdecoder[async]")
    decoder = GoogleDecoderAsync(proxy=proxy, transport=transport)
    try:
        return await decoder.decode_google_news_url(source_url, interval=interval)
    finally:
        await decoder.close()


def decode_batch(source_urls, *, transport=None, proxy: Optional[str] = None, chunk_size: int = 50) -> list:
    """Decode many Google News URLs, sharing one POST per chunk.

    Costs one signature fetch per URL plus one POST per `chunk_size` URLs, rather than two
    requests each. Results are returned in the SAME ORDER as the input; the endpoint
    answers a batch in an arbitrary order and the mapping is handled here, because getting
    it wrong pairs an article with another article's URL rather than failing visibly.
    """
    if not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError(f"chunk_size must be a positive integer, got {chunk_size!r}")
    source_urls = list(source_urls)
    try:
        return drive(
            decode_batch_flow(source_urls, chunk_size=chunk_size),
            transport or RequestsTransport(),
            timeout=DEFAULT_TIMEOUT,
            proxy=proxy,
        )
    except Exception as e:
        # decode() turns a stray exception into a status dict; decode_batch let it escape and
        # discard every already-decoded result with it. Report per URL instead.
        return [{"status": False, "message": f"Error in decode_batch: {e}"} for _ in source_urls]


# Pre-1.0 names, kept working. `decode` and `decode_async` are the ones to reach for.
def gnewsdecoder(source_url: str, interval: Optional[int] = None, proxy: Optional[str] = None) -> dict:
    """Deprecated alias for `decode`."""
    return decode(source_url, proxy=proxy, interval=interval)


async def gnews_decoder_async(source_url: str, interval: Optional[int] = None, proxy: Optional[str] = None) -> dict:
    """Deprecated alias for `decode_async`."""
    return await decode_async(source_url, proxy=proxy, interval=interval)


__all__ = [
    # the API to use
    "decode",
    "decode_async",
    "decode_batch",
    "GoogleDecoder",
    "GoogleDecoderAsync",
    # bring your own transport
    "Transport",
    "TransportError",
    "RequestsTransport",
    "UrllibTransport",
    "Request",
    # drive the algorithm yourself
    "decode_flow",
    "decode_batch_flow",
    "drive",
    "drive_async",
    # pre-1.0 names
    "gnewsdecoder",
    "gnews_decoder_async",
    "decoderv1",
    "decoderv2",
    "decoderv3",
    "decoderv4",
    "new_decoderv1",
    "__version__",
]
