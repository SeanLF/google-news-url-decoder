"""One test per defect found by adversarial review.

Each of these was fixed and then verified by hand, which is not the same as being guarded.
A fix without a test is a fix that comes back.
"""

import contextlib
import functools
import gzip
import io
import json
import threading
import tracemalloc
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

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

