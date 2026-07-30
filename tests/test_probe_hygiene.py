"""The probes are not shipped, but they are how every measured claim in this repo was made.

Nothing here runs a probe -- they all make live requests. These check the properties that decide
whether a probe measures what it says it does, and that are invisible when it appears to work.
"""

import sys
from pathlib import Path

import pytest

PROBES = Path(__file__).parent.parent / "probes"
PROBE_FILES = sorted(p for p in PROBES.glob("*.py") if not p.name.startswith("_"))


def test_there_are_probes_to_check():
    """Guards the glob: a rename or a move must not turn this file into a no-op that passes."""
    assert len(PROBE_FILES) > 5, [p.name for p in PROBE_FILES]


@pytest.mark.parametrize("probe", PROBE_FILES, ids=lambda p: p.stem)
def test_a_probe_does_not_shadow_a_standard_library_module(probe):
    """A probe named after a stdlib module gets imported and RUN as a side effect.

    Every probe puts its own directory at the front of `sys.path` so it can import `_common`,
    which also makes `probes/<name>.py` win over the standard library's `<name>`. A probe called
    `locale.py` was therefore imported by something inside urllib3's own import chain, executing
    it once from a half-initialised module before the interpreter ran it properly: two result
    rows per invocation, the first one produced in a state nobody designed.

    It emitted a plausible row both times, which is why this is a test and not a comment.
    """
    assert probe.stem not in sys.stdlib_module_names, (
        f"probes/{probe.name} shadows the standard library's {probe.stem} module, so importing "
        f"anything that imports {probe.stem} will execute this probe"
    )


@pytest.mark.parametrize("probe", PROBE_FILES, ids=lambda p: p.stem)
def test_a_probe_reports_through_emit(probe):
    """Rows have to carry the labels that say which arm produced them.

    `_common.emit` adds exit, ip, vpn and dns to every row. A probe that builds its own JSON
    silently omits them, and a comparison across tunnels or resolvers then cannot attribute its
    own rows -- which is exactly what happened to the runner's failure paths.
    """
    source = probe.read_text()
    if "json.dumps" not in source:
        return
    assert "decode_with" in probe.name, (
        f"probes/{probe.name} builds JSON directly; use _common.emit so the row carries the "
        "labels identifying the arm. decode_with is the documented exception: it writes a "
        "different row shape to stdout, not to a results file."
    )
