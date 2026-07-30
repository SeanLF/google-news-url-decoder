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

The two are not identical on cookies, and the difference favours the async one: it drops the
`Cookie` header from every request it sends, including one you set yourself, where urllib3
forwards an explicit header and strips it only across origins. A decode has no use for a cookie,
and sending one is what earns the interstitial.

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

import collections
import contextlib
import threading
import zlib
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

# Default number of proxies one transport keeps pools for, evicting least-recently-used.
# Deliberately well above any rotation likely to be in play, because the cliff is sharp: a
# rotation one larger than the cap cycles through it and rebuilds on every single request, which
# on this endpoint is worse than the file descriptors the cap exists to bound. Callers with a
# bigger list should say so -- `Urllib3Transport(max_pools=...)`.
MAX_POOLS = 128

# What urllib3 will actually decode. Anything else must be refused rather than passed through.
_DECODERS = frozenset({"gzip", "deflate", "br", "zstd", "identity"})

# Headers scoped to the origin that issued them; dropped on a cross-origin redirect.
_PER_ORIGIN = frozenset({"authorization", "cookie", "proxy-authorization"})

# What the async chain follows. Matches urllib3's REDIRECT_STATUSES, so the two transports
# resolve the same chain.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class _IdentityStream:
    """No encoding: the ceiling is a slice, and there is no such thing as a truncated one."""

    eof = True

    def __call__(self, raw, budget):
        return raw[:budget]


class _ZlibStream:
    """Incremental zlib decode with an output ceiling, plus the three things zlib will not do.

    Multi-member gzip is legal, and a single decompressobj stops at the end of the first member,
    parks the remainder in `unused_data` and returns b"" from then on -- so a concatenated
    stream decodes to its first member and nothing says so. A fresh object per member is what
    urllib3's GzipDecoder does, and what this does.

    Raw deflate arrives without the zlib wrapper from some servers. urllib3 retries with a
    negative window size, so a body the sync transport reads must not fail here.

    `eof` is how the caller can tell a complete stream from one that stopped mid-member. zlib
    reports a truncated gzip by simply producing less, so without checking it a cut stream is a
    short article, and an empty one is an empty article.
    """

    def __init__(self, wbits):
        self._wbits = wbits
        self._obj = zlib.decompressobj(wbits)
        self._started = False

    @property
    def eof(self):
        return self._obj.eof

    def __call__(self, raw, budget):
        first = self._member(raw, budget)
        # `unused_data` is non-empty only past the end of a member, so one member covering the
        # chunk is the ordinary case -- returned as-is, because copying it through a buffer
        # doubles the peak of a body already sized to the cap.
        if len(first) >= budget or not self._obj.unused_data:
            return first
        out = bytearray(first)
        while self._obj.unused_data and len(out) < budget:
            out += self._member(self._obj.unused_data, budget - len(out), fresh=True)
        return bytes(out)

    def _member(self, data, budget, fresh=False):
        if fresh:
            self._obj = zlib.decompressobj(self._wbits)
        try:
            return self._obj.decompress(data, max(budget, 0))
        except zlib.error:
            if self._started or self._wbits != zlib.MAX_WBITS:
                raise
            self._wbits = -zlib.MAX_WBITS  # raw deflate, no zlib header
            self._obj = zlib.decompressobj(self._wbits)
            return self._obj.decompress(data, max(budget, 0))
        finally:
            self._started = True


def _bounded_decoder(encoding: str):
    """Return a callable `(raw, budget) -> bytes` yielding at most `budget` bytes per call.

    urllib3 gives the sync transport this for free. The async one has to build it, and can only
    offer it for what zlib covers: brotli and zstd expose no output ceiling in their Python
    bindings, so a bomb in either could only be measured by decompressing it. This transport
    advertises `gzip, deflate`, so anything else is a server ignoring what it was asked for --
    and `x-gzip` is refused rather than decoded, because the sync side refuses it too.
    """
    if encoding in ("", "identity"):
        return _IdentityStream()
    if encoding == "gzip":
        return _ZlibStream(16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        return _ZlibStream(zlib.MAX_WBITS)
    raise TransportError(f"unsupported Content-Encoding: {encoding}")


def _close_manager(manager) -> None:
    """Close the sockets a PoolManager is holding, which `clear()` does not do.

    `PoolManager.clear()` is documented to "direct them all to close" and does not: its
    `RecentlyUsedContainer` is built with no `dispose_func` (urllib3 2.7.0), and clearing only
    disposes when one is set. So `clear()` alone drops references and leaves the sockets to the
    garbage collector -- never, for a pool anything else still holds. The docstring describes
    urllib3 1.26, which did pass a dispose_func.

    Uses `keys()`/`get()` rather than the container's internals, because those are the only
    public way in: `__iter__` raises on purpose, calling itself unlikely to be threadsafe.
    """
    for key in manager.pools.keys():  # noqa: SIM118 -- iterating it raises NotImplementedError
        inner = manager.pools.get(key)
        if inner is not None:
            with contextlib.suppress(Exception):
                inner.close()
    manager.clear()


class Urllib3Transport(_HeaderMixin):
    """The default. Redirect resolution, connection pooling and decompression come from
    urllib3, which is what `requests` uses underneath. This package depends on it directly.

    urllib3 implements no cookie support at all -- its pools are connection pools, not
    stateful clients -- which is the property this decode needs: Google's consent endpoint
    walls any client that replays the `SOCS` cookie it sets on the article's 302. It also
    strips `Authorization`, `Cookie` and `Proxy-Authorization` on a cross-origin redirect,
    drops `Content-Type` when a 303 turns a POST into a GET, and resolves relative
    `Location` headers. Hand-rolling that loop got the cookie right and the credential
    stripping wrong.

    One PoolManager per proxy, kept on the instance, up to `max_pools` of them and then
    least-recently-used first. That is not only a latency saving: the throttle on this endpoint
    counts CONNECTIONS, so an unpooled client is refused on every address tested after 65-110 of
    them while a pooled one runs on a single connection. See `probes/connections.py`.

    Which is also why the cap is generous and adjustable. Rotating through one more proxy than
    it holds evicts each pool just before you return to it, so every request opens a connection
    -- worse, on this endpoint, than the descriptors the cap is there to bound. Rotating through
    more than 128, pass `max_pools`. `close()` hands the sockets back.
    """

    def __init__(self, headers: dict | None = None, max_pools: int = MAX_POOLS):
        self.headers = headers
        self._max_pools = max_pools
        self._pools: collections.OrderedDict[str | None, object] = collections.OrderedDict()
        self._pools_lock = threading.Lock()

    def close(self) -> None:
        """Close every pooled connection. The transport stays usable; pools rebuild on demand.

        Without this there was no way to hand back the sockets a long-lived decoder had
        accumulated short of dropping the object and hoping a garbage collector got to it.
        `HttpxAsyncTransport` has had `aclose()` all along.

        Not for `default_transport()`: that instance is shared process-wide, and closing it
        drops pools every decoder in the process is relying on.
        """
        with self._pools_lock:
            pools, self._pools = self._pools, collections.OrderedDict()
        for pool in pools.values():
            _close_manager(pool)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _pool(self, proxy: str | None):
        with self._pools_lock:
            pool = self._pools.get(proxy)
            if pool is not None:
                self._pools.move_to_end(proxy)
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
            while len(self._pools) > self._max_pools:
                # Least recently used first. A PoolManager per proxy kept forever is a
                # descriptor leak the size of the caller's rotation. A connection another thread
                # has checked out is unaffected and closes when its response does, which is
                # urllib3's own documented behaviour for this.
                _, evicted = self._pools.popitem(last=False)
                _close_manager(evicted)
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

    Reads and decompresses the body itself, because httpx offers no bounded decode and the
    sync side's `MAX_RESPONSE_BYTES` has to hold here too. That limits it to what zlib covers:
    `gzip` and `deflate`, which are what it advertises, and identity. See `_read_bounded`.
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
        # the object -- so `_follow` drops the Cookie header off each request instead.
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
        except httpx.HTTPStatusError as e:
            # Only reachable through a caller's client: httpx's documented raise_for_status
            # response hook. It is not a RequestError, so without this it left the transport
            # untyped, escaped `drive_async`, and abandoned a whole batch.
            raise TransportError(str(e), status=e.response.status_code) from e
        except httpx.RequestError as e:
            raise TransportError(str(e)) from e

        try:
            # >= 300, not >= 400: a 3xx this loop declined to follow -- no Location -- would
            # otherwise be handed back as the article body. The sync side already refused that;
            # `raise_for_status()` here did not, because it only fires from 400.
            if response.status_code >= 300:
                raise TransportError(f"HTTP {response.status_code}", status=response.status_code)
            body = await self._read_bounded(response)
        except httpx.RequestError as e:
            await self._give_up(response)
            raise TransportError(str(e)) from e
        except BaseException:
            # Not `finally`: refusing a body means hanging up mid-read, which is exactly when a
            # peer reset makes the close fail, and a bare finally would let that replace the
            # diagnosis. `_give_up` swallows its own errors for the same reason.
            await self._give_up(response)
            raise
        await self._give_up(response)
        return body.decode("utf-8", "replace")

    @staticmethod
    async def _give_up(response) -> None:
        """Finish with a response, keeping the pooled connection when that is cheap.

        Closing a streamed response whose body was never read makes httpcore discard the
        connection: measured one per hop, 5 connections for a 4-hop chain, where the sync
        transport used 1. The throttle counts connections, so that is budget.

        Draining reads RAW bytes and never decodes, so a compressed hop cannot expand past its
        declared length -- `aread()` here would decode, turning a 1 MiB gzipped 302 into a
        gigabyte. Never raises: it runs with another exception possibly in flight.
        """
        try:
            declared = int(response.headers.get("Content-Length", "-1"))
        except ValueError:
            declared = -1
        with contextlib.suppress(Exception):
            if not response.is_stream_consumed and 0 <= declared <= MAX_DRAIN_BYTES:
                async for _ in response.aiter_raw():
                    pass
        with contextlib.suppress(Exception):
            await response.aclose()

    async def _read_bounded(self, response) -> bytes:
        """Accumulate the body, refusing it the moment it decodes past `MAX_RESPONSE_BYTES`.

        Reads `aiter_raw` and decompresses here rather than taking httpx's decoded stream,
        because neither of httpx's two options has a ceiling: `.text` reads to the end and asks
        afterwards, and `aiter_bytes` decodes a whole network chunk per step, so a 64 KiB read
        of a gzip bomb is ~64 MB in one allocation. Counting chunks measured 157 MiB peak
        against a 32 MiB cap. zlib takes the ceiling as an argument; httpx does not expose it.
        """
        if response.is_stream_consumed:
            # The body is already in memory and already decoded, so the cap can only be applied
            # after the fact. Two ways to get here: a Response built with its content in hand
            # (httpx.MockTransport, a caller's stub), or a caller's client whose response hook
            # read the body -- httpx's own idiom for a hook that needs content. The second is a
            # real socket, so this is a hole in the bound rather than a case where none is
            # needed: measured 256 MiB peak against a 32 MiB cap through such a hook.
            body = response.content
            if len(body) > MAX_RESPONSE_BYTES:
                raise TransportError(f"response exceeded {MAX_RESPONSE_BYTES} bytes decompressed")
            return body

        encoding = (response.headers.get("content-encoding") or "").lower()
        decode = _bounded_decoder(encoding)
        chunks, total = [], 0
        try:
            async for raw in response.aiter_raw():
                # cap + 1 rather than cap, so "exactly the cap" stays acceptable and one byte
                # more is detectable -- the same trick as the sync read().
                piece = decode(raw, MAX_RESPONSE_BYTES + 1 - total)
                total += len(piece)
                if total > MAX_RESPONSE_BYTES:
                    raise TransportError(f"response exceeded {MAX_RESPONSE_BYTES} bytes decompressed")
                chunks.append(piece)
        except zlib.error as e:
            # The sync side gets this normalised by urllib3; here it would otherwise reach a
            # caller who was told to catch TransportError.
            raise TransportError(f"could not decode {encoding} body: {e}") from e
        if not decode.eof:
            # zlib reports a stream cut mid-member by producing less, so silence here is how a
            # truncated article becomes a short one and an empty body becomes an empty article.
            raise TransportError(f"truncated {encoding} body")
        return b"".join(chunks)

    async def _follow(self, request: Request, timeout: float | None):
        """Resolve the redirect chain carrying neither cookies nor cross-origin credentials.

        The sync side gets both from urllib3. httpx has no equivalent knob that survives the
        chain, so the two rules are applied here explicitly.
        """
        from urllib.parse import urljoin, urlsplit

        import httpx

        method, url, body = request.method, request.url, request.body
        headers = self._headers(request)
        origin = urlsplit(url)[:2]
        # MAX_REDIRECTS redirects, so one more request than that. `range(MAX_REDIRECTS)` spent
        # the budget on requests instead, which is a different number from the one the sync side
        # enforces -- urllib3's `redirect=MAX_REDIRECTS` counts hops -- so the same chain could
        # resolve through one transport and be refused by the other.
        for _ in range(MAX_REDIRECTS + 1):
            outgoing = self._client.build_request(
                method,
                url,
                headers=headers,
                content=body,
                # timeout was accepted and silently dropped, so a caller supplying their own
                # client got no timeout at all. Passing it through fixes that.
                **({"timeout": timeout} if timeout is not None else {}),
            )
            # build_request is where httpx bakes the client's jar into a Cookie header, so
            # dropping it here is what keeps Google's consent wall shut. The previous version
            # cleared `client.cookies` instead, which works but empties a jar the caller may own
            # and may have populated for their own reasons.
            outgoing.headers.pop("Cookie", None)
            try:
                # stream=True so nothing is read until `_read_bounded` can refuse it. The caller
                # closes the response it gets back; every hop this loop discards, it closes here.
                response = await self._client.send(
                    outgoing,
                    stream=True,
                    # Per request, not just on the client we build: a caller-supplied client with
                    # follow_redirects=True would resolve the chain internally and replay the cookie.
                    follow_redirects=False,
                )
            except (httpx.InvalidURL, ValueError) as e:
                # httpx resolves the previous hop's Location inside send(), even with
                # follow_redirects=False, so this is where a Location its URL layer rejects
                # surfaces -- `data:text/html,x` as InvalidURL, an `xn--` host as an idna error,
                # which is a UnicodeError and so a ValueError. Guarding after send() returns, as
                # an earlier version did, never saw either. Neither is a RequestError, so both
                # left the transport untyped and abandoned the batch.
                raise TransportError(f"unusable URL in redirect chain: {e}") from e
            if response.status_code not in _REDIRECT_STATUSES:
                return response
            location = response.headers.get("Location")
            if not location:
                return response
            await self._give_up(response)
            try:
                url = urljoin(url, location)
                parsed = urlsplit(url)
                parsed.port  # noqa: B018  -- raises outside 0-65535; port 0 parses and fails later
                target = parsed[:2]
            except ValueError as e:
                # A bare ValueError from here is outside the TransportError contract that
                # `drive_async` and every documented wrapper is told to catch, so it escaped and
                # abandoned the batch. `urljoin` raises on "http://[/" and friends; the port is
                # asked for separately because parsing accepts 99999 and httpx then fails on
                # connect with an ExceptionGroup, which is outside the contract too.
                raise TransportError(f"malformed Location header: {location!r}") from e
            if target != origin:
                # What urllib3 and browsers do: a credential scoped to one origin must not
                # follow a redirect to another. The first version of this loop leaked it.
                headers = {k: v for k, v in headers.items() if k.lower() not in _PER_ORIGIN}
                origin = target
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
