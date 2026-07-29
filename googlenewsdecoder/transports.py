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

The shipped transports are `Urllib3Transport` (sync default) and `HttpxAsyncTransport`.
This decode needs a client that never replays a cookie across a redirect, because Google's
consent endpoint walls one that does. urllib3 gives that for free, along with cross-origin
credential stripping; httpx does not, so the async transport resolves the chain itself and
applies both rules explicitly. `RequestsTransport` is an alias of `Urllib3Transport` --
urllib3 is what `requests` uses underneath.

One deliberate difference from `requests`: environment `HTTP_PROXY`/`NO_PROXY` are not
consulted, so pass `proxy=` explicitly. That makes an explicit proxy always win by
construction, which is the behaviour the old urllib path reconstructed by hand.

**Concurrency.** `protocol` is pure and stateless, so it is safe to share across threads,
processes and event loops without qualification. `Urllib3Transport` holds a PoolManager per
proxy behind a lock and is safe to share; sharing one is also what you want, since the
throttle counts connections. `HttpxAsyncTransport` holds a client, which httpx supports for
concurrent use within one event loop.

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

Retrying is not always the right answer to a 429 here. Where the limit behaves as a per-address
*budget* rather than a rate -- see `probes/` -- backing off and trying again does not recover it,
so a caller decoding many URLs usually wants to stand down for the rest of the batch instead.
The same seam expresses that, by turning the status into an exception of your own:

    class RateLimited(Exception):
        pass

    def stop_on_429(inner):
        def transport(request, **kw):
            try:
                return inner(request, **kw)
            except TransportError as e:
                if e.status == 429:
                    raise RateLimited from e
                raise
        return transport

Worth doing at the transport rather than on the result: by the time the flow has turned a
TransportError into `{"status": False, "message": ...}`, the only thing left to key off is prose.
`RateLimited` is not a TransportError, so it propagates out of `drive` to your loop untouched.
"""

import threading
from typing import Protocol, runtime_checkable

# Re-exported, not defined here: `TransportError` moved to `errors` so that `flow` can name it
# without importing this module. Kept importable from here because it always has been.
from .errors import TransportError
from .limits import MAX_RESPONSE_BYTES
from .protocol import Request

__all__ = [
    "DEFAULT_USER_AGENT",
    "HttpxAsyncTransport",
    "RequestsTransport",
    "Transport",
    "TransportError",
    "Urllib3Transport",
]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


@runtime_checkable
class Transport(Protocol):
    """The contract a transport satisfies. Structural: implement the call, that is all."""

    def __call__(self, request: Request, *, timeout: float | None = None, proxy: str | None = None) -> str: ...


class _HeaderMixin:
    """Merges caller-supplied headers over the protocol's, so anything is overridable.

    The protocol emits only what the RPC requires (a Content-Type on the POST). Identity
    -- User-Agent, Accept-Language, an API key on a corporate egress proxy -- is a
    transport concern, and the caller's value wins.
    """

    def _headers(self, request: Request) -> dict:
        # urllib3 sends `Accept-Encoding: identity` unless asked otherwise. Measured on a real
        # article page: 1,130,320 bytes identity against 174,988 gzipped.
        merged = {"User-Agent": DEFAULT_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
        merged.update(request.headers)
        merged.update(self.headers or {})
        return merged


MAX_REDIRECTS = 10

# Bytes we will read and throw away to keep a pooled connection alive after giving up on a
# body. The throttle on this endpoint counts connections, so a small declared remainder is
# worth discarding; an unknown or large one is the peer's number to choose, not ours.
MAX_DRAIN_BYTES = 1 << 20

# What urllib3 will actually decode. Anything else must be refused rather than passed through.
_DECODERS = frozenset({"gzip", "deflate", "br", "zstd", "identity"})

# Headers scoped to the origin that issued them; dropped on a cross-origin redirect.
_PER_ORIGIN = frozenset({"authorization", "cookie", "proxy-authorization"})


class Urllib3Transport(_HeaderMixin):
    """The default. Redirect resolution, connection pooling and decompression come from
    urllib3, which requests already depends on.

    urllib3 implements no cookie support at all -- its pools are connection pools, not
    stateful clients -- which is the property this decode needs: Google's consent endpoint
    walls any client that replays the `SOCS` cookie it sets on the article's 302. It also
    strips `Authorization`, `Cookie` and `Proxy-Authorization` on a cross-origin redirect,
    drops `Content-Type` when a 303 turns a POST into a GET, and resolves relative
    `Location` headers. Hand-rolling that loop got the cookie right and the credential
    stripping wrong.

    One PoolManager per proxy, kept on the instance. That is not only a latency saving: the
    throttle on this endpoint counts CONNECTIONS, so an unpooled client is refused on every
    address tested after 65-110 of them while a pooled one runs on a single connection. See
    `probes/connections.py`.
    """

    def __init__(self, headers: dict | None = None):
        self.headers = headers
        self._pools: dict[str | None, object] = {}
        self._pools_lock = threading.Lock()

    def _pool(self, proxy: str | None):
        with self._pools_lock:
            pool = self._pools.get(proxy)
            if pool is not None:
                return pool
            import urllib3

            # Redirects yes, retries no: a retry on this endpoint spends budget without
            # recovering it, and the caller decides that policy by wrapping the transport.
            retries = urllib3.Retry(
                total=None, connect=0, read=0, status=0, other=0, redirect=MAX_REDIRECTS,
                raise_on_status=False,
            )
            if proxy is None:
                pool = urllib3.PoolManager(retries=retries)
            elif proxy.lower().startswith("socks"):
                try:
                    from urllib3.contrib.socks import SOCKSProxyManager
                except ImportError as e:
                    raise TransportError(
                        "SOCKS proxies need PySocks: pip install googlenewsdecoder[socks]"
                    ) from e
                pool = SOCKSProxyManager(proxy, retries=retries)
            else:
                pool = urllib3.ProxyManager(proxy, retries=retries)
            self._pools[proxy] = pool
            return pool

    def __call__(self, request: Request, *, timeout: float | None = None, proxy: str | None = None) -> str:
        import urllib3

        try:
            urllib3_timeout = urllib3.Timeout(total=timeout) if timeout is not None else None
        except ValueError as e:
            raise TransportError(f"invalid timeout: {e}") from e

        try:
            response = self._pool(proxy).request(
                request.method,
                request.url,
                body=request.body,
                headers=self._headers(request),
                timeout=urllib3_timeout,
                # Read bounded rather than preloaded. urllib3 does NOT cap decompression --
                # measured returning a 33 MB body from a 32 KB gzip -- so `.data` would
                # allocate the bomb before any check of ours could refuse it.
                preload_content=False,
            )
        except urllib3.exceptions.HTTPError as e:
            raise TransportError(str(e)) from e

        try:
            # >= 300, not >= 400: a 3xx urllib3 declined to follow -- no Location, or a status
            # outside its REDIRECT_STATUSES -- would otherwise be handed back as the article
            # body, which is the failure this release exists to remove.
            if response.status >= 300:
                raise TransportError(f"HTTP {response.status}", status=response.status)
            encoding = (response.headers.get("content-encoding") or "").lower()
            if encoding and encoding not in _DECODERS:
                # urllib3 installs a decoder only for encodings it knows and passes anything
                # else through undecoded, which then becomes mojibake and is blamed on Google's
                # markup. brotli and zstd arrive from CDNs whether or not we advertised them.
                raise TransportError(f"unsupported Content-Encoding: {encoding}")
            body = response.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
            if len(body) > MAX_RESPONSE_BYTES:
                raise TransportError(f"response exceeded {MAX_RESPONSE_BYTES} bytes decompressed")
        except urllib3.exceptions.HTTPError as e:
            # A body whose declared encoding does not match its content raises DecodeError
            # here, which a caller catching TransportError would never see.
            self._give_up(response)
            raise TransportError(str(e)) from e
        except BaseException:
            self._give_up(response)
            raise
        # Drain rather than release: an undrained body leaves a readable socket, so the pool
        # discards the connection on next use, measured 10 requests to 10 connections. Safe
        # only here, where the body has been read in full and nothing remains.
        response.drain_conn()
        return body.decode("utf-8", "replace")

    @staticmethod
    def _give_up(response) -> None:
        """Abandon a body we will not use, keeping the connection only when that is cheap.

        `length_remaining` is None on a chunked response, i.e. exactly when the remainder is
        unbounded, so an unknown length has to count as too large.
        """
        remaining = response.length_remaining
        if remaining is not None and remaining <= MAX_DRAIN_BYTES:
            response.drain_conn()
        else:
            response.close()


# The name this transport shipped under before it stopped being backed by `requests`. Kept
# importable for one minor; the behaviour it names is unchanged from a caller's side.
RequestsTransport = Urllib3Transport


class HttpxAsyncTransport(_HeaderMixin):
    """The default for `GoogleDecoderAsync`. Owns its client unless you pass one in.

    An httpx client is safe for concurrent use within one event loop, so one transport can
    serve many in-flight decodes.
    """

    def __init__(self, client=None, proxy: str | None = None, headers: dict | None = None):
        # This is the only place httpx is genuinely required, so it is the only place that can
        # say so usefully. Guarding the import in `__init__.py` instead did nothing: the module
        # imports fine without httpx, so the sentinel it set was never reached and callers got
        # a bare "No module named 'httpx'" from three frames down.
        try:
            import httpx
        except ImportError as e:
            raise ImportError(
                "async decoding requires httpx: pip install googlenewsdecoder[async] "
                "-- or pass your own async transport, which needs no extra at all"
            ) from e

        self.headers = headers
        self._proxy = proxy
        # follow_redirects=False: this transport resolves the chain itself, because httpx
        # offers no way to disable cookies that survives one. Supplying a no-op CookieJar does
        # not work -- `Cookies.__init__` copies its contents into a fresh plain jar and drops
        # the object -- so the jar is cleared per hop in `_follow` instead.
        self._client = client or httpx.AsyncClient(proxy=proxy, follow_redirects=False)
        self._owns_client = client is None

    async def __call__(self, request: Request, *, timeout: float | None = None, proxy: str | None = None) -> str:
        import httpx

        # httpx binds a proxy to the client at construction: httpcore's pool reads `self._proxy`
        # and never looks at request extensions, so there is no per-request override to forward
        # a late `proxy` to. An earlier version passed `extensions={"proxy": ...}`, which httpx
        # accepts and ignores -- egress silently went direct while the code looked correct.
        # Refuse instead, since a proxy that quietly does nothing is the worst of the options.
        if proxy and proxy != self._proxy:
            raise TransportError(
                "HttpxAsyncTransport cannot change proxy per request; httpx binds it to the "
                f"client. Construct HttpxAsyncTransport(proxy={proxy!r}) instead."
            )

        try:
            response = await self._follow(request, timeout)
            response.raise_for_status()
            return response.text
        except httpx.HTTPStatusError as e:
            raise TransportError(str(e), status=e.response.status_code) from e
        except httpx.RequestError as e:
            raise TransportError(str(e)) from e

    async def _follow(self, request: Request, timeout: float | None):
        """Resolve the redirect chain carrying neither cookies nor cross-origin credentials.

        The sync side gets both from urllib3. httpx has no equivalent knob that survives the
        chain, so the two rules are applied here explicitly.
        """
        from urllib.parse import urljoin, urlsplit

        method, url, body = request.method, request.url, request.body
        headers = self._headers(request)
        origin = urlsplit(url)[:2]
        for _ in range(MAX_REDIRECTS):
            self._client.cookies.clear()
            response = await self._client.request(
                method,
                url,
                headers=headers,
                content=body,
                # Per request, not just on the client we build: a caller-supplied client with
                # follow_redirects=True would resolve the chain internally and replay the cookie.
                follow_redirects=False,
                # timeout was accepted and silently dropped, so a caller supplying their own
                # client got no timeout at all. Passing it through fixes that.
                **({"timeout": timeout} if timeout is not None else {}),
            )
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("Location")
            if not location:
                return response
            url = urljoin(url, location)
            if urlsplit(url)[:2] != origin:
                # What urllib3 and browsers do: a credential scoped to one origin must not
                # follow a redirect to another. The first version of this loop leaked it.
                headers = {k: v for k, v in headers.items() if k.lower() not in _PER_ORIGIN}
                origin = urlsplit(url)[:2]
            if response.status_code == 303 or (response.status_code in (301, 302) and method == "POST"):
                method, body = "GET", None
                headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        raise TransportError(f"exceeded {MAX_REDIRECTS} redirects")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()




_default_transport: "Urllib3Transport | None" = None
_default_lock = threading.Lock()


def default_transport() -> "Urllib3Transport":
    """The process-wide default, shared so connections are reused across `decode()` calls.

    Constructing a transport per call gives a PoolManager per call and therefore a connection
    per call -- measured 20 connections for 20 decodes, against 1 when shared. The throttle
    counts connections, so that is the difference between running and being refused.
    """
    global _default_transport
    with _default_lock:
        if _default_transport is None:
            _default_transport = Urllib3Transport()
        return _default_transport
