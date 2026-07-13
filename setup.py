"""Wheel-build hook: embed the git commit fingerprint into the package.

All project metadata lives in ``pyproject.toml``; this file exists ONLY
to give ``setuptools.build_meta`` a ``build_py`` subclass that rewrites
the ``BUILD_SHA`` / ``BUILD_DIRTY`` placeholders in
``hpc_agent/_build_info.py`` — in the *build tree*, never the checkout —
with the commit sha (+ dirty flag) the wheel was built from. See that
module's docstring for the incident this solves (two installs both
reporting "0.10.65" while diverging by days of commits).

Fail-open by contract: no ``git`` binary, an unpacked-sdist build with
no ``.git``, a timeout, or an unexpected placeholder shape all leave the
placeholder file as-is (the wheel then reports the bare version, exactly
like a pre-fingerprint build). The hook must never break a build.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

_HERE = Path(__file__).resolve().parent
# Build-time budget, generous vs the 2 s runtime one — a wedged git here
# only slows one build, and CI checkouts can be cold.
_GIT_TIMEOUT_SECONDS = 15.0

_SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _git(*args: str) -> str | None:
    """``git <args>`` at the repo root; stripped stdout or ``None`` on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(_HERE), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


class BuildPyWithBuildInfo(build_py):
    """``build_py`` that stamps the git fingerprint into the build tree."""

    def run(self) -> None:  # noqa: D102 — setuptools contract
        super().run()
        self._stamp_build_info()

    def _stamp_build_info(self) -> None:
        sha = _git("rev-parse", "--short=8", "HEAD")
        if not sha or not _SHORT_SHA_RE.match(sha):
            return  # not a checkout (sdist unpack) / no git — ship the placeholder
        # None here means the dirty *query* failed while HEAD resolved;
        # claim clean rather than dropping the sha (fail-open, sha is the
        # load-bearing part).
        porcelain = _git("status", "--porcelain")
        dirty = bool(porcelain)

        target = Path(self.build_lib) / "hpc_agent" / "_build_info.py"
        if not target.is_file():
            return
        source = target.read_text(encoding="utf-8")
        # Match the pristine placeholder OR an already-stamped value:
        # setuptools' build_py copies by TIMESTAMP, so a stale build/lib can
        # hand this hook a file stamped by a PREVIOUS build. The old
        # placeholder-only regex silently kept that stale sha (every wheel
        # built in this checkout between d712e69a and 2026-07-07 was
        # mislabeled). Re-stamping an already-stamped line makes the hook
        # idempotent on the CURRENT HEAD regardless of build-tree staleness.
        stamped, n_sha = re.subn(
            r'^BUILD_SHA: str \| None = (?:None|"[0-9a-f]{7,40}")$',
            f'BUILD_SHA: str | None = "{sha}"',
            source,
            count=1,
            flags=re.MULTILINE,
        )
        stamped, n_dirty = re.subn(
            r"^BUILD_DIRTY: bool = (?:False|True)$",
            f"BUILD_DIRTY: bool = {dirty}",
            stamped,
            count=1,
            flags=re.MULTILINE,
        )
        if n_sha != 1 or n_dirty != 1:
            return  # placeholder shape drifted — ship as-is rather than corrupt
        target.write_text(stamped, encoding="utf-8")


setup(cmdclass={"build_py": BuildPyWithBuildInfo})
