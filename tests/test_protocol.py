"""Tests for the decode protocol.

Every test here is a pure function call. No mocks, no monkeypatching, no sockets,
no fixtures standing in for a network. That is the point of keeping the protocol
separate from the transport: the part with all the fiddly detail in it is the
part that needs no I/O to test.
"""

import json
from urllib.parse import parse_qs, unquote

import pytest

from googlenewsdecoder import protocol

TOKEN = "CBMiSHORTTOKEN"


class TestArticleId:
    @pytest.mark.parametrize(
        "url",
        [
            f"https://news.google.com/rss/articles/{TOKEN}?oc=5",
            f"https://news.google.com/articles/{TOKEN}",
            f"https://news.google.com/read/{TOKEN}",
            f"https://NEWS.GOOGLE.COM/rss/articles/{TOKEN}",
            f"http://news.google.com/rss/articles/{TOKEN}",
            f"https://news.google.com/rss/articles/{TOKEN}?hl=en-US&gl=US&ceid=US:en",
        ],
        ids=["rss", "articles", "read", "uppercase-host", "http", "full-query"],
    )
    def test_accepts_the_shapes_google_actually_emits(self, url):
        assert protocol.article_id(url) == TOKEN

    @pytest.mark.parametrize(
        "url",
        [
            f"https://evil.com/fakepath/{TOKEN}",
            f"https://foo.news.google.com/rss/articles/{TOKEN}",
            "https://news.google.com/rss/search?q=site:reuters.com",
            "https://news.google.com",
            "",
        ],
        ids=["other-host", "subdomain", "not-an-article", "bare-host", "empty"],
    )
    def test_rejects_everything_else(self, url):
        assert protocol.article_id(url) is None

    def test_bounds_the_token_when_asked(self):
        long_url = "https://news.google.com/rss/articles/" + "A" * 100
        assert protocol.article_id(long_url, max_length=50) is None
        assert protocol.article_id(long_url, max_length=100) is not None  # inclusive


class TestDecodeRequest:
    def test_the_rpc_envelope_has_three_levels_of_nesting(self):
        """Two levels is accepted by json.dumps and rejected by Google with HTTP 400.

        This is worth pinning: the difference between `[[rpc]]` and `[[[rpc]]]` is
        invisible in review, produces no error locally, and fails 100% of the time
        against the live endpoint.
        """
        request = protocol.decode_request(TOKEN, "SIG", "1700000000")
        envelope = json.loads(parse_qs(request.body.decode())["f.req"][0])

        assert isinstance(envelope, list)
        assert isinstance(envelope[0], list)
        assert isinstance(envelope[0][0], list)
        assert envelope[0][0][0] == "Fbv4je"

    def test_the_timestamp_is_a_bare_number_not_a_string(self):
        request = protocol.decode_request(TOKEN, "SIG", "1700000000")
        inner = json.loads(parse_qs(request.body.decode())["f.req"][0])[0][0][1]
        assert ',1700000000,"SIG"' in inner
        assert '"1700000000"' not in inner

    def test_it_says_what_to_send_without_sending(self):
        request = protocol.decode_request(TOKEN, "SIG", "1")
        assert request.method == "POST"
        assert request.url == protocol.BATCHEXECUTE_URL
        assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
        assert TOKEN in unquote(request.body.decode())


class TestParseParams:
    def test_reads_both_attributes_off_one_element(self):
        html = '<c-wiz><div jscontroller="x" data-n-a-sg="SIG" data-n-a-ts="1700">x</div></c-wiz>'
        assert protocol.parse_params(html) == ("SIG", "1700")

    def test_reads_them_in_either_attribute_order(self):
        html = '<div data-n-a-ts="1700" data-n-a-sg="SIG">x</div>'
        assert protocol.parse_params(html) == ("SIG", "1700")

    def test_will_not_pair_a_signature_and_timestamp_from_different_elements(self):
        """Two independent document-wide searches would return ("SIG", "9999") here.

        A mismatched pair is worse than no pair: it builds a well-formed request
        that Google rejects, and the failure surfaces much later as a confusing
        parse error rather than as "could not find the parameters".
        """
        html = '<div data-n-a-ts="9999">a</div><div data-n-a-sg="SIG" data-n-a-ts="1700">b</div>'
        assert protocol.parse_params(html) == ("SIG", "1700")

    def test_none_when_absent(self):
        assert protocol.parse_params("<html>nothing here</html>") is None
        assert protocol.parse_params("") is None


class TestParseDecoded:
    def _response(self, url):
        """The batchexecute shape: one array whose trailing two entries are housekeeping."""
        body = [
            ["wrb.fr", "Fbv4je", json.dumps(["garturlres", url]), None, None, None, "generic"],
            ["di", 22],
            ["af.httprm", 22],
        ]
        return ")]}'\n\n" + json.dumps(body)

    def test_pulls_the_publisher_url_out(self):
        assert protocol.parse_decoded(self._response("https://www.reuters.com/world/x")) == (
            "https://www.reuters.com/world/x"
        )

    @pytest.mark.parametrize(
        "body", ["", "garbage", ")]}'\n\n[]", ")]}'\n\nnot json"], ids=["empty", "garbage", "empty-list", "not-json"]
    )
    def test_none_rather_than_raising_on_anything_unexpected(self, body):
        assert protocol.parse_decoded(body) is None

    def test_rejects_a_non_url_payload(self):
        assert protocol.parse_decoded(self._response("not-a-url")) is None
