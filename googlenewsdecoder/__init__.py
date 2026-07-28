"""Decode Google News article URLs into their original publisher URLs.

    from googlenewsdecoder import decode
    decode("https://news.google.com/rss/articles/CBMi...")
    # {"status": True, "decoded_url": "https://www.reuters.com/..."}

The package is layered, and you can enter at any level:

    errors      TransportError, the vocabulary shared across the seam
    protocol    pure functions over strings; no I/O, standard library only
    flow        the algorithm as a generator that yields requests
    transports  how requests actually get sent -- swappable, defaults to `requests`

`protocol` and `flow` do not import `transports`, so bringing your own I/O means importing
none of this package's HTTP code. That is checked in CI; see `.importlinter`.

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

from .__version__ import __version__
from .decoder import GoogleDecoder

# Imported unguarded: nothing on this path touches httpx until a transport is constructed,
# so the async API is importable everywhere and only *using* it without the extra fails --
# with a message from HttpxAsyncTransport naming the extra.
from .decoder_async import GoogleDecoderAsync
from .errors import TransportError
from .flow import decode_batch_flow, decode_flow, drive, drive_async
from .limits import DEFAULT_TIMEOUT
from .protocol import Request
from .transports import RequestsTransport, Transport, UrllibTransport


def decode(source_url: str, *, transport=None, proxy: str | None = None, interval: int | None = None) -> dict:
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
    source_url: str, *, transport=None, proxy: str | None = None, interval: int | None = None
) -> dict:
    """Decode one Google News URL asynchronously. Needs httpx unless you pass a transport."""
    decoder = GoogleDecoderAsync(proxy=proxy, transport=transport)
    try:
        return await decoder.decode_google_news_url(source_url, interval=interval)
    finally:
        await decoder.close()


def decode_batch(source_urls, *, transport=None, proxy: str | None = None, chunk_size: int = 50) -> list:
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
        # decode() turns a stray exception into a status dict; decode_batch let one escape to
        # the caller. It is reported per URL instead so the return type is honest.
        #
        # This does NOT preserve partial work: `drive` only returns on StopIteration, so an
        # unexpected exception mid-flow still loses every result computed before it and
        # replaces all of them with this same message.
        return [{"status": False, "message": f"Error in decode_batch: {e}"} for _ in source_urls]


# The five numbered decoders are gone. They were five standalone implementations of one
# decode, four of them carrying their own copy of the batchexecute envelope -- which is why a
# change at Google's end meant a new decoder version rather than an edit. Removing them rather
# than aliasing them is deliberate: their contracts disagreed with each other (bare string vs
# status dict, `url` vs `decoded_url`, raising vs not), so a silent alias would hand callers a
# shape they did not ask for. This names the replacement instead of failing with `AttributeError:
# decoderv3`, which tells nobody anything.
_REMOVED = {
    "decoderv1": (
        "protocol.embedded_url(protocol.article_id(url)) -- still pure and offline, but it "
        "returns None where decoderv1 handed back the original URL unchanged"
    ),
    "decoderv2": "decode(url); it returns {'status', 'decoded_url'} rather than a bare string",
    "decoderv3": "decode(url); the failure key is 'message', not 'error'",
    "decoderv4": "decode_batch(urls); results stay aligned to the input order",
    "new_decoderv1": "decode(url) -- same call, same return shape, current name",
}


def __getattr__(name: str):
    if name in _REMOVED:
        raise AttributeError(
            f"googlenewsdecoder.{name} was removed in 0.2. Use {_REMOVED[name]}. "
            "See the Migrating section of the README."
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Pre-1.0 names, kept working. `decode` and `decode_async` are the ones to reach for.
def gnewsdecoder(source_url: str, interval: int | None = None, proxy: str | None = None) -> dict:
    """Deprecated alias for `decode`."""
    return decode(source_url, proxy=proxy, interval=interval)


async def gnews_decoder_async(source_url: str, interval: int | None = None, proxy: str | None = None) -> dict:
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
    "__version__",
]
