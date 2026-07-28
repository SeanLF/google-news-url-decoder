"""Tokens that carry their own answer, and the requests that buys back.

Some article tokens base64-decode straight to the publisher URL; others decode to an opaque
handle that only batchexecute can resolve. Telling them apart is pure and free, and when the
URL is already there both HTTP requests can be skipped.

Sizing note, so nobody reads these tests as a throughput claim: the embedded form is rare in
practice. Sampling current feeds finds essentially only handles. The form is real -- it is
this project's own README example -- but these tests guard correctness, not a saving.

The length arithmetic is the reason this file exists. The prefix is a protobuf varint, and
reading it as one raw byte truncated every payload of 128 bytes or more -- silently, because
the short result still began with "http" and was returned as a successful decode.
"""

import base64
import json

import pytest

from googlenewsdecoder import decode, decode_batch, protocol

BBC = "https://www.bbc.com/news/articles/cjjjnxdv188o"


def varint(n: int) -> bytes:
    """Protobuf varint, which is what the length prefix actually is.

    The helper here used to write `bytes([len(payload)])`, a single raw byte. That cannot
    express a payload of 128 bytes or more and raises outright at 256 -- so every token the
    suite could build was short, and the suite was structurally incapable of reaching the
    truncation bug in `unwrap_token`. A test helper that cannot encode the failing case is
    not coverage.
    """
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def token_for(payload: str) -> str:
    """Build a real article token carrying `payload`, framed the way Google frames one."""
    body = bytes([0x08, 0x13, 0x22]) + varint(len(payload)) + payload.encode() + bytes([0xD2, 0x01, 0x00])
    return base64.urlsafe_b64encode(body).decode().rstrip("=")


def url_for(payload: str, path: str = "articles") -> str:
    return f"https://news.google.com/{path}/{token_for(payload)}?oc=5"


class Refuses:
    """Fails the test if it is called at all."""

    def __init__(self):
        self.calls = []

    def __call__(self, request, *, timeout=None, proxy=None):
        self.calls.append(request)
        raise AssertionError(f"no request should have been made, got {request.url}")


def answering_transport(seen):
    def transport(request, *, timeout=None, proxy=None):
        seen.append(request.url)
        if "batchexecute" in request.url:
            frame = ["wrb.fr", "Fbv4je", json.dumps(["garturlres", BBC]), None, None, None, "1"]
            # Two trailer frames: the single-decode parser drops the last two, which is how
            # Google actually terminates a response.
            return ")]}'\n\n" + json.dumps([frame, ["di", 1], ["af.httprm", 1]])
        return '<div data-n-a-sg="SIG" data-n-a-ts="1700">x</div>'

    return transport


class TestTellingTheTwoApart:
    def test_a_token_carrying_a_url_unwraps_to_it(self):
        assert protocol.unwrap_token(token_for(BBC)) == BBC
        assert protocol.embedded_url(token_for(BBC)) == BBC

    def test_a_handle_is_recognised_as_needing_the_rpc(self):
        unwrapped = protocol.unwrap_token(token_for("AU_yqLxxxxx"))
        assert protocol.is_opaque_handle(unwrapped) is True
        assert protocol.embedded_url(token_for("AU_yqLxxxxx")) is None

    def test_something_that_is_neither_is_not_mistaken_for_a_handle(self):
        # The distinction that matters. Reporting not-a-URL as "needs the RPC" would spend
        # two requests on an answer that was never coming.
        unwrapped = protocol.unwrap_token(token_for("nonsense"))
        assert unwrapped == "nonsense"
        assert protocol.is_opaque_handle(unwrapped) is False
        assert protocol.embedded_url(token_for("nonsense")) is None

    @pytest.mark.parametrize("token", ["", "!!!", "A", "=", "\x00"])
    def test_unwrapping_junk_returns_none_rather_than_raising(self, token):
        # Callers treat this as a cheap pre-check, so it must never be the thing that throws.
        assert protocol.unwrap_token(token) in (None, "")

    # The length prefix is a protobuf varint: one byte below 128, two bytes above. Reading it
    # as a single raw byte truncated every longer payload -- and since the result still began
    # with "http", it was returned as a successful decode rather than an error. A news URL with
    # a slug and UTM parameters crosses 128 bytes routinely.
    @pytest.mark.parametrize("length", [1, 100, 126, 127, 128, 129, 200, 255, 256, 300, 1000])
    def test_a_payload_of_any_length_survives_the_round_trip(self, length):
        payload = "https://e.com/" + "a" * (length - 14)
        assert protocol.unwrap_token(token_for(payload)) == payload

    def test_a_long_embedded_url_is_not_silently_shortened(self):
        long_url = (
            "https://www.theguardian.com/us-news/2026/jul/28/"
            "federal-reserve-interest-rate-decision-markets-reaction-analysis"
            "?utm_source=rss&utm_medium=feed"
        )
        assert len(long_url) > 128, "this test is pointless if the URL is short"
        assert protocol.embedded_url(token_for(long_url)) == long_url
        assert decode(url_for(long_url), transport=Refuses())["decoded_url"] == long_url


class TestRequestsAreSkipped:
    def test_decode_answers_an_embedded_url_without_any_request(self):
        refuses = Refuses()
        result = decode(url_for(BBC), transport=refuses)
        assert result == {"status": True, "decoded_url": BBC}
        assert refuses.calls == []

    def test_decode_batch_answers_embedded_urls_without_any_request(self):
        refuses = Refuses()
        urls = [url_for("https://a.example/1"), url_for("https://b.example/2")]
        results = decode_batch(urls, transport=refuses)
        assert [r["decoded_url"] for r in results] == ["https://a.example/1", "https://b.example/2"]
        assert refuses.calls == []

    def test_a_handle_still_spends_the_round_trip(self):
        seen = []
        result = decode(url_for("AU_yqLhandle"), transport=answering_transport(seen))
        assert result["decoded_url"] == BBC
        assert any("batchexecute" in url for url in seen)

    def test_a_mixed_batch_only_fetches_the_handles(self):
        seen = []
        urls = [url_for("https://a.example/1"), url_for("AU_yqLhandle"), url_for("https://c.example/3")]
        results = decode_batch(urls, transport=answering_transport(seen))

        assert [r["status"] for r in results] == [True, True, True]
        # Order is preserved and the embedded ones kept their own URLs, not the handle's.
        assert results[0]["decoded_url"] == "https://a.example/1"
        assert results[1]["decoded_url"] == BBC
        assert results[2]["decoded_url"] == "https://c.example/3"
        # One article GET for the single handle, plus one POST. Not three GETs.
        article_gets = [u for u in seen if "batchexecute" not in u]
        assert len(article_gets) == 1
