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
    """Build a real article token carrying `payload`, framed the way Google frames one.

    The length is measured in BYTES, not characters. Writing `varint(len(payload))` against the
    str while framing `payload.encode()` under-counts every non-ASCII payload, so the frame
    claims fewer bytes than it carries and the token silently truncates -- which made the
    non-ASCII tests below fail on length rather than on the encoding bug they exist to catch.
    That is the same defect this helper's own docstring warns about, reintroduced one axis over.
    """
    encoded = payload.encode("utf-8")
    body = bytes([0x08, 0x13, 0x22]) + varint(len(encoded)) + encoded + bytes([0xD2, 0x01, 0x00])
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


def answering_transport(seen, batch_size=8):
    """Answers both legs, and answers every position in a batch rather than only the first."""

    def transport(request, *, timeout=None, proxy=None):
        seen.append(request.url)
        if "batchexecute" in request.url:
            frames = [
                ["wrb.fr", "Fbv4je", json.dumps(["garturlres", BBC]), None, None, None, str(tag)]
                for tag in range(1, batch_size + 1)
            ]
            # Two trailer frames: the single-decode parser drops the last two, which is how
            # Google actually terminates a response.
            return ")]}'\n\n" + json.dumps(frames + [["di", 1], ["af.httprm", 1]])
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
        assert protocol.unwrap_token(token_for(long_url)) == long_url
        assert protocol.embedded_url(token_for(long_url)) == long_url


class TestTheTokenIsNeverTrustedAsTheAnswer:
    """The flows do not short-circuit on an inline URL, and that is the point.

    An earlier version of this branch did, on the reasoning that it saved two requests. It
    saved nothing measurable -- current feeds carry essentially only handles -- and it made
    `decode()` hand back its own caller's input as an authoritative answer, with `status:
    True`, having asked Google nothing. Anything starting "http" qualified.
    """

    def test_decode_asks_google_even_when_the_token_carries_a_url(self):
        seen = []
        result = decode(url_for(BBC), transport=answering_transport(seen))
        assert result["status"] is True
        assert seen, "the answer must come from Google, not from the caller's own token"

    @pytest.mark.parametrize(
        "payload",
        [
            "https://evil.example.com/phish",
            "https://www.bbc.com@evil.example.com/x",
            "https://good.com/a\r\nX-Injected: yes",
            "http",
            "httpNOT-A-URL-AT-ALL",
        ],
        ids=["other-host", "userinfo-confusion", "crlf", "bare-scheme", "not-a-url"],
    )
    def test_a_crafted_token_cannot_become_the_decoded_url(self, payload):
        result = decode(url_for(payload), transport=Refuses())
        assert result["status"] is False, f"{payload!r} was returned as the answer"

    def test_a_token_with_no_frame_tag_is_no_shortcut_either(self):
        # `unwrap_token` does not require the 08 13 22 tag, so nothing has to imitate Google's
        # framing for this to have been reachable.
        raw = varint(len(b"https://evil.example.com/notag")) + b"https://evil.example.com/notag"
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        result = decode(f"https://news.google.com/articles/{token}", transport=Refuses())
        assert result["status"] is False

    def test_a_handle_spends_the_round_trip(self):
        seen = []
        result = decode(url_for("AU_yqLhandle"), transport=answering_transport(seen))
        assert result["decoded_url"] == BBC
        assert any("batchexecute" in url for url in seen)

    def test_a_batch_keeps_results_aligned_to_input_order(self):
        seen = []
        urls = [url_for("https://a.example/1"), url_for("AU_yqLhandle"), url_for("https://c.example/3")]
        results = decode_batch(urls, transport=answering_transport(seen))
        assert [r["status"] for r in results] == [True, True, True]
        assert all(r["decoded_url"] == BBC for r in results), "this transport answers everything with BBC"
        article_gets = [u for u in seen if "batchexecute" not in u]
        assert len(article_gets) == 3, "every URL costs its own article fetch"


class TestEmbeddedUrlValidatesWhatItReturns:
    """`embedded_url` stays public for callers who want to make that trade themselves, so what
    it hands back has to be a URL rather than any string beginning with the right four bytes."""

    @pytest.mark.parametrize(
        "payload",
        ["http", "httpNOT-A-URL", "https://good.com/a\r\nX-Injected: yes", "https://good.com/a\x00.evil", "https://"],
        ids=["bare-scheme", "no-host", "crlf", "nul", "no-host-at-all"],
    )
    def test_it_refuses_things_that_merely_start_with_http(self, payload):
        assert protocol.embedded_url(token_for(payload)) is None

    @pytest.mark.parametrize("scheme", ["javascript", "data", "file", "ftp"])
    def test_it_refuses_non_http_schemes(self, scheme):
        assert protocol.embedded_url(token_for(f"{scheme}://evil.example.com/x")) is None

    def test_it_still_returns_an_ordinary_url(self):
        assert protocol.embedded_url(token_for(BBC)) == BBC


class TestNonAsciiSurvives:
    """A protobuf string field is UTF-8. Decoding it as latin-1 turned every accented URL into
    mojibake that still began with "http", so it passed every check and was returned as the
    answer -- the same silent-wrong-answer class as the truncation above, on another axis."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.spiegel.de/politik/münchen-gipfel",
            "https://elpais.com/españa/madrid",
            "https://www.lemonde.fr/société/à-la-une",
            "https://www3.nhk.or.jp/news/ニュース",
        ],
        ids=["german", "spanish", "french", "japanese"],
    )
    def test_a_non_ascii_url_round_trips(self, url):
        assert protocol.unwrap_token(token_for(url)) == url
        assert protocol.embedded_url(token_for(url)) == url

    def test_a_payload_that_is_not_valid_utf8_is_refused(self):
        raw = b"\x08\x13\x22" + varint(4) + b"\xff\xfe\xfd\xfc"
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        assert protocol.unwrap_token(token) is None
