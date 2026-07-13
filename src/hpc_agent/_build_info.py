"""Content-keyed code identity: which commit is this code, really?

The version *number* alone cannot express skew between installs — the
incident that earned this module had an installed ``uv tool`` wheel and
the repo tip both reporting ``0.10.65`` while diverging by days of
commits (a stale probe ran all night; ``wait-detached`` was missing from
the installed CLI). The fix is a build fingerprint: the git commit sha
travels WITH the code, so two installs of the "same version" are
distinguishable.

Three identity sources, in precedence order (all fail-open — no git, no
sha, not a repo → the plain version number, never an error, never a hang):

1. **Wheel-embedded** — ``setup.py``'s ``build_py`` hook rewrites the
   :data:`BUILD_SHA` / :data:`BUILD_DIRTY` placeholders below in the
   *build tree* at wheel-build time. No git needed at runtime.
2. **Source checkout** — when the placeholders are untouched and this
   file sits inside a git checkout with the ``src/`` layout (editable
   install or dev tree), a best-effort ``git rev-parse`` (2 s subprocess
   timeout) reports the checkout's HEAD as ``+dev.g<sha>``.
3. **Unknown** — an old wheel without the hook, or a checkout without a
   working ``git``: :func:`full_version` degrades to the bare version
   (checkout without git degrades to ``+dev``).

The bare :data:`hpc_agent.__version__` stays UNSUFFIXED on purpose: run
sidecars, the preflight cache, scaffold ``generator_version`` and the
back-compat expiry test all compare it as a plain release number.
Fingerprinted identity is an *additional* surface (``--version``, MCP
``serverInfo``, the ``doctor`` skew check), not a change to the number.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Build-time placeholders. The source tree ALWAYS carries these exact
# values; ``setup.py`` rewrites them in the wheel's build tree only (the
# checked-in file never changes, so builds don't dirty the checkout).
# ---------------------------------------------------------------------------
BUILD_SHA: str | None = None
BUILD_DIRTY: bool = False

# Cheap best-effort git call: never hang (the incident class this module
# exists for is a probe wedged without a timeout).
_GIT_TIMEOUT_SECONDS = 2.0

_SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# Memo for the dev-tree git lookup: (resolved?, sha). One subprocess per
# process at most, and only when --version / serverInfo / doctor ask.
_dev_sha_cache: list[str | None] = []


def git_output(args: list[str], *, cwd: Path, timeout: float = _GIT_TIMEOUT_SECONDS) -> str | None:
    """Run ``git <args>`` in *cwd*; stripped stdout, or ``None`` on ANY failure.

    Fail-open by contract: missing git binary, non-repo cwd, nonexistent
    cwd, timeout, non-zero exit and empty output all return ``None`` —
    callers treat "no answer" as "skip", never as an error.

    Two hard disciplines, both from the 2026-07-10 live MCP-server wedge
    (run #12: ``audit-preflight`` hung the WHOLE server on its first live
    call — py-spy stack: this function's post-timeout drain):

    * ``stdin=DEVNULL`` — a bare ``subprocess.run`` child INHERITS the
      parent's stdin, which inside ``mcp-serve`` is the live JSON-RPC pipe;
      a child that reads it blocks forever (offline probes never reproduce
      this: piped-file stdin hits EOF instantly).
    * tree-kill on timeout via
      :func:`hpc_agent.infra.bounded_subprocess.run_capture_bounded` — plain
      ``subprocess.run(timeout=)`` kills only the immediate child and then
      drains until EOF; a git grandchild (fsmonitor daemon et al.) holding
      the pipe defeats the timeout entirely (the run-#7 orphaned-ssh class,
      same cure). ``git -C <cwd>`` replaces the ``cwd=`` kwarg the bounded
      runner does not take.
    """
    from hpc_agent.infra.bounded_subprocess import run_capture_bounded

    try:
        proc = run_capture_bounded(
            ["git", "-C", str(cwd), *args],
            timeout_sec=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _source_repo_root() -> Path | None:
    """The repo root when running from a source checkout, else ``None``.

    Deliberately shallow: this file lives at ``<root>/src/hpc_agent/``
    in a checkout, so the root is exactly ``parents[2]`` — no upward
    walk (a venv nested inside an unrelated git repo must NOT match).
    A wheel install (``site-packages/hpc_agent/``) fails both guards.
    """
    try:
        root = Path(__file__).resolve().parents[2]
    except (OSError, IndexError):  # pragma: no cover — pathological install layout
        return None
    if (root / ".git").exists() and (root / "src" / "hpc_agent" / "__init__.py").is_file():
        return root
    return None


def _dev_tree_sha() -> str | None:
    """Best-effort short HEAD sha of the source checkout (memoized)."""
    if _dev_sha_cache:
        return _dev_sha_cache[0]
    root = _source_repo_root()
    sha: str | None = None
    if root is not None:
        out = git_output(["rev-parse", "--short=8", "HEAD"], cwd=root)
        if out is not None and _SHORT_SHA_RE.match(out):
            sha = out
    _dev_sha_cache.append(sha)
    return sha


def runtime_sha() -> str | None:
    """Short git sha identifying the *running* code, or ``None`` if unknowable.

    Wheel-embedded :data:`BUILD_SHA` wins; a source checkout falls back
    to its HEAD via git; anything else is ``None`` (fail-open).
    """
    if BUILD_SHA:
        return BUILD_SHA
    return _dev_tree_sha()


def build_fingerprint() -> str | None:
    """PEP 440 local-version segment for the running code, or ``None``.

    * wheel with embedded sha        → ``g<sha>`` (``g<sha>.dirty`` if built
      from a dirty tree)
    * source checkout, git works     → ``dev.g<sha>``
    * source checkout, git unusable  → ``dev``
    * wheel without build info       → ``None`` (identity unknowable)
    """
    if BUILD_SHA:
        return f"g{BUILD_SHA}.dirty" if BUILD_DIRTY else f"g{BUILD_SHA}"
    if _source_repo_root() is None:
        return None
    sha = _dev_tree_sha()
    return f"dev.g{sha}" if sha else "dev"


def full_version() -> str:
    """``<version>[+<fingerprint>]`` — e.g. ``0.10.65+g9154c3af``.

    Backward-parseable: the prefix up to ``+`` is exactly the plain
    version every existing consumer parses today.
    """
    from hpc_agent import __version__

    fingerprint = build_fingerprint()
    return f"{__version__}+{fingerprint}" if fingerprint else __version__


__all__ = [
    "BUILD_DIRTY",
    "BUILD_SHA",
    "build_fingerprint",
    "full_version",
    "git_output",
    "runtime_sha",
]
