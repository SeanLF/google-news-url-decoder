"""One test per defect found by adversarial review.

Each of these was fixed and then verified by hand, which is not the same as being guarded.
A fix without a test is a fix that comes back.
"""

import asyncio
import contextlib
import functools
import gzip
import io
import json
import os
import threading
import time
import tracemalloc
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from googlenewsdecoder import decode, decode_batch, protocol
from googlenewsdecoder.limits import MAX_RESPONSE_BYTES
from googlenewsdecoder.protocol import Request
from googlenewsdecoder.transports import TransportError

GOOGLE_URL = "https://news.google.com/rss/articles/TOKEN?oc=5"

# How much larger than the cap the bomb is. A ratio rather than "cap + 1 KB" is what lets peak
# memory separate a bounded read from an unbounded one -- at cap+1KB the two are the same
# number. 4x is the smallest that keeps the calibration test's >4x margin comfortable, and it
# costs ~620 MB RSS against ~1.3 GB at 10x, which matters on a shared runner.
BOMB_RATIO = 4

# Bytes of slack allowed past the cap in the hang-up test. This is a kernel socket-buffer
# allowance, not a fraction of the cap: measured 3-4 MiB on loopback regardless of cap size,
# so tying it to MAX_RESPONSE_BYTES would go flaky the day the cap shrinks.
SOCKET_BUFFER_SLACK = 16 << 20


@functools.cache
def gzip_bomb(size):
    """A gzip stream expanding to `size` bytes, built without ever holding `size` in memory.

    `b"\\0" * (4 * MAX_RESPONSE_BYTES)` is a 128 MB allocation, which would dominate the very
    measurement it exists to feed. Cached because two tests want the same bomb.
    """
    buf = io.BytesIO()
    chunk = b"\0" * (1 << 20)
    with gzip.GzipFile(fileobj=buf, mode="wb") as f:
        for _ in range(size // len(chunk)):
            f.write(chunk)
    return buf.getvalue()


def fixed_body(payload, encoding=None, status=200):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    return Handler


def streamed_body(total, written):
    """A body written a MiB at a time, recording how much actually went out.

    A client that hangs up leaves `written["n"]` well short of `total`; one that reads to the
    end does not. Chunked writes are what make the difference observable -- a single write()
    of the whole body would land in the socket buffer either way.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(total))
            self.end_headers()
            chunk = b"\0" * (1 << 20)
            for _ in range(total // len(chunk)):
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except OSError:
                    return
                written["n"] += len(chunk)

        def log_message(self, *args):
            pass

    return Handler


@contextlib.contextmanager
def serving(handler, *, keep_alive=False):
    """Run `handler` on loopback, which is the one thing conftest's socket block allows.

    Single-threaded by default, which runs the handler inline and so lets `shutdown()` double
    as a join -- the byte counts are settled by the time the context exits. `keep_alive` needs
    HTTP/1.1 and therefore a threading server, because a client holding the socket open would
    otherwise block `shutdown()` inside `handle_one_request` forever.
    """
    handler.protocol_version = "HTTP/1.1" if keep_alive else "HTTP/1.0"

    class Server(ThreadingHTTPServer if keep_alive else HTTPServer):
        daemon_threads = True
        block_on_close = False
        accepted = 0

        def get_request(self):
            self.accepted += 1
            return super().get_request()

        def handle_error(self, request, client_address):
            # A client that refuses a body hangs up mid-write, so a broken pipe here is the
            # test passing, not a traceback worth printing.
            pass

    server = Server(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        # shutdown() stops the accept loop; without this the listening socket survives the
        # test, one leaked fd per server, which `-W error::ResourceWarning` reports.
        server.server_close()


def peak_bytes(fn):
    """Run `fn` and return the process's peak Python allocation while it ran."""
    # PYTHONTRACEMALLOC=N is how you would investigate a failure here, and stopping a session
    # we did not start would switch it off for the rest of the run.
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        if not was_tracing:
            tracemalloc.stop()


def open_fds():
    """How many file descriptors this process holds. The direct measure of a released socket."""
    return len(os.listdir("/dev/fd"))


def inner_pools(manager):
    """The HTTPConnectionPools inside a PoolManager -- the objects that hold the sockets.

    A test measuring descriptors has to hold these, not the manager: `PoolManager.clear()` drops
    its references to them, and refcounting then closes their sockets for free. Holding the
    manager alone therefore cannot tell a real close from a forgotten one, which is how a `clear()`
    that closes nothing went unnoticed. Something always holds these in practice -- an in-flight
    request, or a response whose body was never drained.
    """
    # noqa on the next line: iterating a RecentlyUsedContainer raises NotImplementedError.
    return [manager.pools.get(key) for key in manager.pools.keys()]  # noqa: SIM118


def wait_until(predicate, timeout=2.0):
    """Poll `predicate` briefly. Closing the client end takes a moment to reach the server's."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def gzipped(data):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as f:
        f.write(data)
    return buf.getvalue()


def raw_deflate(data):
    """deflate with no zlib wrapper, which is what some servers mean by `Content-Encoding`."""
    obj = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return obj.compress(data) + obj.flush()


def redirect_chain(hops, body=b"FINAL"):
    """/0 -> /1 -> ... -> /<hops>, all on one host, so one connection can serve every hop."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            n = int(self.path.lstrip("/"))
            if n < hops:
                self.send_response(302)
                self.send_header("Location", f"/{n + 1}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def fetch(kind, url, timeout=10):
    """One request through whichever shipped transport `kind` names, returning its text."""
    from googlenewsdecoder.transports import HttpxAsyncTransport, Urllib3Transport

    if kind == "sync":
        return Urllib3Transport()(Request("GET", url, {}), timeout=timeout)

    async def go():
        transport = HttpxAsyncTransport()
        try:
            return await transport(Request("GET", url, {}), timeout=timeout)
        finally:
            await transport.aclose()

    return asyncio.run(go())


# Bodies both transports must read identically. The async one decodes for itself, so each of
# these is something urllib3 was doing for the sync side that had to be reimplemented.
DECODABLE = [
    (gzipped(b"HELLO"), "gzip", "HELLO"),
    (gzipped(b"AAAA") + gzipped(b"BBBB"), "gzip", "AAAABBBB"),
    (zlib.compress(b"HELLO"), "deflate", "HELLO"),
    (raw_deflate(b"HELLO"), "deflate", "HELLO"),
    (b"HELLO", None, "HELLO"),
]
DECODABLE_IDS = ["gzip", "multi-member-gzip", "zlib-deflate", "raw-deflate", "identity"]

# A gzip stream cut mid-member. The HTTP body is complete and Content-Length honest, so only
# the decoder can notice.
CUT_GZIP = gzipped(b"X" * 200_000)[:-50]


def frames(pairs):
    body = [["wrb.fr", "Fbv4je", json.dumps(["garturlres", u]), None, None, None, str(t)] for t, u in pairs]
    return ")]}'\n\n" + json.dumps(body + [["di", 1]])


class TestTransportCannotDictateTheResult:
    """`drive` caught StopIteration around the whole loop, so a transport raising it was
    indistinguishable from the flow finishing. decode() returned None instead of a dict,
    silently, and a transport could name the return value outright."""

    def test_a_transport_whose_iterator_runs_dry_does_not_return_none(self):
        class Dry:
            def __init__(self):
                self.bodies = iter([])

            def __call__(self, request, *, timeout=None, proxy=None):
                return next(self.bodies)

        result = decode(GOOGLE_URL, transport=Dry())
        assert isinstance(result, dict), "must not leak None where a dict is documented"
        assert result["status"] is False

    def test_a_transport_cannot_forge_a_successful_result(self):
        class Forge:
            def __call__(self, request, *, timeout=None, proxy=None):
                raise StopIteration({"status": True, "decoded_url": "https://attacker.example/forged"})

        result = decode(GOOGLE_URL, transport=Forge())
        assert result.get("decoded_url") != "https://attacker.example/forged"
        assert result["status"] is False


class TestBatchTagsCannotBeConfused:
    """Results are keyed by the tag we sent. Anything that lets one position be overwritten,
    or a non-canonical tag be read as a position, reintroduces the wrong-URL-for-the-right-
    article failure the whole batch design exists to prevent."""

    def test_a_duplicate_tag_suppresses_the_position_rather_than_overwriting_it(self):
        got = protocol.parse_batch_decoded(
            frames([(1, "https://pub/one"), (2, "https://pub/two"), (1, "https://attacker/pwn")])
        )
        assert 1 not in got, "a contradicted position must be reported missing, not guessed"
        assert got[2] == "https://pub/two"

    def test_a_repeated_tag_agreeing_with_itself_is_kept(self):
        got = protocol.parse_batch_decoded(frames([(1, "https://pub/one"), (1, "https://pub/one")]))
        assert got == {1: "https://pub/one"}

    @pytest.mark.parametrize(
        "tag",
        [" 1 ", "+1", "1_0", "١", "1.9"],
        ids=["spaces", "plus", "underscore", "arabic-indic", "float"],
    )
    def test_non_canonical_tags_are_rejected(self, tag):
        body = ")]}'\n\n" + json.dumps(
            [["wrb.fr", "Fbv4je", json.dumps(["garturlres", "https://pub/x"]), None, None, None, tag]]
        )
        assert protocol.parse_batch_decoded(body) == {}


class TestBatchContainsItsFailures:
    """decode() converts a stray exception into a status dict; decode_batch let one escape and
    discard every already-decoded result with it."""

    def test_an_unexpected_exception_becomes_per_url_failures(self):
        class Boom:
            def __call__(self, request, *, timeout=None, proxy=None):
                raise ValueError("boom")

        results = decode_batch([GOOGLE_URL] * 3, transport=Boom())
        assert len(results) == 3
        assert all(r["status"] is False for r in results)

    @pytest.mark.parametrize("bad", [0, -1, "x", None], ids=["zero", "negative", "string", "none"])
    def test_an_unusable_chunk_size_is_rejected_not_silently_wrong(self, bad):
        with pytest.raises(ValueError):
            decode_batch([GOOGLE_URL], chunk_size=bad)


class TestADecodeDoesNotSpendARequestOnALocaleRedirect:
    """Google answers a bare `/articles/<token>` with a 302 to the same path plus its own
    hl/gl/ceid, so a decode cost three round trips where two would do -- measured on clean exits,
    and the refusal on this endpoint is counted in requests, so the third one is budget.

    Sending the locale ourselves is a strict improvement or neutral: if Google ever redirects
    anyway, the cost is what it already was.
    """

    def test_the_article_page_request_carries_a_locale(self):
        urls = protocol.params_urls("TOKEN")
        assert urls, "params_urls must still offer candidates"
        for url in urls:
            # Asserted on the parsed query, not the escaping. Whether the colon in `ceid` arrives
            # raw or percent-encoded is wire-equivalent, so pinning one spelling would turn a
            # cosmetic change into a failure reading "the locale change regressed".
            assert parse_qs(urlsplit(url).query) == {"hl": ["en-US"], "gl": ["US"], "ceid": ["US:en"]}, url

    def test_the_smaller_article_page_is_tried_first(self):
        """Both candidates carry the same two attributes, and `/rss/articles` is 118 KiB on the
        wire against 167 for `/articles`, sampled over 8 tokens. Order is the whole saving, and
        nothing else in the suite would notice it flipping back.
        """
        first, second = protocol.params_urls("TOKEN")
        assert "/rss/articles/" in first, first
        assert "/rss/articles/" not in second and "/articles/" in second, second

    def test_a_caller_can_ask_for_the_bare_url(self):
        for url in protocol.params_urls("TOKEN", locale=None):
            assert "?" not in url, url

    def test_a_caller_can_choose_a_different_locale(self):
        urls = protocol.params_urls("TOKEN", locale={"hl": "fr-CA", "gl": "CA", "ceid": "CA:fr"})
        assert all("hl=fr-CA" in u for u in urls), urls

    def test_the_flow_asks_for_the_url_the_locale_produced(self):
        """The parameter has to reach the request, not just the helper. A default that the flow
        does not pass through would look right in isolation and change nothing in practice.
        """
        seen = []

        class Recorder:
            def __call__(self, request, *, timeout=None, proxy=None):
                seen.append(request.url)
                raise TransportError("stop here")

        decode(GOOGLE_URL, transport=Recorder())
        assert seen, "the flow made no request"
        assert all("hl=en-US" in url for url in seen if "/articles/" in url), seen


class TestDecompressionIsBounded:
    """A gzipped body can expand by orders of magnitude; a ~1 MB response was measured
    expanding to 1 GB. Neither urllib3 nor httpx caps this itself, so both transports read
    bounded rather than preloading.

    These assert the BOUND, not the message. An earlier version matched only
    `TransportError("...exceeded...")`, which an implementation that allocates the whole bomb
    and then measures it passes identically -- so it could not distinguish the guarantee the
    README makes from a body that had already been in memory.
    """

    def test_the_sync_transport_refuses_a_bomb_without_allocating_it(self):
        from googlenewsdecoder.transports import Urllib3Transport

        bomb = gzip_bomb(BOMB_RATIO * MAX_RESPONSE_BYTES)
        with serving(fixed_body(bomb, encoding="gzip")) as server:
            url = f"http://127.0.0.1:{server.server_port}/bomb"

            def refuse():
                with pytest.raises(TransportError, match="exceeded"):
                    Urllib3Transport()(Request("GET", url, {}), timeout=30)

            peak = peak_bytes(refuse)
        assert peak < 3 * MAX_RESPONSE_BYTES, (
            f"peak {peak >> 20} MiB against a {MAX_RESPONSE_BYTES >> 20} MiB cap: "
            "read(amt, decode_content=True) is not bounding DECODED output"
        )

    def test_the_async_transport_refuses_a_bomb_without_allocating_it(self):
        """`MAX_RESPONSE_BYTES` was enforced by one of the two shipped transports. This one
        returned the whole 41.9 MB body, since `response.text` reads to the end before anyone
        can object -- and switching to httpx's decoded stream only moved the problem, since it
        decodes a whole network chunk per step (measured 157 MiB peak against a 32 MiB cap).
        """
        pytest.importorskip("httpx", reason="the async transport needs the [async] extra")

        from googlenewsdecoder.transports import HttpxAsyncTransport

        bomb = gzip_bomb(BOMB_RATIO * MAX_RESPONSE_BYTES)
        with serving(fixed_body(bomb, encoding="gzip")) as server:
            url = f"http://127.0.0.1:{server.server_port}/bomb"
            transport = HttpxAsyncTransport()

            async def go():
                try:
                    await transport(Request("GET", url, {}), timeout=30)
                finally:
                    await transport.aclose()

            def refuse():
                with pytest.raises(TransportError, match="exceeded"):
                    asyncio.run(go())

            peak = peak_bytes(refuse)
        assert peak < 3 * MAX_RESPONSE_BYTES, (
            f"peak {peak >> 20} MiB against a {MAX_RESPONSE_BYTES >> 20} MiB cap: "
            "the async path is not bounding decoded output"
        )

    def test_the_measurement_can_see_an_unbounded_read(self):
        """Calibration, not behaviour: `.data` is the unbounded read the transports avoid.

        Without this, the assertions above are consistent with tracemalloc simply not observing
        the decompression buffer at all -- a green suite proving nothing.
        """
        import urllib3

        bomb = gzip_bomb(BOMB_RATIO * MAX_RESPONSE_BYTES)
        got = {}
        with serving(fixed_body(bomb, encoding="gzip")) as server:
            url = f"http://127.0.0.1:{server.server_port}/bomb"

            def read_it_all():
                with urllib3.PoolManager() as pool:
                    got["size"] = len(pool.request("GET", url, headers={"Accept-Encoding": "gzip"}).data)

            peak = peak_bytes(read_it_all)
        assert got["size"] == BOMB_RATIO * MAX_RESPONSE_BYTES
        assert peak > 4 * MAX_RESPONSE_BYTES, (
            f"an unbounded read of {BOMB_RATIO}x the cap peaked at only {peak >> 20} MiB: "
            "either tracemalloc is not seeing the decompression buffer, in which case the "
            "bound tests above are blind, or urllib3 has started bounding .data itself"
        )

    def test_an_ordinary_body_still_decompresses(self):
        from googlenewsdecoder.transports import Urllib3Transport

        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as f:
            f.write(b"<html>fine</html>")
        with serving(fixed_body(buf.getvalue(), encoding="gzip")) as server:
            url = f"http://127.0.0.1:{server.server_port}/ok"
            assert Urllib3Transport()(Request("GET", url, {}), timeout=10) == "<html>fine</html>"

    def test_a_mismatched_encoding_raises_transport_error(self):
        from googlenewsdecoder.transports import Urllib3Transport

        with serving(fixed_body(b"not gzipped at all", encoding="gzip")) as server:
            url = f"http://127.0.0.1:{server.server_port}/lie"
            with pytest.raises(TransportError):
                Urllib3Transport()(Request("GET", url, {}), timeout=10)


class TestTheTwoTransportsDecodeAlike:
    """A body one transport reads and the other refuses is the worst kind of bug report: which
    answer you get depends on whether the caller reached for `decode` or `decode_async`.

    The async transport decodes for itself in order to bound the output, so everything urllib3
    was quietly handling for the sync side became something it had to be given. Multi-member
    gzip decoded to its first member; raw deflate raised.
    """

    @pytest.mark.parametrize("kind", ["sync", "async"])
    @pytest.mark.parametrize(("payload", "encoding", "expected"), DECODABLE, ids=DECODABLE_IDS)
    def test_both_transports_read_the_same_body(self, kind, payload, encoding, expected):
        if kind == "async":
            pytest.importorskip("httpx", reason="the async transport needs the [async] extra")
        with serving(fixed_body(payload, encoding=encoding)) as server:
            assert fetch(kind, f"http://127.0.0.1:{server.server_port}/x") == expected

    def test_the_async_transport_refuses_a_gzip_stream_that_was_cut(self):
        pytest.importorskip("httpx", reason="the async transport needs the [async] extra")
        with serving(fixed_body(CUT_GZIP, encoding="gzip")) as server, pytest.raises(TransportError, match="truncated"):
            fetch("async", f"http://127.0.0.1:{server.server_port}/cut")

    def test_the_async_transport_refuses_an_empty_body_claiming_gzip(self):
        """The same check, at zero length. The sync transport returns "" here, so this is a
        deliberate divergence: an empty gzip stream is malformed, and an empty article is
        indistinguishable from a decode that quietly lost everything.
        """
        pytest.importorskip("httpx", reason="the async transport needs the [async] extra")
        with serving(fixed_body(b"", encoding="gzip")) as server, pytest.raises(TransportError, match="truncated"):
            fetch("async", f"http://127.0.0.1:{server.server_port}/empty")

    @pytest.mark.xfail(
        strict=True,
        reason="known, measured: urllib3 never checks the gzip trailer, so a stream cut "
        "mid-member comes back as a short article -- 158672 of 200000 characters, no error. "
        "Detecting it needs urllib3's private decoder state, which is why it is not fixed here. "
        "Strict, so the day that changes this test says so instead of staying quietly green.",
    )
    def test_the_sync_transport_refuses_a_gzip_stream_that_was_cut(self):
        with serving(fixed_body(CUT_GZIP, encoding="gzip")) as server, pytest.raises(TransportError):
            fetch("sync", f"http://127.0.0.1:{server.server_port}/cut")


class TestOnlyAFailedConnectIsRetried:
    """One retry, and only for the failure urllib3 can promise never arrived.

    Everything that reached the endpoint costs a unit of the address's budget whether or not we
    liked the answer, so retrying it spends a second unit to be told the same thing. A connect
    that never completed sent no bytes. That is the whole line, and urllib3's counters draw it in
    exactly one place: `connect`.
    """

    def test_a_failed_connect_is_retried_once(self):
        """Driven by making the connect fail, because nothing else reaches that branch.

        A pre-response drop looks like the same class of failure and is NOT retried, since urllib3
        files it under `read` alongside read timeouts, and a read timeout on a throttled endpoint
        means the request was received and stalled. There is no counter that separates the two.
        """
        import urllib3.util.connection

        from googlenewsdecoder.transports import Urllib3Transport

        with serving(fixed_body(b"ok"), keep_alive=True) as server:
            url = f"http://127.0.0.1:{server.server_port}/x"
            real = urllib3.util.connection.create_connection
            attempts = {"n": 0}

            def fail_once(*args, **kwargs):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise OSError("simulated connect failure")
                return real(*args, **kwargs)

            transport = Urllib3Transport()
            try:
                urllib3.util.connection.create_connection = fail_once
                assert transport(Request("GET", url, {}), timeout=10) == "ok"
            finally:
                urllib3.util.connection.create_connection = real
                transport.close()
        assert attempts["n"] == 2, f"expected one connect retry, saw {attempts['n']} attempts"

    def test_a_read_timeout_is_not_retried(self):
        """The reverted half of this change, pinned so it cannot come back by accident.

        urllib3's own note on the read branch says to assume the server began processing the
        request. Retrying that spends a second unit of budget on an endpoint whose refusal mode
        includes stalling, which is what `status=0` exists to prevent one flavour of.
        """
        from googlenewsdecoder.transports import Urllib3Transport

        delivered = {"n": 0}

        class Stall(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                delivered["n"] += 1
                time.sleep(3)

            def log_message(self, *args):
                pass

        with serving(Stall, keep_alive=True) as server:
            url = f"http://127.0.0.1:{server.server_port}/slow"
            transport = Urllib3Transport()
            try:
                with pytest.raises(TransportError):
                    transport(Request("GET", url, {}), timeout=1)
            finally:
                transport.close()
        assert delivered["n"] == 1, (
            f"a read timeout was retried: the server received the request {delivered['n']} times, "
            "which is a second unit of the address's budget spent for nothing"
        )

    def test_a_429_is_not_retried(self):
        """Counted at the handler, not at the connection.

        This asserted on accepted TCP connections, which cannot detect a status retry at all:
        urllib3 drains the response and REUSES the keep-alive connection, so a version of this
        transport retrying 429s three times passed the old assertion unchanged.
        """
        from googlenewsdecoder.transports import Urllib3Transport

        delivered = {"n": 0}

        class Refuse(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                delivered["n"] += 1
                body = b"slow down"
                self.send_response(429)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        with serving(Refuse, keep_alive=True) as server:
            url = f"http://127.0.0.1:{server.server_port}/429"
            transport = Urllib3Transport()
            try:
                with pytest.raises(TransportError, match="HTTP 429"):
                    transport(Request("GET", url, {}), timeout=10)
            finally:
                transport.close()
        assert delivered["n"] == 1, f"a 429 was retried: the server received {delivered['n']} requests"


class TestHoldingADecoderKeepsOneConnection:
    """Connections are the resource Google's throttle is most sensitive to, so the difference
    between holding a decoder and building one per call is the difference that matters.

    `decode_async()` builds and closes a client per call, measured 5 connections for 5 decodes,
    which is why its docstring says to hold a decoder for more than one URL. The sync side has no
    such trap because `default_transport()` is shared process-wide. Both good paths are pinned
    here; the bad one is documented rather than fixed, because an httpx client belongs to its
    event loop and cannot be shared process-wide the way the sync pool can.
    """

    def _point_at(self, port, monkeypatch):
        monkeypatch.setattr(
            protocol,
            "params_urls",
            lambda token, locale=None: (f"http://127.0.0.1:{port}/a", f"http://127.0.0.1:{port}/b"),
        )

    def test_five_sync_decodes_share_one_connection(self, monkeypatch):
        with serving(fixed_body(b"<html>no params</html>"), keep_alive=True) as server:
            self._point_at(server.server_port, monkeypatch)
            for _ in range(5):
                decode(GOOGLE_URL)
            accepted = server.accepted
        assert accepted == 1, f"5 sync decodes opened {accepted} connections; the default is shared"

    def test_five_async_decodes_through_one_decoder_share_one_connection(self, monkeypatch):
        pytest.importorskip("httpx", reason="the async transport needs the [async] extra")

        from googlenewsdecoder.decoder_async import GoogleDecoderAsync

        with serving(fixed_body(b"<html>no params</html>"), keep_alive=True) as server:
            self._point_at(server.server_port, monkeypatch)

            async def go():
                async with GoogleDecoderAsync() as decoder:
                    for _ in range(5):
                        await decoder.decode_google_news_url(GOOGLE_URL)

            asyncio.run(go())
            accepted = server.accepted
        assert accepted == 1, f"5 decodes through one decoder opened {accepted} connections"


class TestTheSyncTransportsPoolsHaveALifetime:
    """A PoolManager per proxy, kept forever, on a library whose whole reason to pool is a
    throttle counted per address. Rotating proxies is the normal way to work around that, so the
    dict grew with the rotation and there was no way to give the sockets back.
    """

    def test_rotating_through_more_proxies_than_the_cap_does_not_grow_forever(self):
        from googlenewsdecoder.transports import Urllib3Transport

        transport = Urllib3Transport(max_pools=4)
        # No requests needed: this is the dict's bound, and none of these has to resolve.
        for i in range(20):
            transport._pool(f"http://proxy{i}.invalid:8080")
        assert len(transport._pools) == 4

    def test_the_proxy_still_in_use_is_not_the_one_evicted(self):
        """LRU, not insertion order: a rotation that keeps coming back to one proxy has to keep
        that pool, since reuse is the entire reason these are cached.
        """
        from googlenewsdecoder.transports import Urllib3Transport

        transport = Urllib3Transport(max_pools=4)
        hot = "http://hot.invalid:8080"
        first = transport._pool(hot)
        for i in range(8):
            transport._pool(f"http://proxy{i}.invalid:8080")
            transport._pool(hot)  # touched throughout, so insertion order would have dropped it
        assert transport._pool(hot) is first, "the pool in active use was evicted"

    def test_close_hands_back_the_socket_rather_than_just_the_reference(self):
        """Measured on descriptors, and with the connection pools still referenced.

        Both halves matter. An accept count proves nothing, because after `close()` the dict is
        empty and the next request must open a connection whether or not the old socket closed.
        And descriptors prove nothing either while nothing holds the pools, because dropping the
        last reference lets refcounting close the sockets for us -- which is precisely what hid
        the fact that `PoolManager.clear()` closes nothing itself.
        """
        from googlenewsdecoder.transports import Urllib3Transport

        with serving(fixed_body(b"ok"), keep_alive=True) as server:
            url = f"http://127.0.0.1:{server.server_port}/x"
            transport = Urllib3Transport()
            assert transport(Request("GET", url, {}), timeout=10) == "ok"
            assert transport(Request("GET", url, {}), timeout=10) == "ok"
            assert server.accepted == 1, "the second request should have reused the connection"

            held = inner_pools(transport._pool(None))
            before = open_fds()
            transport.close()
            closed = wait_until(lambda: open_fds() < before)
            assert held  # keep the connection pools alive across the measurement
            assert closed, (
                f"{before} descriptors before close() and still {open_fds()} after, with the "
                "connection pools still referenced: the socket was forgotten, not closed"
            )
            assert transport(Request("GET", url, {}), timeout=10) == "ok", "still usable"

    def test_eviction_hands_back_the_socket_too(self):
        from googlenewsdecoder.transports import Urllib3Transport

        with serving(fixed_body(b"ok"), keep_alive=True) as server:
            url = f"http://127.0.0.1:{server.server_port}/x"
            transport = Urllib3Transport(max_pools=2)
            assert transport(Request("GET", url, {}), timeout=10) == "ok"

            held = inner_pools(transport._pool(None))
            before = open_fds()
            # Two more proxies push the direct pool, now least recently used, off the end.
            transport._pool("http://a.invalid:8080")
            transport._pool("http://b.invalid:8080")
            closed = wait_until(lambda: open_fds() < before)
            assert held  # as above: an evicted pool someone still holds
            assert closed, (
                f"{before} descriptors before eviction and still {open_fds()} after: an evicted "
                "pool is being forgotten rather than closed"
            )

    def test_it_can_be_used_as_a_context_manager(self):
        from googlenewsdecoder.transports import Urllib3Transport

        with serving(fixed_body(b"ok"), keep_alive=True) as server:
            url = f"http://127.0.0.1:{server.server_port}/x"
            with Urllib3Transport() as transport:
                assert transport(Request("GET", url, {}), timeout=10) == "ok"
            assert transport._pools == {}


class TestACallerSuppliedClientStaysInsideTheContract:
    """`HttpxAsyncTransport(client=...)` accepts a client the caller configured, and both of
    these were ways that configuration escaped the TransportError contract that `drive_async`
    and every documented wrapper are told to catch.
    """

    def test_a_hook_that_raises_for_status_still_arrives_as_a_transport_error(self):
        """httpx's documented raise_for_status hook throws HTTPStatusError, which is not a
        RequestError. It escaped the transport untyped, so it escaped drive_async, so one 503
        abandoned an entire batch instead of failing one position."""
        httpx = pytest.importorskip("httpx", reason="the async transport needs the [async] extra")

        from googlenewsdecoder.transports import HttpxAsyncTransport

        async def raise_on_error(response):
            response.raise_for_status()

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503, text="nope")),
            event_hooks={"response": [raise_on_error]},
        )
        with pytest.raises(TransportError) as excinfo:
            asyncio.run(
                HttpxAsyncTransport(client=client)(Request("GET", "https://news.example/x", {}), timeout=10)
            )
        assert excinfo.value.status == 503, "the 429/503 seam callers key off must survive"

    def test_a_body_already_in_memory_is_still_capped(self):
        """A response that arrives already read -- MockTransport, a caller's stub, or a client
        whose response hook read the body -- cannot be bounded while it streams, because there
        is no stream left. Measured 256 MiB peak against a 32 MiB cap through such a hook. The
        cap is still applied, which is the most that is left to do at that point.
        """
        httpx = pytest.importorskip("httpx", reason="the async transport needs the [async] extra")

        from googlenewsdecoder.transports import HttpxAsyncTransport

        oversize = b"\0" * (MAX_RESPONSE_BYTES + 1)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=oversize))
        )
        with pytest.raises(TransportError, match="exceeded"):
            asyncio.run(
                HttpxAsyncTransport(client=client)(Request("GET", "https://news.example/x", {}), timeout=10)
            )


class TestGivingUpOnABodyIsBounded:
    """Both halves of one trade. Abandoning a body we will not use can either drain it, keeping
    the pooled connection, or hang up. The throttle on this endpoint counts CONNECTIONS, so
    draining is usually right -- but the remainder is the peer's number to choose, so it is only
    right while that number is small and declared.

    Neither half is visible to peak memory: `drain_conn()` calls `_raw_read()` and frees the
    decoder, so it never decodes and both choices measure the same there.
    """

    def test_an_error_status_does_not_cost_a_connection(self):
        """A run of 429s is the expected pattern here -- it is what the `with_retries` and
        `stop_on_429` examples in the transport docstring are for -- and hanging up on each one
        measured 10 requests to 10 connections against a budget the probes put at 65-110.
        """
        from googlenewsdecoder.transports import Urllib3Transport

        transport = Urllib3Transport()
        with serving(fixed_body(b"slow down", status=429), keep_alive=True) as server:
            url = f"http://127.0.0.1:{server.server_port}/429"
            for _ in range(10):
                with pytest.raises(TransportError, match="HTTP 429"):
                    transport(Request("GET", url, {}), timeout=10)
            accepted = server.accepted
        assert accepted == 1, (
            f"10 refused requests opened {accepted} connections: the error path is hanging up "
            "on a small declared body instead of draining it"
        )

    @pytest.mark.parametrize("kind", ["sync", "async"])
    def test_a_redirect_chain_does_not_cost_a_connection_per_hop(self, kind):
        """Streaming a response and closing it unread makes httpcore drop the connection, so
        resolving the chain by hand cost one per hop: 5 for a 4-hop chain, where urllib3 used 1.
        The consent 302 is in the hot path, and the throttle counts connections.
        """
        if kind == "async":
            pytest.importorskip("httpx", reason="the async transport needs the [async] extra")
        with serving(redirect_chain(4), keep_alive=True) as server:
            assert fetch(kind, f"http://127.0.0.1:{server.server_port}/0") == "FINAL"
            accepted = server.accepted
        assert accepted == 1, f"a 4-hop chain opened {accepted} connections"

    def test_an_oversize_body_is_not_read_to_the_end(self):
        from googlenewsdecoder.transports import Urllib3Transport

        total = 4 * MAX_RESPONSE_BYTES
        written = {"n": 0}
        with serving(streamed_body(total, written)) as server:
            url = f"http://127.0.0.1:{server.server_port}/big"
            with pytest.raises(TransportError, match="exceeded"):
                Urllib3Transport()(Request("GET", url, {}), timeout=30)
        # The single-threaded server ran the handler inline, so shutdown() joined it: the count
        # is final here without a sleep.
        assert written["n"] < MAX_RESPONSE_BYTES + SOCKET_BUFFER_SLACK, (
            f"the server got {written['n'] >> 20} MiB out of a refused {total >> 20} MiB body: "
            "an unbounded remainder is being drained rather than hung up on"
        )

