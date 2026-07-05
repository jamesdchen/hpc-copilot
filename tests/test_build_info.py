"""Tests for the build fingerprint (``hpc_agent._build_info``).

The contract under test: code identity is content-keyed (a git sha travels
with the code), the ``--version`` string stays backward-parseable, and every
resolution path is fail-open — no git binary, no embedded sha, not a checkout
→ a plain version string, never an exception, never a hang.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import hpc_agent
import hpc_agent._build_info as bi

# ``<major>.<minor>.<patch>`` then an OPTIONAL PEP 440 local segment. The
# prefix up to ``+`` must parse as the plain version — this regex IS the
# backward-parseability pin for every consumer that reads --version today.
_FULL_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:\+[0-9A-Za-z][0-9A-Za-z.]*)?$")


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch):
    """Each test sees an unresolved dev-sha memo."""
    monkeypatch.setattr(bi, "_dev_sha_cache", [])


def test_full_version_is_base_version_plus_optional_local_segment() -> None:
    v = bi.full_version()
    assert v.split("+", 1)[0] == hpc_agent.__version__
    assert _FULL_VERSION_RE.match(v), v


def test_embedded_build_sha_wins_over_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bi, "BUILD_SHA", "abcd1234")
    monkeypatch.setattr(bi, "BUILD_DIRTY", False)

    def _no_git(*a: object, **k: object) -> None:  # pragma: no cover — must not be reached
        raise AssertionError("embedded sha must not shell out to git")

    monkeypatch.setattr(bi.subprocess, "run", _no_git)
    assert bi.runtime_sha() == "abcd1234"
    assert bi.build_fingerprint() == "gabcd1234"
    assert bi.full_version() == f"{hpc_agent.__version__}+gabcd1234"


def test_dirty_build_carries_dirty_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bi, "BUILD_SHA", "abcd1234")
    monkeypatch.setattr(bi, "BUILD_DIRTY", True)
    assert bi.build_fingerprint() == "gabcd1234.dirty"


def test_source_checkout_reports_dev_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bi, "_source_repo_root", lambda: tmp_path)
    monkeypatch.setattr(bi, "git_output", lambda *a, **k: "9154c3af")
    assert bi.runtime_sha() == "9154c3af"
    assert bi.build_fingerprint() == "dev.g9154c3af"
    assert bi.full_version() == f"{hpc_agent.__version__}+dev.g9154c3af"


def test_no_git_binary_fails_open_to_bare_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checkout without a working git → ``+dev``, no exception."""
    monkeypatch.setattr(bi, "_source_repo_root", lambda: tmp_path)

    def _missing(*a: object, **k: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(bi.subprocess, "run", _missing)
    assert bi.runtime_sha() is None
    assert bi.build_fingerprint() == "dev"
    assert bi.full_version() == f"{hpc_agent.__version__}+dev"


def test_git_timeout_fails_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bi, "_source_repo_root", lambda: tmp_path)

    def _hang(*a: object, **k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=2.0)

    monkeypatch.setattr(bi.subprocess, "run", _hang)
    assert bi.build_fingerprint() == "dev"


def test_wheel_without_build_info_reports_plain_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old wheel: no embedded sha, not a checkout → identity unknowable, bare version."""
    monkeypatch.setattr(bi, "_source_repo_root", lambda: None)
    assert bi.runtime_sha() is None
    assert bi.build_fingerprint() is None
    assert bi.full_version() == hpc_agent.__version__


def test_garbage_git_output_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bi, "_source_repo_root", lambda: tmp_path)
    monkeypatch.setattr(bi, "git_output", lambda *a, **k: "fatal: not a repo")
    assert bi.runtime_sha() is None
    assert bi.build_fingerprint() == "dev"


def test_git_output_fail_open_on_missing_cwd(tmp_path: Path) -> None:
    assert bi.git_output(["rev-parse", "HEAD"], cwd=tmp_path / "nope") is None
