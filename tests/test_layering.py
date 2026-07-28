"""The layering, asserted at runtime.

`.importlinter` checks the import graph statically and is the real enforcement. These cover the
part a graph cannot: that the promise the layering exists to make is actually keepable, and that
moving `TransportError` did not break anyone importing it from where it used to live.
"""

import subprocess
import sys

import pytest

from googlenewsdecoder import TransportError, decode_flow, drive
from googlenewsdecoder.errors import TransportError as FromErrors


class TestTheErrorTypeIsWhereEverybodyExpects:
    def test_the_package_root_still_exports_it(self):
        assert TransportError is FromErrors

    def test_transports_still_exports_it(self):
        # It lived here for the whole 0.1.x line. Moving the definition must not break an
        # import that has always worked.
        from googlenewsdecoder.transports import TransportError as FromTransports

        assert FromTransports is FromErrors

    def test_it_still_carries_the_status(self):
        assert TransportError("boom", status=429).status == 429
        assert TransportError("boom").status is None


class TestTheAlgorithmCanRunWithoutThePackagesTransports:
    """The claim the three-layer shape rests on: bring your own I/O and never touch
    `transports`. It was almost true -- `flow` imported `transports` just to name the exception
    a caller was required to raise, so replacing `transports` meant importing it anyway."""

    def test_a_full_decode_runs_against_a_hand_written_driver(self):
        # No transport object, no `transports` import: just the generator and a dict of canned
        # responses, which is the entire point of the sans-I/O shape.
        import json

        token_url = "https://news.google.com/rss/articles/TOKEN?oc=5"
        frame = ["wrb.fr", "Fbv4je", json.dumps(["garturlres", "https://publisher.example/x"]), None, None, None, "1"]
        canned = ")]}'\n\n" + json.dumps([frame, ["di", 1], ["af.httprm", 1]])

        flow = decode_flow(token_url)
        request = next(flow)
        try:
            while True:
                body = canned if "batchexecute" in request.url else '<div data-n-a-sg="S" data-n-a-ts="1">x</div>'
                request = flow.send(body)
        except StopIteration as stop:
            result = stop.value

        assert result == {"status": True, "decoded_url": "https://publisher.example/x"}

    def test_a_caller_raises_the_error_type_from_a_leaf_module(self):
        # `errors` imports nothing else in the package, so depending on it does not drag in a
        # transport -- which is what made the old arrangement self-defeating.
        def failing_transport(request, *, timeout=None, proxy=None):
            raise FromErrors("connection refused")

        result = drive(decode_flow("https://news.google.com/rss/articles/T?oc=5"), failing_transport)
        assert result["status"] is False
        assert "connection refused" in result["message"]


@pytest.mark.parametrize("module", ["errors", "limits", "protocol", "flow"])
def test_the_pure_core_imports_without_any_http_client(module):
    """A subprocess with `requests`, `httpx` and `aiohttp` blocked at import time.

    Guards the property `protocol` claims in its docstring. Doing it in-process would prove
    nothing: the modules are already imported by the time a test runs.
    """
    # `find_spec`, not `find_module`. The latter was removed from the import system in 3.12,
    # so a blocker using it blocks nothing and this test passes without testing anything --
    # which is what the first draft of it did.
    blocker = (
        "import sys\n"
        "BLOCKED = {'requests', 'httpx', 'aiohttp'}\n"
        "class Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in BLOCKED:\n"
        "            raise ImportError(f'{name} is blocked for this test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
    )
    # Prove the blocker works in the same interpreter that runs the real check, so this can
    # never silently degrade into asserting nothing.
    self_check = subprocess.run(
        [sys.executable, "-c", blocker + "import requests\nprint('NOT BLOCKED')\n"],
        capture_output=True,
        text=True,
    )
    assert self_check.returncode != 0, "the blocker did not block; this test proves nothing"

    proc = subprocess.run(
        [sys.executable, "-c", blocker + f"import googlenewsdecoder.{module}\nprint('ok')\n"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
