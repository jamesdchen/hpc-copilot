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

import subprocess
import sys
from typing import Any

__all__ = ["run_cli"]


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
        env=env,
        **kwargs,
    )
