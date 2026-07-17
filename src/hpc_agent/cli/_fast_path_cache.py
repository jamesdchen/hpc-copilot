"""Cross-process cache of the CLI fast-path plugin verdict (latency rank 13).

``_fast_dispatch_enabled`` must know whether an installed plugin can reshape a
core verb's CLI before it may serve that verb on the single-verb fast path. The
authoritative answer comes from
:func:`hpc_agent._kernel.registry.plugins.cli_reshaping_verdict`, which calls
``load_plugins()`` — and that pays an ``importlib.metadata.entry_points()`` scan
of installed-distribution metadata (MEASURED ~0.06 s on a lean dev venv, ~0.35 s
on the heavier live demo/hook env). Every ``hpc-agent`` subprocess in a
``block-drive`` loop re-pays it, even though the answer changes only when the
set of installed distributions changes (a ``pip install`` / upgrade / uninstall).

This module caches the reduced verdict — ``(conservative_full_walk,
reshaped_verbs)`` — in a small global JSON file, keyed on a CHEAP fingerprint of
the installed-distribution set (a scandir of ``*.dist-info`` / ``*.egg-info``
names across the active ``site-packages`` directories, MEASURED ~0.005 s, ~10×
cheaper than the ``entry_points`` scan it avoids). A signature match returns the
cached verdict without ever calling ``load_plugins()``; any change to the
installed set changes the signature and forces one fresh scan that repopulates
the cache.

Fail-open by construction: any error computing the signature, reading, or
writing the cache degrades to a fresh :func:`cli_reshaping_verdict` scan — the
cache is a pure latency optimisation, never a correctness gate. The kill switch
``HPC_AGENT_NO_FAST_PATH_CACHE=1`` bypasses the disk cache entirely (always a
fresh scan); ``HPC_AGENT_DISABLE_PLUGINS=1`` is handled upstream in the
dispatcher (no plugins → nothing to cache).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "cache_disabled",
    "cached_cli_reshaping_verdict",
    "installed_dist_signature",
]

_DISABLE_ENV_VAR = "HPC_AGENT_NO_FAST_PATH_CACHE"


def cache_disabled() -> bool:
    """True when ``HPC_AGENT_NO_FAST_PATH_CACHE=1`` opts the disk cache out."""
    return os.environ.get(_DISABLE_ENV_VAR) == "1"


def _cache_path() -> Path:
    """Global cache file under the journal home (honours ``HPC_JOURNAL_DIR``)."""
    from hpc_agent.state.run_record import current_homedir

    return current_homedir() / "_fast_path_verdict_cache.json"


def installed_dist_signature() -> str:
    """A cheap, stable fingerprint of the installed-distribution set.

    Scans every existing directory on ``sys.path`` (a single ``scandir`` level,
    not a walk) for dist-metadata directory names (``*.dist-info``,
    ``*.egg-info``, ``*.egg-link``) and hashes the sorted union together with the
    owning directory. A ``pip install`` / upgrade / uninstall — or a plugin
    dist-info injected via ``PYTHONPATH`` — adds, renames (the version is in the
    ``.dist-info`` name), or removes one of these entries, so any change to the
    discoverable distribution set changes the signature, exactly the
    invalidation the cache needs. A mere reorder of ``sys.path`` does not (the
    union is sorted). Deliberately does NOT read metadata file contents (that is
    the expensive path the cache exists to avoid); scanning every ``sys.path``
    dir (a handful) stays ~10× cheaper than the ``entry_points`` scan because it
    touches only directory entries.

    Raises on an environment we cannot fingerprint (nothing scannable) so the
    caller fails open to a fresh scan rather than trusting a degenerate key.
    """
    tokens: list[str] = []
    scanned = 0
    seen: set[str] = set()
    for entry in sys.path:
        if not entry:
            continue
        norm = os.path.normcase(os.path.abspath(entry))
        if norm in seen:
            continue
        seen.add(norm)
        try:
            with os.scandir(entry) as it:
                for de in it:
                    n = de.name
                    if n.endswith((".dist-info", ".egg-info", ".egg-link")):
                        tokens.append(f"{norm}/{n}")
            scanned += 1
        except OSError:
            continue
    if scanned == 0:
        raise RuntimeError("no scannable directory on sys.path")
    digest = hashlib.sha256("\n".join(sorted(tokens)).encode("utf-8")).hexdigest()
    return f"{scanned}:{len(tokens)}:{digest[:24]}"


def _read_cache() -> dict[str, Any]:
    """Best-effort read of the cache file; ``{}`` on any problem."""
    try:
        with open(_cache_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _decode_verdict(entry: Any) -> tuple[bool, frozenset[str]] | None:
    """Decode a cached verdict entry, or ``None`` when malformed."""
    if not isinstance(entry, dict):
        return None
    conservative = entry.get("conservative")
    reshaped = entry.get("reshaped")
    if not isinstance(conservative, bool) or not isinstance(reshaped, list):
        return None
    if not all(isinstance(v, str) for v in reshaped):
        return None
    return conservative, frozenset(reshaped)


def _write_cache(signature: str, verdict: tuple[bool, frozenset[str]]) -> None:
    """Record *verdict* under *signature* (best-effort; swallows write failure).

    Stores a SINGLE-entry file keyed on the current signature: on any
    installed-set change the whole file is replaced, so it never accretes stale
    per-signature rows. A lock/write failure degrades silently — the cache is an
    optimisation, never a correctness gate.
    """
    from hpc_agent.infra.io import advisory_flock, atomic_write_json

    conservative, reshaped = verdict
    payload = {
        "signature": signature,
        "verdict": {"conservative": conservative, "reshaped": sorted(reshaped)},
    }
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with advisory_flock(path.with_suffix(path.suffix + ".lock"), timeout_sec=30.0):
            atomic_write_json(path, payload)
    except OSError:
        pass


def cached_cli_reshaping_verdict() -> tuple[bool, frozenset[str]]:
    """Return the fast-path plugin verdict, cached across processes on the dist set.

    On a signature hit, returns the stored ``(conservative_full_walk,
    reshaped_verbs)`` WITHOUT importing or scanning entry points. On a miss (new
    signature, missing/corrupt cache, disabled cache, or any error), computes a
    fresh :func:`cli_reshaping_verdict` and repopulates the cache. Every failure
    path collapses to the fresh scan — the byte-identical, always-correct
    fallback.
    """
    # ``cli_reshaping_verdict`` is imported LAZILY, only on the branches that
    # actually scan (disabled cache, un-fingerprintable env, cache miss). On a
    # signature HIT — the steady state in a ``block-drive`` loop — the plugins
    # module (and its ``importlib.metadata`` entry-points chain) is never
    # imported at all, so a hot non-discovery fast verb pays only the cheap
    # scandir + cache read. Every scan branch still yields the byte-identical
    # verdict; only import timing differs.
    if cache_disabled():
        from hpc_agent._kernel.registry.plugins import cli_reshaping_verdict

        return cli_reshaping_verdict()

    try:
        signature = installed_dist_signature()
    except Exception:  # noqa: BLE001 — an un-fingerprintable env → fresh scan
        from hpc_agent._kernel.registry.plugins import cli_reshaping_verdict

        return cli_reshaping_verdict()

    data = _read_cache()
    if data.get("signature") == signature:
        decoded = _decode_verdict(data.get("verdict"))
        if decoded is not None:
            return decoded

    from hpc_agent._kernel.registry.plugins import cli_reshaping_verdict

    verdict = cli_reshaping_verdict()
    _write_cache(signature, verdict)
    return verdict
