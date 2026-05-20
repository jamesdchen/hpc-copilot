"""Tests for ``claude_hpc.atoms.preflight.check_preflight``.

Focus on the file-transfer capability check: a missing ``rsync`` must
NOT fail preflight when the ``scp``+``tar`` fallback transport is
available — the path Windows hosts without WSL/MSYS rsync take, where
``infra.remote`` uses a ``tar c | ssh tar x`` push / ``scp -r`` pull
pipeline instead.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest import mock

from claude_hpc.atoms import preflight


def _which_for(present: set[str]) -> Callable[[str], str | None]:
    """Return a ``shutil.which`` stub that resolves only *present* binaries."""

    def _which(binary: str) -> str | None:
        return f"/usr/bin/{binary}" if binary in present else None

    return _which


def _checks_by_name(present: set[str]) -> dict[str, dict]:
    """Run check_preflight with only *present* binaries on PATH; index by name."""
    with mock.patch.object(preflight.shutil, "which", _which_for(present)):
        result = preflight.check_preflight()
    return {c["name"]: c for c in result["checks"]}


def test_rsync_present_satisfies_file_transfer() -> None:
    checks = _checks_by_name({"ssh", "rsync", "scp", "tar"})
    assert checks["file_transfer_on_path"]["ok"] is True
    assert "rsync" in checks["file_transfer_on_path"]["detail"]


def test_rsync_absent_scp_tar_fallback_satisfies_file_transfer() -> None:
    """The Windows path: no rsync, but scp+tar cover the transport."""
    checks = _checks_by_name({"ssh", "scp", "tar"})
    assert checks["file_transfer_on_path"]["ok"] is True
    assert "fallback" in checks["file_transfer_on_path"]["detail"]


def test_partial_fallback_fails_file_transfer() -> None:
    """scp without tar (or vice versa) is not a usable fallback."""
    checks = _checks_by_name({"ssh", "scp"})
    ft = checks["file_transfer_on_path"]
    assert ft["ok"] is False
    assert "tar" in ft["detail"]


def test_no_transport_fails_file_transfer() -> None:
    checks = _checks_by_name({"ssh"})
    ft = checks["file_transfer_on_path"]
    assert ft["ok"] is False
    assert "rsync" in ft["detail"]
    assert "scp" in ft["detail"]
    assert "tar" in ft["detail"]


def test_ssh_check_named_ssh_on_path_and_no_legacy_rsync_check() -> None:
    """ssh keeps its own check; the old rsync_on_path check is gone."""
    checks = _checks_by_name({"ssh", "rsync"})
    assert checks["ssh_on_path"]["ok"] is True
    assert "rsync_on_path" not in checks
