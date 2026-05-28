"""Regression tests for the sidecar-vs-package version-mismatch warning path.

The reader at :func:`hpc_agent.state.runs.read_run_sidecar` does a defensive
``from hpc_agent import __version__`` so a (theoretical) circular import
during eager package init can't break the read. The fallback assigns
``_pkg_version = None`` — the surrounding ``if _pkg_version and …`` then
short-circuits and emits no warning. These tests pin both paths.
"""

from __future__ import annotations

import sys
import warnings
from typing import TYPE_CHECKING

import pytest

from hpc_agent.state import runs as runs_mod
from hpc_agent.state.runs import (
    read_run_sidecar,
    write_run_sidecar,
)

if TYPE_CHECKING:
    from pathlib import Path


def _common_kwargs(run_id: str = "20260101-000000-deadbee") -> dict:
    return dict(
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",  # ← writer was an older package version
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=4,
        tasks_py_sha="1" * 64,
    )


def test_version_mismatch_warning_fires_when_import_succeeds(tmp_path: Path) -> None:
    """Happy path: package import works, mismatch warning fires once."""
    runs_mod._warned_version_mismatch.clear()
    write_run_sidecar(tmp_path, **_common_kwargs())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        read_run_sidecar(tmp_path, _common_kwargs()["run_id"])
    msgs = [str(w.message) for w in caught]
    assert any("0.2.0" in m and "reader is" in m for m in msgs), msgs


def test_version_mismatch_silent_when_pkg_import_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback path: ``from hpc_agent import __version__`` raises.

    The except-branch sets ``_pkg_version = None`` (typed as ``str | None``).
    The subsequent guard ``if _pkg_version and …`` must short-circuit so
    no spurious warning fires when the reader can't determine its own
    version.
    """
    runs_mod._warned_version_mismatch.clear()
    write_run_sidecar(tmp_path, **_common_kwargs())

    # Force the deferred import inside read_run_sidecar to raise. The
    # function does ``from hpc_agent import __version__``; deleting the
    # attribute makes the import raise ImportError on lookup.
    pkg = sys.modules["hpc_agent"]
    original = pkg.__version__
    monkeypatch.delattr(pkg, "__version__")
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            out = read_run_sidecar(tmp_path, _common_kwargs()["run_id"])
    finally:
        pkg.__version__ = original

    # The read still returns sensible data — fallback never fails the read.
    assert out["hpc_agent_version"] == "0.2.0"
    # No version-mismatch warning (the guard correctly short-circuited
    # on _pkg_version=None).
    mismatch_msgs = [str(w.message) for w in caught if "reader is" in str(w.message)]
    assert mismatch_msgs == [], mismatch_msgs
