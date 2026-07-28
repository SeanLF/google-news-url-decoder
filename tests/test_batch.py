"""Batch decoding: many URLs, one POST, results aligned to input order.

The response frames come back in an arbitrary order. Measured against the live endpoint
for a five-item batch, the tags returned were 2, 4, 1, 3, 5. Every test here builds a
response in a deliberately wrong order, because getting this right is the entire reason
batching belongs in the library rather than in each caller.
"""

import json

import pytest

from googlenewsdecoder import protocol
from googlenewsdecoder.flow import decode_batch_flow, drive
from googlenewsdecoder.transports import TransportError


def url_for(n):
    return f"https://news.google.com/rss/articles/TOKEN{n}?oc=5"


def batch_response(pairs):
    """A batched response carrying (tag, url) frames, in whatever order is given."""
    frames = [
        ["wrb.fr", "Fbv4je", json.dumps(["garturlres", url]), None, None, None, str(tag)] for tag, url in pairs
    ]
    return ")]}'\n\n" + json.dumps(frames + [["di", 1], ["af.httprm", 1]])


class ScriptedTransport:
    """Answers signature fetches, then returns a scripted batch response."""

    def __init__(self, batch_body, params="1700"):
        self.batch_body = batch_body
        self.params = params
        self.requests = []

    def __call__(self, request, *, timeout=None, proxy=None):
        self.requests.append(request)
        if request.method == "POST":
            return self.batch_body
        return f'<div data-n-a-sg="SIG" data-n-a-ts="{self.params}">x</div>'


class TestResultsAlignToInputOrder:
    def test_a_shuffled_response_is_mapped_back_correctly(self):
        """The failure this prevents: zip(request_order, response_order) hands every
        article another article's URL. Plausible, silent, and wrong."""
        urls = [url_for(i) for i in range(1, 6)]
        shuffled = [(2, "https://pub/two"), (4, "https://pub/four"), (1, "https://pub/one"),
                    (3, "https://pub/three"), (5, "https://pub/five")]
        transport = ScriptedTransport(batch_response(shuffled))

        results = drive(decode_batch_flow(urls), transport)

        assert [r["decoded_url"] for r in results] == [
            "https://pub/one", "https://pub/two", "https://pub/three", "https://pub/four", "https://pub/five",
        ]

    def test_one_post_serves_the_whole_batch(self):
        urls = [url_for(i) for i in range(1, 6)]
        transport = ScriptedTransport(batch_response([(i, f"https://pub/{i}") for i in range(1, 6)]))

        drive(decode_batch_flow(urls), transport)

        posts = [r for r in transport.requests if r.method == "POST"]
        gets = [r for r in transport.requests if r.method == "GET"]
        assert len(posts) == 1, "the batch should cost exactly one POST"
        assert len(gets) == 5, "each article still needs its own signature fetch"


class TestPartialAndMalformedResponses:
    def test_a_missing_frame_fails_only_its_own_url(self):
        """If the endpoint caps a batch, the shortfall must surface as a visible failure
        for the specific URLs, never as a silent shift of everyone else's results."""
        urls = [url_for(i) for i in range(1, 5)]
        transport = ScriptedTransport(batch_response([(1, "https://pub/one"), (3, "https://pub/three")]))

        results = drive(decode_batch_flow(urls), transport)

        assert results[0] == {"status": True, "decoded_url": "https://pub/one"}
        assert results[1]["status"] is False
        assert results[2] == {"status": True, "decoded_url": "https://pub/three"}
        assert results[3]["status"] is False

    def test_a_failed_post_fails_only_that_chunk(self):
        class Failing(ScriptedTransport):
            def __call__(self, request, *, timeout=None, proxy=None):
                if request.method == "POST":
                    raise TransportError("HTTP 429: Too Many Requests", status=429)
                return super().__call__(request, timeout=timeout, proxy=proxy)

        urls = [url_for(i) for i in range(1, 4)]
        results = drive(decode_batch_flow(urls), Failing(""))
        assert all(r["status"] is False for r in results)
        assert all("429" in r["message"] for r in results)

    def test_an_invalid_url_never_consumes_a_batch_slot(self):
        urls = [url_for(1), "https://evil.com/x/TOKEN", url_for(2)]
        transport = ScriptedTransport(batch_response([(2, "https://pub/two"), (1, "https://pub/one")]))

        results = drive(decode_batch_flow(urls), transport)

        assert results[0] == {"status": True, "decoded_url": "https://pub/one"}
        assert results[1]["status"] is False and "Invalid" in results[1]["message"]
        assert results[2] == {"status": True, "decoded_url": "https://pub/two"}


class TestChunking:
    def test_chunk_size_bounds_how_many_ride_in_one_post(self):
        urls = [url_for(i) for i in range(1, 8)]
        transport = ScriptedTransport(batch_response([(i, f"https://pub/{i}") for i in range(1, 4)]))

        drive(decode_batch_flow(urls, chunk_size=3), transport)

        posts = [r for r in transport.requests if r.method == "POST"]
        assert len(posts) == 3, "7 urls at chunk_size=3 is three posts"


class TestBatchRequestShape:
    def test_every_rpc_carries_its_position_tag(self):
        request = protocol.batch_decode_request([("T1", "S1", "1"), ("T2", "S2", "2")])
        from urllib.parse import parse_qs

        envelope = json.loads(parse_qs(request.body.decode())["f.req"][0])
        assert [rpc[3] for rpc in envelope[0]] == ["1", "2"]
        assert all(rpc[0] == "Fbv4je" for rpc in envelope[0])

    @pytest.mark.parametrize("body", ["", "garbage", ")]}'\n\n[]"], ids=["empty", "garbage", "no-frames"])
    def test_unparseable_responses_yield_nothing_rather_than_raising(self, body):
        assert protocol.parse_batch_decoded(body) == {}
