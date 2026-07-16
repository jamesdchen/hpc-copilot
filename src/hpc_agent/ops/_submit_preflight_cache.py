"""TTL cache of a passing ``submit-preflight`` verdict (latency rank 14).

``submit-preflight`` fans out ``install-commands`` + ``load-context`` +
``check-preflight`` (a cold cluster SSH probe) + ``resolve-resources`` on every
S1, and the submit driver invokes S1 TWICE per run (a pre-resolve boundary, then
with the resolve spec). Across those two calls — minutes apart — nothing the
preflight validates has changed: same machine, same cluster, same
``clusters.yaml``. The second call re-proves an unchanged environment, paying the
SSH round-trip and four subprocess spawns again (MEASURED ~15-30 s/run of pure
repetition, per the 2026-07-15 latency audit).

This module caches a *passing* ``submit-preflight`` verdict per
``(cluster, framework-version, clusters.yaml mtime)`` for a TTL (default 15 min),
in one global JSON file alongside the journal home — the same shape and
invalidation discipline as the #255 :mod:`hpc_agent.state.preflight_cache`:

* The **cluster** name keys the verdict (a different cluster re-probes).
* The **framework version** is folded in, so a ``pip install -U`` misses.
* The **clusters.yaml mtime** is folded in, so editing a login node / target
  between submits misses.
* ``HPC_NO_SUBMIT_PREFLIGHT_CACHE=1`` disables the cache globally.
* ``HPC_SUBMIT_PREFLIGHT_TTL_SEC`` overrides the TTL.

Only PASSING verdicts are recorded — a ``fail`` is never cached, so the next
submit re-runs the fan-out (and a red cluster is never masked). Every hit is
DISCLOSED: the returned verdict carries a ``cache`` block naming the key, the
age, and the TTL so a reader always sees the machine was not re-probed.

Separately, the ``install-commands`` sub-call is skipped unless the **wheel
version stamp moved**: its assets (``~/.claude/{commands,skills}``) change only
on a package upgrade, so re-copying them on every submit at the same version is
pure waste. :func:`install_commands_fresh` / :func:`record_install_commands`
track the last version for which the copy ran; this skip fires even when the
broader verdict cache misses (e.g. a ``clusters.yaml`` edit forces a fresh SSH
probe but the assets are still current).

Fail-open by construction: a read/write/resolve error degrades to running the
fan-out. The cache is a latency optimisation, never a correctness gate.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_TTL_SEC",
    "cache_disabled",
    "clusters_yaml_mtime_token",
    "install_commands_fresh",
    "read_fresh_verdict",
    "record_install_commands",
    "record_verdict",
    "submit_preflight_cache_key",
]

#: Default freshness window (seconds) — 15 minutes, the #255 default value.
DEFAULT_TTL_SEC = 900

_DISABLE_ENV_VAR = "HPC_NO_SUBMIT_PREFLIGHT_CACHE"
_TTL_ENV_VAR = "HPC_SUBMIT_PREFLIGHT_TTL_SEC"
_INSTALL_MARKER_KEY = "_install_commands_version"


def cache_disabled() -> bool:
    """True when ``HPC_NO_SUBMIT_PREFLIGHT_CACHE=1`` opts the cache out entirely."""
    return os.environ.get(_DISABLE_ENV_VAR) == "1"


def _ttl_sec() -> int:
    """Effective TTL — ``HPC_SUBMIT_PREFLIGHT_TTL_SEC`` (positive int) or default."""
    raw = os.environ.get(_TTL_ENV_VAR)
    if raw:
        try:
            val = int(raw)
        except ValueError:
            return DEFAULT_TTL_SEC
        if val > 0:
            return val
    return DEFAULT_TTL_SEC


def _cache_path() -> Path:
    """Global cache file under the journal home (honours ``HPC_JOURNAL_DIR``)."""
    from hpc_agent.state.run_record import _current_homedir

    return _current_homedir() / "_submit_preflight_cache.json"


def _clusters_yaml_path() -> Path | None:
    """Resolve the ``clusters.yaml`` path :func:`load_clusters_config` would read.

    Independently re-derives that function's search order (explicit path not
    applicable here → ``HPC_CLUSTERS_CONFIG`` → ``~/.hpc-agent/clusters.yaml`` →
    the packaged default) purely to fingerprint the file's mtime. This is a
    deliberate best-effort replica, NOT a contract-pinned copy: if the loader's
    search order ever changes and this drifts, the only consequence is a cache
    key that misses (or fails to invalidate on a yaml edit) — never a wrong
    verdict. Returns ``None`` if nothing resolves; the caller then folds a
    sentinel token so the key stays stable and well-formed.
    """
    env_path = os.environ.get("HPC_CLUSTERS_CONFIG")
    if env_path:
        return Path(env_path)
    user_path = Path("~/.hpc-agent/clusters.yaml").expanduser()
    if user_path.is_file():
        return user_path
    try:
        from hpc_agent import _PACKAGE_ROOT

        return _PACKAGE_ROOT / "config" / "clusters.yaml"
    except Exception:  # noqa: BLE001 — package introspection must not break the key
        return None


def clusters_yaml_mtime_token() -> str:
    """A stable token for the active ``clusters.yaml``'s mtime (invalidation input).

    Returns the file mtime in nanoseconds as a string, or ``"none"`` when the
    path can't be resolved / stat'd — a stable sentinel that keeps the cache key
    well-formed (it simply won't invalidate on a yaml edit in that rare case).
    """
    path = _clusters_yaml_path()
    if path is None:
        return "none"
    try:
        return str(path.stat().st_mtime_ns)
    except OSError:
        return "none"


def submit_preflight_cache_key(*, cluster: str | None, version: str, clusters_mtime: str) -> str:
    """Stable cache key for a ``(cluster, framework-version, clusters.yaml mtime)`` tuple.

    ``cluster`` may be ``None`` (a cluster-less submit that runs no SSH probe);
    it is folded as the literal ``"-"`` so that verdict caches separately from
    any named cluster's. A change in any of the three yields a different key.
    """
    return f"{cluster or '-'}|{version}|{clusters_mtime}"


def _read_cache() -> dict[str, Any]:
    """Best-effort read of the cache file; ``{}`` on any problem."""
    try:
        with open(_cache_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (tolerating a trailing ``Z``) to aware UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def read_fresh_verdict(key: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Return the cached PASS verdict for *key* if still within TTL, else ``None``.

    The returned dict is the stored ``submit-preflight`` output augmented with a
    ``cache`` disclosure block (``hit``/``cached_at``/``age_sec``/``ttl_sec``/
    ``key``) so a hit is never silent. Every "not positively fresh" case
    (disabled, absent, malformed, expired) returns ``None`` → re-run the
    fan-out, the safe default. *now* is injectable for tests.
    """
    if cache_disabled():
        return None
    entry = _read_cache().get(key)
    if not isinstance(entry, dict):
        return None
    validated_at = _parse_iso(entry.get("validated_at"))
    if validated_at is None:
        return None
    verdict = entry.get("verdict")
    if not isinstance(verdict, dict):
        return None
    ttl = entry.get("ttl_sec")
    if not isinstance(ttl, int) or ttl <= 0:
        ttl = DEFAULT_TTL_SEC
    now = now or datetime.now(timezone.utc)
    age = (now - validated_at).total_seconds()
    if not (0 <= age < ttl):
        return None
    disclosed = dict(verdict)
    disclosed["cache"] = {
        "hit": True,
        "cached_at": entry.get("validated_at"),
        "age_sec": round(age, 3),
        "ttl_sec": ttl,
        "key": key,
        "message": (
            "submit-preflight verdict served from the TTL cache (rank 14): the "
            "machine + cluster + clusters.yaml were validated "
            f"{round(age)}s ago and re-proving was skipped. Set "
            "HPC_NO_SUBMIT_PREFLIGHT_CACHE=1 to force a fresh probe."
        ),
    }
    return disclosed


def _locked_update(mutate: Any) -> None:
    """Run *mutate(cache_dict)* under the advisory flock, then atomically write.

    Shared read-modify-write plumbing for both the verdict record and the
    install-commands marker so two concurrent submits can't lost-update each
    other. Best-effort — a lock/write failure is swallowed (the cache is an
    optimisation).
    """
    from hpc_agent.infra.io import advisory_flock, atomic_write_json

    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with advisory_flock(path.with_suffix(path.suffix + ".lock"), timeout_sec=120.0):
            cache = _read_cache()
            mutate(cache)
            atomic_write_json(path, cache)
    except OSError:
        pass


def record_verdict(key: str, verdict: dict[str, Any]) -> None:
    """Record a PASSING *verdict* for *key* (no-op when caching disabled).

    Only the caller's already-checked ``overall == "pass"`` verdict should reach
    here; a ``fail`` must never be cached. The stored copy strips any ``cache``
    block so a re-served verdict never nests stale disclosures.
    """
    if cache_disabled():
        return
    from hpc_agent.infra.time import utcnow_iso

    clean = {k: v for k, v in verdict.items() if k != "cache"}
    ttl = _ttl_sec()

    def _mutate(cache: dict[str, Any]) -> None:
        cache[key] = {"validated_at": utcnow_iso(), "ttl_sec": ttl, "verdict": clean}

    _locked_update(_mutate)


def install_commands_fresh(version: str) -> bool:
    """True when ``install-commands`` already ran for *version* (skip the re-copy).

    The assets ``install-commands`` writes change only on a package upgrade, so a
    recorded marker matching the current wheel version means the copy is current
    and the sub-call can be skipped even on a verdict-cache miss. Disabled →
    always ``False`` (never skip). A version that hasn't been recorded (fresh
    machine) → ``False`` so the first submit installs.
    """
    if cache_disabled():
        return False
    if not version:
        return False
    return _read_cache().get(_INSTALL_MARKER_KEY) == version


def record_install_commands(version: str) -> None:
    """Record that ``install-commands`` ran for *version* (no-op when disabled)."""
    if cache_disabled() or not version:
        return

    def _mutate(cache: dict[str, Any]) -> None:
        cache[_INSTALL_MARKER_KEY] = version

    _locked_update(_mutate)
