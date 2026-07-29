"""One test per defect found by adversarial review.

Each of these was fixed and then verified by hand, which is not the same as being guarded.
A fix without a test is a fix that comes back.
"""

import gzip
import io
import json

import pytest

from googlenewsdecoder import decode, decode_batch, protocol
from googlenewsdecoder.limits import MAX_RESPONSE_BYTES
from googlenewsdecoder.transports import TransportError

GOOGLE_URL = "https://news.google.com/rss/articles/TOKEN?oc=5"


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
    expanding to 1 GB. urllib3 does NOT cap this itself -- measured returning a 33 MB body
    from a 32 KB gzip -- so the transport reads bounded rather than preloading."""

    def _serve(self, payload, encoding):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                if encoding:
                    self.send_header("Content-Encoding", encoding)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def test_a_bomb_is_refused_rather_than_allocated(self):
        from googlenewsdecoder.protocol import Request
        from googlenewsdecoder.transports import Urllib3Transport

        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as f:
            f.write(b"\0" * (MAX_RESPONSE_BYTES + 1024))
        server = self._serve(buf.getvalue(), "gzip")
        try:
            url = f"http://127.0.0.1:{server.server_port}/bomb"
            with pytest.raises(TransportError, match="exceeded"):
                Urllib3Transport()(Request("GET", url, {}), timeout=10)
        finally:
            server.shutdown()

    def test_an_ordinary_body_still_decompresses(self):
        from googlenewsdecoder.protocol import Request
        from googlenewsdecoder.transports import Urllib3Transport

        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as f:
            f.write(b"<html>fine</html>")
        server = self._serve(buf.getvalue(), "gzip")
        try:
            url = f"http://127.0.0.1:{server.server_port}/ok"
            assert Urllib3Transport()(Request("GET", url, {}), timeout=10) == "<html>fine</html>"
        finally:
            server.shutdown()

    def test_a_mismatched_encoding_raises_transport_error(self):
        from googlenewsdecoder.protocol import Request
        from googlenewsdecoder.transports import Urllib3Transport

        server = self._serve(b"not gzipped at all", "gzip")
        try:
            url = f"http://127.0.0.1:{server.server_port}/lie"
            with pytest.raises(TransportError):
                Urllib3Transport()(Request("GET", url, {}), timeout=10)
        finally:
            server.shutdown()

