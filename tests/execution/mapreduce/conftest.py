"""Shared fixtures for ``tests/execution/mapreduce/``.

Defensive pre-warm of the lazy ``ssh -V`` version probe cache before every
test — the same discipline ``tests/infra/conftest.py`` documents at length.

**Why this directory needs it.** The TUI log-open tests
(``test_tui_module.py::TestOpenLog``) mock the subprocess seam
(``patch("...tui.subprocess.Popen")``) and, inside that window, call
``ssh_argv("ssh")`` to build the expected argv. On Windows ``ssh_argv`` may
fire the lazy ``ssh -V`` probe (``_local_openssh_supports_gcm`` /
``_windows_openssh_named_pipe_supported``, both ``functools.cache`` over
``subprocess.run(["ssh", "-V"])``). Even though those tests patch ``Popen``
(not ``run``), warming BOTH probes up front keeps this directory immune to a
future test that patches ``subprocess.run`` and would otherwise capture the
cold-cache probe in its mock's call list. An autouse directory-level warm-up
is drift-proof: any new test added here inherits the protection. See
``tests/infra/conftest.py`` for the full root-cause narrative.
"""

from __future__ import annotations

import sys

import pytest

from hpc_agent.infra import ssh_options


@pytest.fixture(autouse=True)
def _warm_ssh_version_probe_cache() -> None:
    """Pre-warm BOTH cached ``ssh -V`` probes before each test.

    Idempotent on a warm cache. On a cold cache this fires the real
    ``subprocess.run(["ssh", "-V"])`` BEFORE any test's mocking begins, so the
    probe never lands inside a ``patch("...subprocess.*")`` scope. The
    named-pipe probe is win32-gated in product code, so warm it only there.
    """
    ssh_options._local_openssh_supports_gcm()
    if sys.platform == "win32":
        ssh_options._windows_openssh_named_pipe_supported()
