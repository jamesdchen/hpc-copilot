"""Content-keyed disk cache for the evidence digest (E-cache).

The evidence digest (`state/evidence.py::collect_evidence` + the render) is a
recompute-on-read projection over sealed records. Recomputing it is cheap
relative to SSH but not free — every greenlight embeds a point query. This
memoizes a computed digest keyed by the *content* of the stores it reads, so a
hit is served only when nothing the collector would walk has changed.

The posture is `state/describe_cache.py` verbatim — opportunistic (any I/O or
decode error falls through to the live path, never raising),
``HPC_NO_EVIDENCE_CACHE=1`` opts out entirely — with **content keying**
replacing describe-cache's version keying:

- **Key = sha256 over the canonical JSON of**
  ``{pkg_version, spec, fingerprint}`` where ``fingerprint`` is the sorted list
  of ``(relpath, mtime_ns, size)`` for every file the collector would walk
  (globbed cheaply, ``os.stat`` only — no file reads). Any append to any
  journal / ledger / sidecar moves an ``st_mtime_ns`` → a new key → recompute.
  ``pkg_version`` in the key means a render-logic upgrade invalidates for free
  (the describe-cache version-string trap avoided by keying on content AND
  version, not version alone).
- Stored under ``<journal home>/evidence_cache/<key[:16]>.json``; old keys are
  harmless kilobyte debris. **Deleting the directory loses nothing** — the
  digest is recomputed byte-identically from the live stores (enforcement row:
  cache-deleted output byte-equals cached output).
- mtime granularity is accepted honestly: a same-``mtime_ns`` same-size rewrite
  could serve stale ONCE — tolerable for an advisory digest whose remedy is
  re-run, and the reason the cache is NEVER consulted by the append gate
  (citation verification there is always live).

``st_mtime_ns`` is available from ``os.stat`` on win32, so the key is
cross-platform.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from hpc_agent.infra.env_flags import env_flag

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "STORE_GLOBS",
    "CacheState",
    "cache_disabled",
    "cached_result",
    "compute_key",
    "lookup",
    "store_fingerprint",
    "store_result",
]

# The five stores `state/evidence.py::collect_evidence` walks (E-collector),
# as globs relative to an experiment dir. Exposed so T1 (the collector, which
# OWNS the authoritative walk) can import this constant rather than re-spelling
# the patterns — the one-walk seam. `store_fingerprint` takes the glob list as
# an argument so the collector is free to pass its own (this constant is the
# shared default). Scope journals contribute two globs (decision journal +
# look ledger); the other four stores contribute one each.
STORE_GLOBS: tuple[str, ...] = (
    ".hpc/conclusions/*.decisions.jsonl",  # 1 — conclusion journals
    ".hpc/scopes/*.decisions.jsonl",  # 2a — scope (tag) journals
    ".hpc/scopes/*.looks.jsonl",  # 2b — look ledgers
    ".hpc/campaigns/*/decisions.jsonl",  # 3 — campaign journals
    ".hpc/runs/*.json",  # 4 — run sidecars
    "_aggregated/_fingerprints/*.jsonl",  # 5 — fingerprint ledgers
)

CacheState = Literal["hit", "miss", "disabled"]


def cache_disabled() -> bool:
    """True when ``HPC_NO_EVIDENCE_CACHE=1`` opts the cache out.

    Follows the ``infra/env_flags.py`` convention (the ``HPC_NO_DESCRIBE_CACHE``
    idiom): unset/blank is default-off; only ``1``/``true``/``yes``/``on``
    enables the bypass.
    """
    return env_flag("HPC_NO_EVIDENCE_CACHE")


def _pkg_version() -> str:
    """Installed ``hpc-agent`` version, or a stable placeholder when absent."""
    from importlib.metadata import PackageNotFoundError, version

    for dist in ("hpc-agent", "hpc_agent"):
        try:
            return version(dist)
        except PackageNotFoundError:
            continue
    return "0+unknown"


def store_fingerprint(experiment_dir: Path, globs: Sequence[str] = STORE_GLOBS) -> list[list[Any]]:
    """Cheap content fingerprint of every file *globs* would match under *experiment_dir*.

    Returns a sorted list of ``[relpath, mtime_ns, size]`` (POSIX relpaths for
    cross-platform-stable keys), ``os.stat`` only — no file reads. Any append to
    a walked store moves its ``st_mtime_ns`` and/or ``size`` → a different
    fingerprint → a different cache key. Non-creating: ``Path.glob`` never
    materializes a directory, so fingerprinting a fresh namespace yields ``[]``
    and touches nothing.

    Tolerant: a file that vanishes mid-walk (glob then stat race) is skipped —
    the fingerprint is a best-effort snapshot, and a wrong snapshot only ever
    costs one stale-or-missed hit, never a raise.
    """
    entries: list[list[Any]] = []
    base = Path(experiment_dir)
    for pattern in globs:
        try:
            matches = list(base.glob(pattern))
        except OSError:
            continue
        for match in matches:
            try:
                st = match.stat()
            except OSError:
                continue
            try:
                rel = match.relative_to(base).as_posix()
            except ValueError:
                rel = match.as_posix()
            entries.append([rel, st.st_mtime_ns, st.st_size])
    entries.sort()
    return entries


def compute_key(spec: Mapping[str, Any], fingerprint: Any) -> str:
    """The content cache key: sha256 hex over ``{pkg_version, spec, fingerprint}``.

    *spec* is the query spec's fields (the caller passes a JSON-safe mapping;
    tags/lineage/as_of/fleet…). *fingerprint* is whatever content snapshot the
    caller assembled — a single :func:`store_fingerprint` list for one
    namespace, or a mapping ``{repo_hash: fingerprint}`` for fleet mode. The
    cache is agnostic to its shape; it only canonicalizes and hashes.
    """
    import hashlib

    material = {
        "pkg_version": _pkg_version(),
        "spec": spec,
        "fingerprint": fingerprint,
    }
    # The harness-contract canonical form (`state/determinism.py::canonical_sha`
    # spelling) — deterministic, key-sorted, compact.
    payload = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    """Cache file for *key* under the journal home's ``evidence_cache/`` dir."""
    from hpc_agent.state.run_record import _current_homedir

    return _current_homedir() / "evidence_cache" / f"{key[:16]}.json"


def cached_result(key: str) -> dict[str, Any] | None:
    """Return the cached digest payload for *key*, or ``None``.

    ``None`` on cache-disabled, miss, or any read/parse error — every "not a
    clean hit" case collapses to "recompute it live". Never raises.
    """
    if cache_disabled():
        return None
    try:
        with open(_cache_path(key), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def store_result(key: str, payload: dict[str, Any]) -> None:
    """Cache the digest *payload* for *key* (best-effort, no-op if disabled).

    Any I/O error is swallowed — the cache is disposable, so a failed write
    just means the next read misses and recomputes.
    """
    if cache_disabled():
        return
    from hpc_agent.infra.io import atomic_write_json

    path = _cache_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, payload, fsync=False)
    except OSError:
        pass


def lookup(key: str) -> tuple[CacheState, dict[str, Any] | None]:
    """Read the cache and report which of the three states occurred.

    The verbs (T5/T6) record the returned :data:`CacheState` in their result's
    ``cache`` field. ``"disabled"`` when the env opt-out is set (no read
    attempted), ``"hit"`` with the payload when a clean cached entry exists,
    ``"miss"`` with ``None`` otherwise (absent, corrupt, or unreadable — all
    fall through to recompute).
    """
    if cache_disabled():
        return "disabled", None
    payload = cached_result(key)
    if payload is None:
        return "miss", None
    return "hit", payload
