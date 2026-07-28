"""Regression tests for the bounds added in response to issue #18.

Network-free: the HTTP calls are monkeypatched, so these run anywhere.

Run with:  python -m pytest tests/ -q
"""

import time
import types

import pytest

# these public names are the decode functions themselves, not modules (see __init__.py)
from googlenewsdecoder import decoderv1, decoderv2, decoderv3, decoderv4
from googlenewsdecoder.limits import (
    DEFAULT_TIMEOUT,
    MAX_INTERVAL,
    MAX_TOKEN_LENGTH,
    clamp_interval,
)
from googlenewsdecoder.new_decoderv2 import GoogleDecoder

GOOGLE_URL = "https://news.google.com/rss/articles/CBMiSHORTTOKEN?oc=5"
OTHER_HOST_URL = "https://evil.com/fakepath/CBMiSHORTTOKEN"
OVERSIZED_URL = "https://news.google.com/rss/articles/" + "A" * (MAX_TOKEN_LENGTH + 1)


class RecordingTransport:
    """A transport that records what it was asked to send and sends nothing.

    This is the seam the protocol/transport split buys: the decoder can be driven
    without monkeypatching a third-party module out from under it, and without a
    socket anywhere in the test.
    """

    def __init__(self, body=""):
        self.calls = []
        self.body = body

    def __call__(self, request, *, timeout=None, proxy=None):
        self.calls.append({"request": request, "timeout": timeout, "proxy": proxy})
        return self.body


@pytest.fixture
def recording():
    return RecordingTransport()


def working_transport(body=""):
    """Answers both legs of a decode, so a whole flow can run without a network."""
    import json

    def transport(request, *, timeout=None, proxy=None):
        if "batchexecute" in request.url:
            payload = [
                ["wrb.fr", "Fbv4je", json.dumps(["garturlres", "https://publisher.example/x"]), None, None, None, "g"],
                ["di", 1],
                ["af.httprm", 1],
            ]
            return ")]}'\n\n" + json.dumps(payload)
        return '<div data-n-a-sg="SIG" data-n-a-ts="1700">x</div>'

    return transport


class TestHostnameIsChecked:
    """The guard read `hostname == "news.google.com" and path[-2] == "articles" or "read"`,
    which groups as `(... and ...) or "read"` and so was true for every URL."""

    def test_v2_leaves_a_url_from_another_host_undecoded(self):
        assert decoderv2(OTHER_HOST_URL) == OTHER_HOST_URL

    def test_v3_rejects_a_url_from_another_host(self):
        assert decoderv3(OTHER_HOST_URL)["status"] is False

    def test_v2_still_decodes_read_paths(self):
        # the `or "read"` was meant to accept /read/ alongside /articles/; keep that working
        url = "https://news.google.com/read/" + "A" * 32
        assert decoderv2(url) != url


class TestOversizedTokenIsRejectedBeforeDecoding:
    """The token is the last path segment, so its size is caller-controlled. Decoding it
    unbounded lets a multi-megabyte URL force a matching allocation."""

    def test_v1_returns_the_source_url_untouched(self):
        assert decoderv1(OVERSIZED_URL) == OVERSIZED_URL

    def test_v2_returns_the_source_url_untouched(self):
        assert decoderv2(OVERSIZED_URL) == OVERSIZED_URL

    # v3 and v4 wrap everything in `except Exception`, so asserting only that status is
    # False cannot tell "rejected by the length check" from "blew up inside b64decode and
    # got swallowed". Assert on which one it was: rejected at the URL guard means the
    # decode was never attempted.
    def test_v3_rejects_it_at_the_url_guard_rather_than_in_the_decoder(self):
        result = decoderv3(OVERSIZED_URL)
        assert result["status"] is False
        assert result["error"] == "Invalid Google News URL"
        assert "base64" not in result["error"]

    def test_v4_rejects_it_at_the_url_guard_rather_than_in_the_decoder(self):
        result = decoderv4([OVERSIZED_URL])[0]
        assert result["status"] is False
        assert result["error"] == "Invalid Google News URL"
        assert "base64" not in result["error"]

    def test_a_real_token_still_decodes(self):
        # From the worked example in decoderv1.py. Guards against the bound being set so
        # low, or the guard so tight, that legitimate URLs stop resolving.
        url = (
            "https://news.google.com/rss/articles/CBMiLmh0dHBzOi8vd3d3LmJiYy5jb20vbmV3cy9h"
            "cnRpY2xlcy9jampqbnhkdjE4OG_SATJodHRwczovL3d3dy5iYmMuY29tL25ld3MvYXJ0aWNsZXMv"
            "Y2pqam54ZHYxODhvLmFtcA?oc=5"
        )
        assert decoderv1(url) == "https://www.bbc.com/news/articles/cjjjnxdv188o"


    def test_the_documented_entry_point_bounds_it_too(self):
        # gnewsdecoder -> GoogleDecoder -> new_decoderv2 is the only path the README
        # documents. An earlier draft bounded only decoderv1-v4, which it never mentions.
        assert GoogleDecoder().get_base64_str(OVERSIZED_URL)["status"] is False

    def test_a_token_of_exactly_the_limit_is_still_accepted(self):
        at_limit = "https://news.google.com/rss/articles/" + "A" * MAX_TOKEN_LENGTH
        assert GoogleDecoder().get_base64_str(at_limit)["status"] is True


class TestIntervalIsBounded:
    """interval goes straight into time.sleep()/asyncio.sleep(), so a large value from
    untrusted input pins a worker for as long as the caller likes."""

    def test_large_interval_is_clamped(self):
        assert clamp_interval(999999) == MAX_INTERVAL

    def test_ordinary_interval_is_untouched(self):
        assert clamp_interval(2) == 2

    @pytest.mark.parametrize("value", ["30", 1j, [1], b"5"])
    def test_values_that_cannot_be_compared_to_a_number_are_left_alone(self, value):
        # min() would raise here and name this module; time.sleep() names the caller's type.
        assert clamp_interval(value) is value
        with pytest.raises(TypeError):
            time.sleep(clamp_interval(value))

    def test_a_legitimate_backoff_is_not_silently_truncated(self):
        assert clamp_interval(300) == 300

    def test_values_time_sleep_would_reject_are_passed_through_unchanged(self):
        # Deliberately no validation: every call site already guards with `if interval:`,
        # and a bad value should keep raising from time.sleep() with its original error
        # rather than a new one pointing at this helper.
        assert clamp_interval(-5) == -5
        with pytest.raises(ValueError):
            time.sleep(clamp_interval(-5))

    def test_sleep_never_receives_more_than_the_cap(self, monkeypatch):
        slept = []
        monkeypatch.setattr("googlenewsdecoder.new_decoderv2.time.sleep", slept.append)
        GoogleDecoder(transport=working_transport()).decode_google_news_url(GOOGLE_URL, interval=10**9)
        assert slept == [MAX_INTERVAL]


class TestEveryRequestIsTimed:
    """requests applies no timeout unless asked, so a peer that accepts a connection and
    never answers holds the calling thread open forever."""

    def test_every_leg_of_a_decode_carries_the_timeout(self, recording):
        # Both the article-page GET and the batchexecute POST go through one seam now,
        # so one assertion covers every request the decode makes.
        GoogleDecoder(transport=recording).decode_google_news_url(GOOGLE_URL)
        assert recording.calls, "no request was made"
        assert all(c["timeout"] == DEFAULT_TIMEOUT for c in recording.calls)

    def test_the_default_transport_is_requests(self):
        # requests is the default deliberately: it bounds decompression (urllib3 >=2.7),
        # normalises content-encoding failures, and lets an explicit proxy beat NO_PROXY.
        # UrllibTransport exists for callers who cannot take a dependency and accept less.
        from googlenewsdecoder.transports import RequestsTransport

        assert isinstance(GoogleDecoder().transport, RequestsTransport)



class TestSocksProxies:
    """requests handles socks via PySocks; urllib cannot, and must say so."""

    def test_urllib_transport_refuses_a_socks_proxy_clearly(self):
        from googlenewsdecoder.protocol import params_request
        from googlenewsdecoder.transports import TransportError, UrllibTransport

        with pytest.raises(TransportError, match="SOCKS"):
            UrllibTransport()(params_request("https://news.google.com/articles/T"), proxy="socks5://127.0.0.1:9050")

    def test_it_does_not_quietly_treat_socks_as_an_http_proxy(self):
        # urllib's ProxyHandler would connect to the host and speak HTTP at it, failing with
        # "connection reset" -- a confusing error for a proxy that is simply unsupported.
        from googlenewsdecoder.protocol import params_request
        from googlenewsdecoder.transports import TransportError, UrllibTransport

        try:
            UrllibTransport()(params_request("https://news.google.com/articles/T"), proxy="socks5h://127.0.0.1:9050")
        except TransportError as e:
            assert "SOCKS" in str(e) and "reset" not in str(e)
