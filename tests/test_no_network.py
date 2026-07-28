"""The network guard in conftest, tested.

A guard that never fires looks exactly like no guard at all, so these assert that it does fire
-- and that the default transport, the one a user actually gets, is what it catches.
"""

import socket

import pytest
from conftest import NetworkAccessAttempted

from googlenewsdecoder import decode
from googlenewsdecoder.transports import RequestsTransport


def test_a_direct_socket_to_the_internet_is_refused():
    with pytest.raises(NetworkAccessAttempted):
        socket.create_connection(("news.google.com", 443), timeout=1)


def test_loopback_is_still_allowed():
    # Left open deliberately: pytest plugins and debuggers use it, and blocking it would make
    # the guard the reason a test fails rather than the thing it is guarding against.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        with socket.create_connection(listener.getsockname(), timeout=1):
            pass
    finally:
        listener.close()


def test_the_real_default_transport_would_be_caught():
    # Not a hypothetical: this is the path a test gets by forgetting to pass `transport=`.
    result = decode("https://news.google.com/rss/articles/TOKEN?oc=5", transport=RequestsTransport())
    assert result["status"] is False, "a blocked request must surface as a failed decode"
