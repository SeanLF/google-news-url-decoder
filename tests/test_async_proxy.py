"""A proxy that quietly does nothing is worse than one that refuses.

`HttpxAsyncTransport` used to forward a late proxy as `extensions={"proxy": ...}`. httpx accepts
that key and ignores it -- httpcore's pool reads the proxy it was constructed with and never
looks at request extensions -- so traffic went direct while the code read as though it were
proxied. For anyone using a proxy for egress control that is a silent failure, not a degraded
one.
"""


import pytest

from googlenewsdecoder.transports import HttpxAsyncTransport, TransportError

pytest.importorskip("httpx", reason="the async transport needs the [async] extra")

PROXY = "http://proxy.internal:8080"


class FakeRequest:
    method, url, headers, body = "GET", "https://news.google.com/articles/T", {}, None


def test_a_late_proxy_is_refused_rather_than_ignored():
    transport = HttpxAsyncTransport()  # built with no proxy

    async def go():
        return await transport(FakeRequest(), proxy=PROXY)

    import asyncio

    with pytest.raises(TransportError, match="cannot change proxy per request"):
        asyncio.run(go())


def test_the_error_names_the_call_that_would_work():
    transport = HttpxAsyncTransport()

    async def go():
        return await transport(FakeRequest(), proxy=PROXY)

    import asyncio

    with pytest.raises(TransportError) as excinfo:
        asyncio.run(go())
    assert "HttpxAsyncTransport(proxy=" in str(excinfo.value)
    assert PROXY in str(excinfo.value)


def test_the_proxy_it_was_built_with_is_not_refused():
    # The ordinary path: GoogleDecoderAsync(proxy=...) constructs the transport with the same
    # proxy and then passes it per request. That must not trip the guard.
    transport = HttpxAsyncTransport(proxy=PROXY)
    assert transport._proxy == PROXY

    async def go():
        return await transport(FakeRequest(), proxy=PROXY)

    import asyncio

    # It gets past the guard and fails on the network instead, which conftest blocks.
    with pytest.raises(TransportError) as excinfo:
        asyncio.run(go())
    assert "cannot change proxy per request" not in str(excinfo.value)
