"""Make "these tests do not touch the network" a guarantee rather than a promise.

Every test here substitutes the transport, so nothing should open a socket. That was true when
each test was written and is the kind of property that decays quietly: one test forgets to pass
a transport, starts hitting Google, and passes anyway -- until it fails in CI for reasons that
have nothing to do with the change under review, or worse, keeps passing while asserting
against whatever Google happens to serve that day.

So the sockets are taken away. A test that tries to reach the network fails naming itself.
"""

import os
import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

# Which copy of the package do the tests import? Both answers are legitimate and they must be
# chosen explicitly, because getting it wrong is silent.
#
# Default: the checkout, so a developer's edits are what runs.
#
# TEST_INSTALLED_PACKAGE=1: whatever pip installed, so CI can prove the built artifact works
# -- a missing entry in `packages` fails there instead of on someone's `pip install`.
#
# An earlier version keyed this off `find_spec(...) is None`, i.e. "add the checkout only if
# the package is not importable". That answers "is it installed anywhere", never "is this the
# tree I am editing". With the package installed, sabotaging the source tree still gave 134
# green tests: 22 of them never saw the change at all. Worse, `pytest` and `python -m pytest`
# disagreed, because only the latter puts the working directory on the path -- so the same
# code passed or failed depending on how you invoked it.
TEST_INSTALLED = os.environ.get("TEST_INSTALLED_PACKAGE") == "1"
if not TEST_INSTALLED:
    sys.path.insert(0, str(REPO_ROOT))


def pytest_report_header(config):
    """Say which copy is under test, on every run. This is the fact that was invisible."""
    import googlenewsdecoder

    where = Path(googlenewsdecoder.__file__).resolve().parent
    kind = "installed" if REPO_ROOT not in where.parents else "checkout"
    return f"googlenewsdecoder under test: {kind} at {where}"


def pytest_configure(config):
    """Fail loudly if the mode asked for is not the mode obtained.

    Without this the header above is just a note nobody reads; with it, a run that imports the
    wrong copy cannot report success.
    """
    import googlenewsdecoder

    where = Path(googlenewsdecoder.__file__).resolve().parent
    from_checkout = REPO_ROOT in where.parents
    if TEST_INSTALLED and from_checkout:
        raise pytest.UsageError(
            f"TEST_INSTALLED_PACKAGE=1 but the checkout was imported ({where}). "
            "Run pytest from outside the repo root, or unset the variable."
        )
    if not TEST_INSTALLED and not from_checkout:
        raise pytest.UsageError(
            f"expected to test the checkout at {REPO_ROOT} but imported {where}. "
            "Set TEST_INSTALLED_PACKAGE=1 if that was intended."
        )

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
