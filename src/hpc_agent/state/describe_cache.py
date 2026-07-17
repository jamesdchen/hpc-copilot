"""Disk cache of ``hpc-agent describe <name>`` output, keyed by BUILD CONTENT (#261).

``describe`` output is *framework-stable*: for a given build of ``hpc_agent``,
``describe <name>`` returns the same bytes every time — there is no
per-invocation input beyond the name. Yet the orchestrator issues many
``describe`` calls per workflow, each forking a Python subprocess + loading the
registry (~100-500ms). This memoizes the resolved ``data`` payload to disk,
keyed by the build fingerprint (``_build_info.BUILD_SHA``, stamped into a
wheel's build tree and travelling WITH the code):

    ~/.claude/hpc/describe_cache/g<BUILD_SHA>/<name>.json

A hit skips the registry load entirely.

**Why content-keying, not the version string (closes the same-version-reinstall
trap).** The cache directory used to be keyed by ``(pkg_version, name)``. That
assumed a new build always lands a new version number — false in this project,
which force-reinstalls dev wheels at the SAME version (``0.11.0``) constantly. A
same-version reinstall left the old version dir in place, so ``describe`` served
a STALE payload from the previous build's registry — wrong-but-plausible output,
the worst class (docket "describe-cache content-keying"). Keying the directory
on ``BUILD_SHA`` kills that trap by construction: a new wheel gets a new sha even
at the same version number, so a same-version reinstall lands in a fresh
directory and the stale payload is unreachable.

**When the cache is DISABLED.** ``BUILD_SHA`` identifies the running code only
for a clean wheel. In two cases the content is not cheaply identifiable, so the
cache is disabled outright — ``load`` returns ``None`` and ``store`` no-ops:

* ``BUILD_SHA is None`` — a source checkout (editable install / dev tree) or an
  old wheel without the build hook. There is no cheap content identity, and a
  dev checkout never had a fast describe anyway (the ``operations.json`` bake is
  likewise untrusted without a fingerprint — see the CLI fast-path principle).
* ``BUILD_DIRTY`` — a wheel built from a dirty tree. The sha names a commit the
  working content diverged from, so ``g<sha>`` would NOT be content-true; two
  different dirty trees at the same HEAD would collide. A wrong describe hit
  poisons every reader, so we refuse rather than key on a non-pinning sha.

``HPC_NO_DESCRIBE_CACHE=1`` additionally bypasses the cache (for development on
the describe path itself). The cache is opportunistic — any I/O error falls
through to the live path, never raising.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

__all__ = ["cache_disabled", "load", "store"]


def cache_disabled() -> bool:
    """True when ``HPC_NO_DESCRIBE_CACHE=1`` opts the cache out."""
    return os.environ.get("HPC_NO_DESCRIBE_CACHE") == "1"


def _full_registration_done() -> bool:
    """True only when the FULL primitive registry walk has completed.

    ``describe`` output is stable *only against the whole registry*. The
    single-verb CLI fast path leaves the registry PARTIAL — it imports one
    module and sets the weaker ``_DISPATCH_READY`` latch, but NOT
    ``_REGISTRATION_DONE`` (see ``register_single_module``). A ``describe``
    resolved off a partial registry would be wrong-but-plausible; persisting it
    here would POISON every full-path reader for the build's lifetime
    (premortem A1). So the *store* side gates on the STRONG latch — the *load*
    side stays as-is (a stale hit is impossible once storing is guarded).

    Read as a module attribute (never ``from ... import _REGISTRATION_DONE``):
    that honors the module-private boundary and keeps
    ``lint_private_cross_package_imports`` quiet — the latch is an internal
    registry signal, not a promoted API.
    """
    from hpc_agent._kernel.registry import primitive

    return bool(getattr(primitive, "_REGISTRATION_DONE", False))


def _content_key() -> str | None:
    """Cache directory key for the running build, or ``None`` when disabled.

    ``g<BUILD_SHA>`` for a clean wheel (content-true — a new wheel gets a new
    sha even at the same version, so a same-version reinstall never reuses a
    stale dir). ``None`` for a source checkout (``BUILD_SHA is None``) or a
    dirty-tree wheel (``BUILD_DIRTY``), where the content is not cheaply
    identifiable and the cache is disabled by construction.

    Attributes are read off the module object (not ``from ... import BUILD_SHA``)
    so a test can flip them with ``monkeypatch.setattr`` and so the value tracks
    the actually-loaded build. Importing ``hpc_agent._build_info`` is cheap: its
    module body pulls only ``re`` / ``subprocess`` / ``pathlib`` (all stdlib,
    already resident), and the git subprocess is lazy inside its functions —
    none of which this path calls.
    """
    from hpc_agent import _build_info

    if _build_info.BUILD_SHA and not _build_info.BUILD_DIRTY:
        return f"g{_build_info.BUILD_SHA}"
    return None


# describe names are validated (lowercase letters / digits / hyphens) before we
# ever reach here, but sanitise defensively so the name can never escape the
# build-key dir into a traversal path.
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


def _cache_path(name: str) -> Path | None:
    """Cache file for *name* under the build-key dir.

    ``None`` when the name is unsafe OR the cache is disabled for this build
    (source checkout / dirty wheel — see :func:`_content_key`). Resolving the
    journal home goes through the leaf :func:`hpc_agent.state._homedir.journal_homedir`,
    which is byte-identical to ``run_record.current_homedir`` but does not drag
    ``run_record``'s ~85ms ``dataclasses`` + ``inspect`` chain onto every hit.
    """
    if not _SAFE_NAME.match(name):
        return None
    key = _content_key()
    if key is None:
        return None
    from hpc_agent.state._homedir import journal_homedir

    return journal_homedir() / "describe_cache" / key / f"{name}.json"


def load(name: str) -> dict[str, Any] | None:
    """Return the cached ``describe`` data payload for *name*, or ``None``.

    ``None`` on cache-disabled (env or non-content-keyable build), miss, unsafe
    name, or any read/parse error — every "not a clean hit" case collapses to
    "compute it live".
    """
    if cache_disabled():
        return None
    path = _cache_path(name)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def store(name: str, data: dict[str, Any]) -> None:
    """Cache the ``describe`` data payload for *name* (best-effort, no-op if disabled).

    Refuses to persist under a PARTIAL registry (the single-verb fast path):
    caching a payload computed off an incompletely-walked registry would serve
    wrong-but-plausible ``describe`` output to every full-path reader for the
    build's lifetime (premortem A1). Only the full walk yields the stable bytes
    this cache promises, so ``store`` no-ops until ``_REGISTRATION_DONE``. It
    also no-ops when the build is not content-keyable (source checkout / dirty
    wheel — :func:`_cache_path` returns ``None``).
    """
    if cache_disabled():
        return
    if not _full_registration_done():
        return
    path = _cache_path(name)
    if path is None:
        return
    from hpc_agent.infra.io import atomic_write_json

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data)
    except OSError:
        pass
