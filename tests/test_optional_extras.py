"""What happens when an optional extra is not installed.

The package advertises `pip install googlenewsdecoder[async]`, which means the un-extra'd case
is a supported state and needs to fail in a way that names the fix. It did not: `__init__`
guarded the async import with `except ImportError` and set `GoogleDecoderAsync = None`, but
httpx is imported lazily inside the transport, so the module import always succeeded, the
sentinel was never None, and the helpful message behind it was unreachable.
"""

import builtins
import sys

import pytest

import googlenewsdecoder as g
from googlenewsdecoder.transports import HttpxAsyncTransport


@pytest.fixture
def httpx_missing(monkeypatch):
    """Make `import httpx` fail the way a machine without the extra would."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "httpx" or name.startswith("httpx."):
            raise ModuleNotFoundError("No module named 'httpx'", name="httpx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "httpx", raising=False)


def test_the_package_imports_without_the_async_extra():
    # Not a tautology: on upstream main this raised ModuleNotFoundError, because __init__
    # imported the async decoder while setup.py never required httpx.
    assert g.decode is not None
    assert g.decode_batch is not None


def test_constructing_the_async_transport_says_which_extra_to_install(httpx_missing):
    with pytest.raises(ImportError, match=r"googlenewsdecoder\[async\]"):
        HttpxAsyncTransport()


async def _run(coro):
    return await coro


def test_decode_async_says_which_extra_to_install(httpx_missing):
    import asyncio

    with pytest.raises(ImportError, match=r"googlenewsdecoder\[async\]"):
        asyncio.run(_run(g.decode_async("https://news.google.com/rss/articles/TOKEN?oc=5")))


def test_a_supplied_async_transport_needs_no_httpx_at_all(httpx_missing):
    """The whole point of a swappable transport: bring your own client, skip the extra."""

    class Fake:
        async def __call__(self, request, *, timeout=None, proxy=None):
            raise AssertionError("not reached; construction is what is under test")

    decoder = g.GoogleDecoderAsync(transport=Fake())
    assert decoder.transport is not None
