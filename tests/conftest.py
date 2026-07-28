"""Make "these tests do not touch the network" a guarantee rather than a promise.

Every test here substitutes the transport, so nothing should open a socket. That was true when
each test was written and is the kind of property that decays quietly: one test forgets to pass
a transport, starts hitting Google, and passes anyway -- until it fails in CI for reasons that
have nothing to do with the change under review, or worse, keeps passing while asserting
against whatever Google happens to serve that day.

So the sockets are taken away. A test that tries to reach the network fails naming itself.
"""

import importlib.util
import socket
import sys
from pathlib import Path

import pytest

# Make the checkout importable, but ONLY when the package is not installed. Every test file
# used to do this unconditionally, which put the repo root ahead of site-packages and meant
# the CI job that installs the built artifact was still testing the source tree beside it --
# so a missing entry in `packages` would have passed the job written to catch it.
if importlib.util.find_spec("googlenewsdecoder") is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection


def _is_local(address):
    """Loopback stays allowed -- pytest plugins and debuggers legitimately use it."""
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX and friends: not the internet
    host = address[0]
    return host in ("127.0.0.1", "::1", "localhost", "", None)


class NetworkAccessAttempted(RuntimeError):
    pass


def _blocked(address):
    return NetworkAccessAttempted(
        f"a test tried to open a network connection to {address!r}. "
        "Tests in this suite substitute the transport -- pass a fake instead of reaching out."
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def connect(self, address):
        if _is_local(address):
            return _real_connect(self, address)
        raise _blocked(address)

    def connect_ex(self, address):
        if _is_local(address):
            return _real_connect_ex(self, address)
        raise _blocked(address)

    def create_connection(address, *args, **kwargs):
        if _is_local(address):
            return _real_create_connection(address, *args, **kwargs)
        raise _blocked(address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
