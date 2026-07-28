"""Transports: how a `protocol.Request` actually gets sent.

A transport is any callable taking `(Request, timeout=..., proxy=...)` and returning
the response body as text, raising `TransportError` on failure. `Transport` below is a
`typing.Protocol`, so a type checker verifies your object matches without you inheriting
from anything -- structural typing, checked statically.

    def my_transport(request, *, timeout=None, proxy=None):
        r = my_session.request(request.method, request.url,
                               headers=request.headers, data=request.body)
        return r.text

    GoogleDecoder(transport=my_transport)

`requests` stays the default, so existing behaviour, proxy handling and dependencies are
unchanged. `UrllibTransport` is offered for callers who would rather add no dependency at
all; it is deliberately not the default, because `requests` does several things urllib
does not -- transparent decompression, `NO_PROXY` that does not override an explicitly
configured proxy, authenticated SOCKS -- and switching the default would quietly change
all of them.

**Concurrency.** `_protocol` is pure and stateless, so it is safe to share across threads,
processes and event loops without qualification. Everything with a concurrency hazard in it
is a transport, which is the caller's choice: the default `RequestsTransport` holds no state
and is thread-safe but pools no connections; supply a `Session`-backed transport if you want
pooling, and give each thread its own if you want both. `HttpxAsyncTransport` holds a client,
which httpx supports for concurrent use within one event loop.

**Composition.** A transport is a callable, so wrapping one is ordinary decoration -- retries,
caching, rate limiting and logging are all just another transport:

    def with_retries(inner, attempts=3):
        def transport(request, **kw):
            for i in range(attempts):
                try:
                    return inner(request, **kw)
                except TransportError as e:
                    if i == attempts - 1 or e.status not in (429, 503):
                        raise
                    time.sleep(2 ** i)
        return transport

    GoogleDecoder(transport=with_retries(RequestsTransport()))
"""

from typing import Optional, Protocol, runtime_checkable

from .limits import MAX_RESPONSE_BYTES
from .protocol import Request

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

# Google serves a "Before you continue" consent interstitial to SOME addresses instead of the
# article. It carries no signature, so the decode fails with a message about missing data
# attributes that points at the markup rather than the real cause.
#
# UNVERIFIED as a fix. Observed once, on a walled address, that sending a consent choice
# returned the article (1.06 MB) where omitting it returned the interstitial (661 KB). That
# could not be reproduced through this transport, because the wall is address-dependent and
# a walled address could not be obtained on demand afterwards. Sending this is harmless on
# addresses that are not walled (checked: identical response with and without), so it stays
# as a cheap best-effort -- but do not treat the interstitial as solved.
DEFAULT_COOKIE = "CONSENT=YES+cb.20210328-17-p0.en+FX+000"


class TransportError(Exception):
    """A transport-level failure, independent of which client produced it.

    `status` carries the HTTP status when there was one, so a caller can back off on 429
    and skip on 404 without knowing which library did the sending.
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@runtime_checkable
class Transport(Protocol):
    """The contract a transport satisfies. Structural: implement the call, that is all."""

    def __call__(self, request: Request, *, timeout: Optional[float] = None, proxy: Optional[str] = None) -> str: ...


class _HeaderMixin:
    """Merges caller-supplied headers over the protocol's, so anything is overridable.

    The protocol emits only what the RPC requires (a Content-Type on the POST). Identity
    -- User-Agent, Accept-Language, an API key on a corporate egress proxy -- is a
    transport concern, and the caller's value wins.
    """

    def _headers(self, request: Request) -> dict:
        merged = {"User-Agent": DEFAULT_USER_AGENT, "Cookie": DEFAULT_COOKIE}
        merged.update(request.headers)
        merged.update(self.headers or {})
        return merged


class RequestsTransport(_HeaderMixin):
    """The default. Preserves the behaviour this package has always had.

    Stateless, so it is safe to share across threads. It opens a fresh connection per
    request; pass a `Session`-backed transport instead if you want connection pooling.
    """

    def __init__(self, headers: Optional[dict] = None):
        self.headers = headers

    def __call__(self, request: Request, *, timeout: Optional[float] = None, proxy: Optional[str] = None) -> str:
        import requests

        proxies = {"http": proxy, "https": proxy} if proxy else None
        try:
            response = requests.request(
                request.method,
                request.url,
                headers=self._headers(request),
                data=request.body,
                proxies=proxies,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.text
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            raise TransportError(str(e), status=status) from e
        except requests.exceptions.RequestException as e:
            raise TransportError(str(e)) from e


class UrllibTransport(_HeaderMixin):
    """A standard-library transport, for callers who want no dependencies at all.

    Deliberately not the default; see the module docstring for what `requests` does that
    this does not.
    """

    def __init__(self, headers: Optional[dict] = None):
        self.headers = headers

    def __call__(self, request: Request, *, timeout: Optional[float] = None, proxy: Optional[str] = None) -> str:
        import gzip
        import urllib.error
        import urllib.request
        import zlib

        # urllib has no SOCKS support: ProxyHandler would connect to the proxy host and
        # speak HTTP at it, failing with a confusing "connection reset" rather than saying
        # what is wrong. Refuse clearly instead -- RequestsTransport handles socks via PySocks.
        if proxy and proxy.lower().startswith("socks"):
            raise TransportError(
                "UrllibTransport cannot use a SOCKS proxy; use RequestsTransport "
                "(pip install googlenewsdecoder[socks]) or supply your own transport"
            )
        headers = self._headers(request)
        # urllib sends `Accept-Encoding: identity` and does not decompress, so without this
        # every article page arrives uncompressed. Measured on a real page: 1,130,320 bytes
        # identity vs 174,988 gzipped -- 6.5x, and this endpoint rate-limits.
        headers.setdefault("Accept-Encoding", "gzip, deflate")
        req = urllib.request.Request(  # nosec B310 - https URLs built by protocol
            request.url, data=request.body, headers=headers, method=request.method
        )
        if proxy:
            # Set the proxy on the request rather than via ProxyHandler. ProxyHandler consults
            # proxy_bypass() even for a proxy passed in explicitly, so an unrelated NO_PROXY in
            # the environment silently routes traffic direct -- a caller using a proxy for
            # egress control would lose it with no error. requests lets an explicit proxy win;
            # match that. (Patching proxy_bypass globally would work and is not thread-safe.)
            from urllib.parse import urlparse as _urlparse

            parsed = _urlparse(proxy)
            req.set_proxy(parsed.netloc, parsed.scheme)
        try:
            with urllib.request.build_opener().open(req, timeout=timeout) as response:
                raw = response.read()
                encoding = (response.headers.get("Content-Encoding") or "").lower()
        except urllib.error.HTTPError as e:
            raise TransportError(f"HTTP {e.code}: {e.reason}", status=e.code) from e
        except urllib.error.URLError as e:
            raise TransportError(str(e.reason)) from e

        return _decompress(raw, encoding).decode("utf-8", "replace")


class HttpxAsyncTransport(_HeaderMixin):
    """The default for `GoogleDecoderAsync`. Owns its client unless you pass one in.

    An httpx client is safe for concurrent use within one event loop, so one transport can
    serve many in-flight decodes.
    """

    def __init__(self, client=None, proxy: Optional[str] = None, headers: Optional[dict] = None):
        import httpx

        self.headers = headers
        self._client = client or httpx.AsyncClient(proxy=proxy, follow_redirects=True)
        self._owns_client = client is None

    async def __call__(self, request: Request, *, timeout: Optional[float] = None, proxy: Optional[str] = None) -> str:
        import httpx

        try:
            response = await self._client.request(
                request.method,
                request.url,
                headers=self._headers(request),
                content=request.body,
                # Both were accepted and silently dropped: a caller supplying their own client
                # got no timeout and unproxied traffic, failing open in both cases.
                **({"timeout": timeout} if timeout is not None else {}),
                **({"extensions": {"proxy": proxy}} if proxy and self._owns_client is False else {}),
            )
            response.raise_for_status()
            return response.text
        except httpx.HTTPStatusError as e:
            raise TransportError(str(e), status=e.response.status_code) from e
        except httpx.RequestError as e:
            raise TransportError(str(e)) from e

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class AdaptiveRateLimit:
    """Wraps a transport and discovers the endpoint's rate limit instead of being told it.

    Google publishes no limit for this endpoint, sends no Retry-After, and enforces it per
    IP in a way that depends on that address's recent history -- the same pacing that ran
    96 requests clean on one exit throttled after ~20 on another. So a configured interval
    is a guess that is wrong somewhere.

    This is additive-increase / multiplicative-decrease, the control law TCP uses, applied to
    the REQUEST RATE rather than to the delay. On a 429 the rate is halved; after a run of
    successes it gains a fixed increment. Working in rate space matters: additively shrinking
    the *delay* instead sounds equivalent and is not, because delay is the reciprocal of rate.
    Recovering a 32s gap back to 2s takes ~47 requests in rate space against ~600 by
    subtracting a fixed step from the delay, which is indistinguishable from never recovering.

    Share ONE instance across every decoder in a process. The budget belongs to the IP, so a
    per-instance limiter divides attention without dividing the load.
    """

    def __init__(
        self,
        inner,
        initial: float = 2.0,
        floor: float = 0.25,
        ceiling: float = 120.0,
        backoff: float = 2.0,  # rate is divided by this on a 429
        probe: float = 0.05,   # requests/sec added per success run (additive increase)
        success_run: int = 5,
        retries: int = 6,
        sleep=None,
        observer=None,
    ):
        import threading

        self.inner = inner
        self.gap = initial
        self.floor, self.ceiling = floor, ceiling
        self.backoff, self.probe = backoff, probe
        self.success_run, self.retries = success_run, retries
        self._sleep = sleep or __import__("time").sleep
        self._observer = observer
        self._streak = 0
        self._lock = threading.Lock()

    def __call__(self, request: Request, *, timeout: Optional[float] = None, proxy: Optional[str] = None) -> str:
        import random

        for attempt in range(self.retries):
            with self._lock:
                gap = self.gap
            self._sleep(gap * (1 + random.random() * 0.1))  # jitter, so parallel callers desynchronise
            try:
                body = self.inner(request, timeout=timeout, proxy=proxy)
            except TransportError as e:
                if e.status != 429:
                    raise
                with self._lock:
                    rate = (1.0 / self.gap) / self.backoff        # multiplicative decrease
                    self.gap = min(1.0 / rate, self.ceiling)
                    self._streak = 0
                    widened = self.gap
                if self._observer:
                    self._observer(attempt, widened, "429")
                if attempt == self.retries - 1:
                    raise
                continue
            with self._lock:
                self._streak += 1
                if self._streak >= self.success_run:
                    self.gap = max(self.gap - self.probe, self.floor)
                    self._streak = 0
                narrowed = self.gap
            if self._observer:
                self._observer(attempt, narrowed, "ok")
            return body
        raise TransportError("rate limited beyond the retry budget", status=429)


def _decompress(raw: bytes, encoding: str) -> bytes:
    """Decompress within a size bound, reporting failures as TransportError.

    Both parts matter. Unbounded, a small body can expand until the process dies. And a
    body whose declared encoding does not match its content raises BadGzipFile, zlib.error
    or EOFError -- none of which a caller catching TransportError would see, so a malformed
    response would escape as an unrelated exception and take a whole batch with it.
    """
    import gzip
    import zlib

    if not encoding:
        return raw
    try:
        if encoding == "gzip":
            with gzip.GzipFile(fileobj=__import__("io").BytesIO(raw)) as f:
                out = f.read(MAX_RESPONSE_BYTES + 1)
        elif encoding == "deflate":
            try:  # servers disagree on whether "deflate" means zlib-wrapped or raw
                obj = zlib.decompressobj()
                out = obj.decompress(raw, MAX_RESPONSE_BYTES + 1)
            except zlib.error:
                obj = zlib.decompressobj(-zlib.MAX_WBITS)
                out = obj.decompress(raw, MAX_RESPONSE_BYTES + 1)
        else:
            # An encoding we do not implement. Returning the raw bytes would surface later as
            # a confusing "no data attributes" parse failure rather than as what it is.
            raise TransportError(f"unsupported Content-Encoding: {encoding}")
    except TransportError:
        raise
    except Exception as e:
        raise TransportError(f"could not decode {encoding} response: {e}") from e
    if len(out) > MAX_RESPONSE_BYTES:
        raise TransportError(f"response exceeded {MAX_RESPONSE_BYTES} bytes decompressed")
    return out
