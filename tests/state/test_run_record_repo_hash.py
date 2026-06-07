"""Tests for ``repo_hash`` path-form-invariance (#296).

The same logical experiment directory expressed under Windows
backslash, Bash MINGW (``/c/...``), or WSL (``/mnt/c/...``) MUST hash to
the same namespace. Without this guarantee, a submit issued from Git
Bash writes the journal under one namespace and a reconcile call from
the native Windows session reads from a different one — they silently
miss each other and the run looks corrupt locally even though the
cluster sidecar is fine.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hpc_agent.state.run_record import repo_hash

# ─── Windows-only: the four path forms must hash to one value ──────────────


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path forms")
class TestWindowsPathFormInvariance:
    """All representations of the same logical Windows dir hash identically."""

    def test_windows_form_is_canonical(self) -> None:
        """The native backslash form is the canonical reference."""
        assert repo_hash(Path(r"C:\Users\james\demo-hpc"))
        # Sanity: deterministic
        assert repo_hash(Path(r"C:\Users\james\demo-hpc")) == repo_hash(
            Path(r"C:\Users\james\demo-hpc")
        )

    def test_bash_mingw_form_matches_windows_form(self) -> None:
        win = repo_hash(Path(r"C:\Users\james\demo-hpc"))
        bash = repo_hash(Path("/c/Users/james/demo-hpc"))
        assert win == bash, (
            f"Bash MINGW '/c/Users/james/demo-hpc' must hash to the same "
            f"namespace as Windows 'C:\\Users\\james\\demo-hpc' (#296). "
            f"Got: win={win!r} bash={bash!r}"
        )

    def test_wsl_form_matches_windows_form(self) -> None:
        win = repo_hash(Path(r"C:\Users\james\demo-hpc"))
        wsl = repo_hash(Path("/mnt/c/Users/james/demo-hpc"))
        assert win == wsl, (
            f"WSL '/mnt/c/Users/james/demo-hpc' must hash to the same "
            f"namespace as Windows 'C:\\Users\\james\\demo-hpc' (#296). "
            f"Got: win={win!r} wsl={wsl!r}"
        )

    def test_all_three_forms_agree(self) -> None:
        win = repo_hash(Path(r"C:\Users\james\demo-hpc"))
        bash = repo_hash(Path("/c/Users/james/demo-hpc"))
        wsl = repo_hash(Path("/mnt/c/Users/james/demo-hpc"))
        assert win == bash == wsl

    def test_drive_letter_case_does_not_affect_translation(self) -> None:
        """Translator uppercases the drive letter so /C/... and /c/... agree."""
        upper = repo_hash(Path("/C/Users/james/demo-hpc"))
        lower = repo_hash(Path("/c/Users/james/demo-hpc"))
        assert upper == lower

    def test_distinct_drives_hash_distinctly(self) -> None:
        """Different drives are different logical locations — hashes diverge."""
        c_drive = repo_hash(Path(r"C:\Users\james\demo-hpc"))
        d_drive = repo_hash(Path(r"D:\Users\james\demo-hpc"))
        assert c_drive != d_drive

    def test_distinct_paths_under_same_drive_hash_distinctly(self) -> None:
        a = repo_hash(Path(r"C:\Users\james\demo-hpc"))
        b = repo_hash(Path(r"C:\Users\james\other-exp"))
        assert a != b

    def test_cluster_form_hashes_distinctly(self) -> None:
        """The cluster path ``/u/scratch/...`` is a remote location — its hash
        must NOT match any local form. Translator only fires on drive-letter
        patterns it can canonicalize."""
        local = repo_hash(Path(r"C:\Users\james\demo-hpc"))
        cluster = repo_hash(Path("/u/scratch/j/jamesdc1/demo-hpc"))
        assert local != cluster


# ─── Non-Windows: behavior is unchanged ────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="Posix-only sanity")
def test_posix_unchanged_resolves_paths_normally(tmp_path: Path) -> None:
    """On Linux / macOS, the canonicalizer is a no-op beyond ``resolve()``."""
    real = tmp_path / "demo"
    real.mkdir()
    sym = tmp_path / "link"
    sym.symlink_to(real)
    # Path.resolve() follows the symlink → both forms hash identically.
    assert repo_hash(real) == repo_hash(sym)


# ─── Determinism / regression guards ───────────────────────────────────────


def test_repeated_calls_are_deterministic(tmp_path: Path) -> None:
    target = tmp_path / "exp"
    target.mkdir()
    h1 = repo_hash(target)
    h2 = repo_hash(target)
    assert h1 == h2
    assert len(h1) == 12
    # Lowercase hex characters only.
    assert all(c in "0123456789abcdef" for c in h1)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only regression value")
def test_demo_hpc_hash_is_stable() -> None:
    """The canonical Windows form for the dev's demo-hpc dir hashes to the
    same value the framework has used since before #296 — so existing
    ``~/.claude/hpc/bc64a2106672/`` namespace dirs continue to work."""
    assert repo_hash(Path(r"C:\Users\james\demo-hpc")) == "bc64a2106672"
