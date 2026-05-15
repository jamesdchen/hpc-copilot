"""Tests for the ``hpc-agent discover --search-dirs`` CLI flag.

The Python API (`claude_hpc.state.discover.discover_executors`) has long
accepted a `search_dirs` override. This file exercises the matching CLI
flag so integrators that want a tighter scan (e.g. modules-only `src/`)
can stay on a pure CLI call without importing the Python package.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_EXEC_SRC = (
    "import argparse\n"
    "def main():\n"
    "    argparse.ArgumentParser().parse_args()\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
)


def _write_executor(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_EXEC_SRC, encoding="utf-8")


def _run_discover(experiment_dir: Path, *extra: str) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "claude_hpc",
            "discover",
            "--experiment-dir",
            str(experiment_dir),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert len(lines) == 1
    env = json.loads(lines[0])
    assert env["ok"] is True
    return env["data"]


def test_default_scans_executors_scripts_src(tmp_path: Path) -> None:
    """Without --search-dirs the scanner walks every default candidate."""
    _write_executor(tmp_path / "scripts" / "a.py")
    _write_executor(tmp_path / "src" / "b.py")
    _write_executor(tmp_path / "executors" / "c.py")
    data = _run_discover(tmp_path)
    names = sorted(e["name"] for e in data["executors"])
    assert names == ["a", "b", "c"]


def test_search_dirs_narrows_to_scripts(tmp_path: Path) -> None:
    """Passing --search-dirs scripts excludes src/ executors."""
    _write_executor(tmp_path / "scripts" / "wanted.py")
    _write_executor(tmp_path / "src" / "modules_only.py")
    data = _run_discover(tmp_path, "--search-dirs", "scripts")
    names = sorted(e["name"] for e in data["executors"])
    assert names == ["wanted"]


def test_search_dirs_accepts_multiple_comma_separated(tmp_path: Path) -> None:
    _write_executor(tmp_path / "scripts" / "a.py")
    _write_executor(tmp_path / "executors" / "b.py")
    _write_executor(tmp_path / "src" / "skip.py")
    data = _run_discover(tmp_path, "--search-dirs", "scripts,executors")
    names = sorted(e["name"] for e in data["executors"])
    assert names == ["a", "b"]


def test_search_dirs_strips_whitespace_and_empty_entries(tmp_path: Path) -> None:
    """Trailing commas / spaces shouldn't add a phantom search dir."""
    _write_executor(tmp_path / "scripts" / "a.py")
    _write_executor(tmp_path / "src" / "skip.py")
    data = _run_discover(tmp_path, "--search-dirs", " scripts , ,")
    names = sorted(e["name"] for e in data["executors"])
    assert names == ["a"]


def test_search_dirs_nonexistent_returns_empty(tmp_path: Path) -> None:
    """Passing a directory that doesn't exist yields zero executors."""
    _write_executor(tmp_path / "scripts" / "ignored.py")
    data = _run_discover(tmp_path, "--search-dirs", "no_such_dir")
    assert data["executors"] == []
