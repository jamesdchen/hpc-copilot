"""Canonical CLI subprocess runner for tests.

Every test that shells out to ``python -m hpc_agent ...`` should funnel
through :func:`run_cli` rather than calling :func:`subprocess.run`
directly. The wrapper always passes ``timeout=`` (default 30s) so a
hanging production code path cannot pin CI indefinitely — a regression
that snuck in twice before this helper landed.

This module is the only file (besides the migration grandfathered set)
that may call :func:`subprocess.run` without a ``timeout=`` kwarg per
:mod:`tests.contracts.test_subprocess_timeout_discipline`.

Usage
-----

.. code-block:: python

    from tests._subprocess import run_cli

    proc = run_cli("capabilities", env={...}, timeout=10)
    assert proc.returncode == 0

The signature mirrors :func:`subprocess.run`'s keyword arguments so a
caller can pass ``cwd``, ``input``, ``env``, etc. without surprises.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

__all__ = ["run_cli"]

# Coverage env vars forwarded into spawned children so subprocess coverage
# (``[tool.coverage.run] parallel`` + the ``process_startup`` .pth installed by
# ``scripts/enable_subprocess_coverage.py``) records the child's lines. When a
# caller passes an explicit ``env=`` dict it replaces the parent environment
# wholesale, which would drop ``COVERAGE_PROCESS_START`` and silently leave the
# child uncounted; re-inject it. Inert outside a coverage run — the vars are
# simply unset, and the app ignores them regardless.
_COVERAGE_ENV_VARS = ("COVERAGE_PROCESS_START", "COVERAGE_FILE")


def _forward_coverage_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """Copy coverage env vars from the parent into an explicit child *env*.

    Returns *env* unchanged when it is ``None`` (the child inherits the parent
    environment, coverage vars included) or when no coverage var is set.
    """
    if env is None:
        return None
    forwarded = {k: os.environ[k] for k in _COVERAGE_ENV_VARS if k in os.environ}
    if not forwarded:
        return env
    return {**env, **forwarded}


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float = 30,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m hpc_agent <args>`` and return the completed process.

    *timeout* defaults to 30 seconds so a hanging production code path
    cannot pin CI. Tests that need a longer budget pass an explicit
    *timeout*; tests that need NO timeout (none in tree today) must
    motivate it inline and add themselves to the grandfathered set in
    :mod:`tests.contracts.test_subprocess_timeout_discipline`.

    Extra ``**kwargs`` are forwarded to :func:`subprocess.run` (e.g.
    ``cwd``, ``input``). ``capture_output=True``, ``text=True``,
    ``encoding="utf-8"``, and ``check=False`` are always set.
    """
    return subprocess.run(  # noqa: S603  # trusted invocation: sys.executable + literal args
        [sys.executable, "-m", "hpc_agent", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=timeout,
        env=_forward_coverage_env(env),
        **kwargs,
    )
