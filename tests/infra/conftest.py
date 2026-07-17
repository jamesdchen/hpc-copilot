"""Shared fixtures for ``tests/infra/``.

Defensive pre-warm of the lazy ``ssh -V`` version probe cache before every
test so ``subprocess.run`` mocks don't accidentally capture it.

**Background.**
:func:`hpc_agent.infra.ssh_options._local_openssh_supports_gcm` is
``functools.cache``-protected and lazily calls
:func:`hpc_agent.infra.ssh_options._local_openssh_major`, which fires
``subprocess.run(["ssh", "-V"])`` once per process. Several tests in this
directory mock ``subprocess.run`` via patches like
``patch("hpc_agent.infra.transport.subprocess.run")`` or
``patch("hpc_agent.infra.remote.subprocess.run")``. Those patches land on
the global ``subprocess`` module (every module's ``subprocess.run``
reference resolves to the same callable), so a cold-cache version probe
that fires inside the ``with patch(...):`` block ends up in the mock's
call list — bumping assertions like ``assert mock_run.call_count == 1``
or shifting ``mock_run.call_args[0][0]`` away from the expected argv.

**Why an autouse fixture instead of per-test filtering.**
``test_remote_rsync_fallback.py`` carries a per-call
``_is_ssh_version_probe`` filter at the helper boundary (and we keep it
as a per-call defensive layer), but ``test_remote.py`` has ~25 separate
``call_args`` / ``call_count`` assertions across many tests that would
each need the filter. A directory-level autouse warm-up fixture is
simpler and immune to drift: any new test added in this directory
inherits the protection automatically.

**Cache interaction with ``test_remote_windows_compat.py``.**
That file has its own file-level autouse fixture that ``cache_clear()``s
``_windows_openssh_named_pipe_supported`` between tests as part of
probe-state tests. Pytest applies the file-level fixture AFTER this
directory-level one, so:

1. This conftest's autouse warms the probe caches.
2. The file-level fixture (in tests where it applies) clears the
   named-pipe cache (it does NOT clear the GCM cache — only the
   named-pipe + ssh-config caches).
3. Test runs with the intended probe state for that test.

**Why BOTH probes must be warmed (the 661a6ca7 CI double-red).** Warming
only the GCM cache left ``_windows_openssh_named_pipe_supported`` — its
own independent ``functools.cache`` over the same ``ssh -V`` probe — cold
whenever ``test_remote_windows_compat.py``'s fixture had cleared it
earlier in the SAME xdist worker. The next test in that worker to enter a
``patch("...subprocess.Popen")`` window and call ``ssh_argv("ssh")``
(``test_transport_pull.py::test_pull_transfer_drives_bounded_runner_not_ssh_run``)
then fired the real probe against the mocked ``Popen``:
``MagicMock.communicate()`` iterates empty, so ``subprocess.run``'s
``stdout, stderr = process.communicate(...)`` raised ``ValueError: not
enough values to unpack (expected 2, got 0)`` — Windows-only (only win32
multiplex opts consult the named-pipe probe) and xdist-order-dependent.
Warming both here means every test starts with every ssh-probe cache
warm, whatever ran before it in the worker.
"""

from __future__ import annotations

import sys

import pytest

from hpc_agent.infra import ssh_options


@pytest.fixture(autouse=True)
def _warm_ssh_version_probe_cache() -> None:
    """Pre-warm BOTH cached ``ssh -V`` probes before each test.

    Idempotent on a warm cache (``functools.cache`` returns the cached
    value without re-firing the probe). On a cold cache this fires
    ``subprocess.run(["ssh", "-V"])`` against the REAL subprocess module
    *before* any test's mocking begins, so the probe never lands inside
    a ``patch("...subprocess.run")`` / ``patch("...subprocess.Popen")``
    scope. The named-pipe probe is win32-gated in product code, so warm
    it only there (on POSIX it is never consulted).
    """
    ssh_options._local_openssh_supports_gcm()
    if sys.platform == "win32":
        ssh_options._windows_openssh_named_pipe_supported()
