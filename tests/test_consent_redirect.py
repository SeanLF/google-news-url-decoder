"""The transports must never send a cookie, and must not leak credentials across a redirect.

Google's consent endpoint walls any client that replays the `SOCS` cookie it sets on the
article's 302, so this protocol is cookieless. Both properties are asserted on the wire rather
than on a mechanism, so they survive swapping the client underneath.

The sync tests use a loopback HTTP server rather than a fake: the property under test is what
urllib3 puts on the wire, and a fake of urllib3 would only assert our beliefs about it.
`conftest.py` leaves loopback allowed.
"""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from googlenewsdecoder.protocol import Request
from googlenewsdecoder.transports import TransportError, Urllib3Transport


class _Origins:
    """Two origins: the first redirects to the second, setting a cookie on the way."""

    def __init__(self):
        self.seen = {}
        self.dest = self._serve("DEST")
        dest_url = f"http://127.0.0.1:{self.dest.server_port}/article"
        self.src = self._serve("SRC", redirect_to=dest_url)
        # localhost vs 127.0.0.1 is a cross-origin hop for credential-stripping purposes.
        self.src_url = f"http://localhost:{self.src.server_port}/start"

    def _serve(self, name, redirect_to=None):
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.setdefault(name, []).append(dict(self.headers))
                if redirect_to:
                    self.send_response(302)
                    self.send_header("Location", redirect_to)
                    self.send_header("Set-Cookie", "SOCS=CAAaBgiAhaXTBg; Path=/")
                    self.end_headers()
                    return
                body = b'<div data-n-a-sg="sig" data-n-a-ts="123"></div>'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def shutdown(self):
        self.src.shutdown()
        self.dest.shutdown()


@pytest.fixture
def origins():
    made = _Origins()
    yield made
    made.shutdown()


class TestCookiesNeverReachTheWire:
    def test_the_cookie_set_on_the_redirect_is_not_replayed(self, origins):
        # THE regression: replaying SOCS to the consent endpoint is what earns the 644 KB
        # interstitial instead of a bounce back to the article.
        Urllib3Transport()(Request("GET", origins.src_url, {}), timeout=10)
        assert origins.seen["DEST"][0].get("Cookie") is None

    def test_a_caller_supplied_cookie_is_not_sent_either(self, origins):
        # "Send an accepted consent cookie" is the advice circulating online. It does not work
        # on this endpoint, and honouring it would defeat the property above.
        Urllib3Transport(headers={"Cookie": "SOCS=accepted"})(
            Request("GET", origins.src_url, {}), timeout=10
        )
        assert all(h.get("Cookie") is None for h in origins.seen["DEST"])


class TestCredentialsDoNotCrossOrigins:
    def test_authorization_is_stripped_on_a_cross_origin_redirect(self, origins):
        # A hand-rolled redirect loop passed caller headers unchanged to every hop, so a key
        # meant for one host reached another.
        Urllib3Transport(headers={"Authorization": "Bearer SECRET"})(
            Request("GET", origins.src_url, {}), timeout=10
        )
        assert origins.seen["SRC"][0].get("Authorization") == "Bearer SECRET"
        assert origins.seen["DEST"][0].get("Authorization") is None


def _serve_once(status, headers=None, body=b"<html>body</html>"):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class TestANonSuccessBodyIsNeverReturned:
    """The bug this release exists to remove was a non-article body accepted as an article."""

    def test_a_3xx_with_no_location_is_refused_not_returned(self):
        # urllib3 cannot follow it, so it hands the response back. Refusing only >= 400 would
        # return the redirect's own body as the article.
        server = _serve_once(302, body=b"<html>redirect body</html>")
        try:
            url = f"http://127.0.0.1:{server.server_port}/x"
            with pytest.raises(TransportError) as caught:
                Urllib3Transport()(Request("GET", url, {}), timeout=10)
            assert caught.value.status == 302
        finally:
            server.shutdown()

    def test_an_error_status_reaches_the_caller_as_a_status(self):
        # `stop_on_429` in the module docstring tells callers to key on this.
        server = _serve_once(429)
        try:
            url = f"http://127.0.0.1:{server.server_port}/x"
            with pytest.raises(TransportError) as caught:
                Urllib3Transport()(Request("GET", url, {}), timeout=10)
            assert caught.value.status == 429
        finally:
            server.shutdown()

    def test_an_undecodable_encoding_fails_loudly_rather_than_returning_junk(self):
        # urllib3 passes an encoding it has no decoder for straight through, which becomes
        # mojibake and gets blamed on Google's markup.
        server = _serve_once(200, {"Content-Encoding": "made-up"}, b"not-really-encoded")
        try:
            url = f"http://127.0.0.1:{server.server_port}/x"
            with pytest.raises(TransportError, match="unsupported Content-Encoding"):
                Urllib3Transport()(Request("GET", url, {}), timeout=10)
        finally:
            server.shutdown()

    def test_compression_is_actually_requested(self):
        # identity costs ~6.5x the bytes on a real article page, on an endpoint that throttles.
        seen = {}

        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["ae"] = self.headers.get("Accept-Encoding")
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/x"
            Urllib3Transport()(Request("GET", url, {}), timeout=10)
            assert "gzip" in (seen["ae"] or "")
        finally:
            server.shutdown()


class TestAsyncTransportIsCookieless:
    def test_no_cookie_or_credential_survives_the_chain(self):
        # The async loop is hand-written, so both rules it enforces need covering here: the
        # sync tests exercise urllib3's implementations, not this one.
        httpx = pytest.importorskip("httpx")

        from googlenewsdecoder.transports import HttpxAsyncTransport

        seen = []

        def handler(request):
            seen.append(
                (
                    str(request.url),
                    request.headers.get("cookie"),
                    request.headers.get("authorization"),
                )
            )
            if request.url.path == "/start":
                return httpx.Response(
                    302,
                    headers={
                        "Location": "https://consent.example/m",
                        "Set-Cookie": "SOCS=CAAaBgiAhaXTBg; Path=/",
                    },
                )
            if request.url.host == "consent.example":
                return httpx.Response(303, headers={"Location": "https://news.example/article"})
            return httpx.Response(200, text="ok")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = HttpxAsyncTransport(client=client, headers={"Authorization": "Bearer SECRET"})
        body = asyncio.run(
            transport(Request("GET", "https://news.example/start", {}), timeout=10)
        )

        assert body == "ok"
        assert len(seen) == 3, seen
        assert all(cookie is None for _, cookie, _ in seen), seen
        # The credential belongs to news.example and must not reach the consent host.
        assert seen[0][2] == "Bearer SECRET"
        assert seen[1][2] is None, seen[1]

    def test_the_redirect_cap_is_enforced(self):
        httpx = pytest.importorskip("httpx")

        from googlenewsdecoder.transports import MAX_REDIRECTS, HttpxAsyncTransport
        from googlenewsdecoder.transports import TransportError as TE

        hops = []

        def handler(request):
            hops.append(request.url)
            return httpx.Response(302, headers={"Location": "https://news.example/loop"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(TE, match="redirects"):
            asyncio.run(
                HttpxAsyncTransport(client=client)(
                    Request("GET", "https://news.example/start", {}), timeout=10
                )
            )
        # MAX_REDIRECTS redirects means one more request than that. This asserted equality with
        # MAX_REDIRECTS, i.e. a budget of 10 REQUESTS and so 9 redirects, where urllib3 gives the
        # sync transport 10 redirects from the same constant -- a chain of exactly 10 resolved
        # through one transport and was refused by the other.
        assert len(hops) == MAX_REDIRECTS + 1

    def test_a_chain_of_exactly_max_redirects_still_resolves(self):
        """The boundary the mismatch was hiding: both transports must accept this."""
        httpx = pytest.importorskip("httpx")

        from googlenewsdecoder.transports import MAX_REDIRECTS, HttpxAsyncTransport

        seen = []

        def handler(request):
            seen.append(request.url)
            if len(seen) <= MAX_REDIRECTS:
                return httpx.Response(302, headers={"Location": f"https://news.example/{len(seen)}"})
            return httpx.Response(200, text="ok")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        body = asyncio.run(
            HttpxAsyncTransport(client=client)(Request("GET", "https://news.example/start", {}), timeout=10)
        )
        assert body == "ok"
        assert len(seen) == MAX_REDIRECTS + 1

    @pytest.mark.parametrize(
        "location",
        [
            "//]/",
            "http://[/",
            "http://127.0.0.1:99999/x",
            "data:text/html,x",
            "javascript:alert(1)",
            "http://xn--/",
            "http://xn--a/",
        ],
        ids=[
            "invalid-ipv6",
            "unterminated-bracket",
            "port-out-of-range",
            "data-scheme",
            "javascript-scheme",
            "invalid-a-label",
            "invalid-a-label-codepoint",
        ],
    )
    def test_a_malformed_location_stays_inside_the_contract(self, location):
        """Every one of these left the transport as something `drive_async` does not catch, so
        one bad Location took every already-decoded result in the batch with it.

        Three different escapes, which is why the list is this long: stdlib's parser raises
        ValueError on the bracket cases, httpx's URL layer raises InvalidURL on a scheme with no
        authority and an idna error (a UnicodeError, so a ValueError) on an invalid A-label, and
        an out-of-range port on an IP literal reaches connect and comes back as an
        ExceptionGroup. The first group is caught before the request, the rest around it,
        because httpx resolves the previous Location inside send().
        """
        httpx = pytest.importorskip("httpx")

        from googlenewsdecoder.transports import HttpxAsyncTransport
        from googlenewsdecoder.transports import TransportError as TE

        def handler(request):
            return httpx.Response(302, headers={"Location": location})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(TE, match="malformed Location|unusable URL"):
            asyncio.run(
                HttpxAsyncTransport(client=client)(
                    Request("GET", "https://news.example/start", {}), timeout=10
                )
            )

    def test_a_caller_supplied_cookie_survives_the_chain_without_being_sent(self):
        """The cookie rule was enforced by emptying `client.cookies` on every hop, which also
        throws away cookies the caller set for their own use -- an auth cookie for a corporate
        egress proxy, say. Dropping the header off our own requests achieves the same thing
        without destroying their jar.

        Not the same as leaving the jar untouched: httpx extracts Set-Cookie into it inside
        `_send_single_request`, so Google's SOCS lands there either way. Asserted, because it is
        the real contract -- nothing we send carries a cookie, and what the jar collects is the
        caller's to deal with -- and because it fails loudly if httpx ever moves where cookies
        are applied to a request.
        """
        httpx = pytest.importorskip("httpx")

        from googlenewsdecoder.transports import HttpxAsyncTransport

        sent = []

        def handler(request):
            sent.append(request.headers.get("cookie"))
            if request.url.path == "/start":
                return httpx.Response(
                    302,
                    headers={"Location": "https://news.example/article", "Set-Cookie": "SOCS=x; Path=/"},
                )
            return httpx.Response(200, text="ok")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client.cookies.set("mine", "keep-me", domain="news.example")
        transport = HttpxAsyncTransport(client=client)
        assert asyncio.run(transport(Request("GET", "https://news.example/start", {}), timeout=10)) == "ok"

        assert all(cookie is None for cookie in sent), sent
        assert client.cookies.get("mine", domain="news.example") == "keep-me"
        assert client.cookies.get("SOCS", domain="news.example") == "x", (
            "httpx extracts Set-Cookie into the caller's jar; if that ever stops being true, "
            "the reason nothing replays it is no longer the popped header"
        )

    def test_a_303_turns_a_post_into_a_get_without_its_body(self):
        httpx = pytest.importorskip("httpx")

        from googlenewsdecoder.transports import HttpxAsyncTransport

        seen = []

        def handler(request):
            seen.append((request.method, request.read(), request.headers.get("content-type")))
            if len(seen) == 1:
                return httpx.Response(303, headers={"Location": "https://news.example/x"})
            return httpx.Response(200, text="ok")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        asyncio.run(
            HttpxAsyncTransport(client=client)(
                Request("POST", "https://news.example/rpc", {"Content-Type": "application/json"}, b"payload"),
                timeout=10,
            )
        )
        assert seen[1][0] == "GET"
        assert seen[1][1] == b""
        assert seen[1][2] is None, "Content-Type must not ride along on a bodyless GET"
