"""Cross-invocation TTL cache for the cluster ssh-probe verdicts.

Every funnel stage's composite preflight re-runs ``check-preflight``, and
each invocation pays one full cold TCP+SSH handshake (named-pipe
ControlMaster multiplexing is broken on Windows — see
``_cluster_combined_probe``): ~1s on a healthy login node, 30-60s on a
loaded one (run #8 live, 2026-07-06). The probes are advisory HEALTH
checks, not correctness gates (staging and the canary are the hard gates),
so a SUCCESS verdict observed seconds ago is still meaningful — and NOT
reconnecting is strictly ban-safer: every cache hit is one fewer
connection for the cluster's intrusion filter to count (the 2026-07-04
ban-hammer incident is why ``infra.ssh_circuit`` exists).

Rules (ban-safety + honesty):

* **SUCCESS-only.** A failed probe is never cached; the next call re-probes.
* **TTL.** :data:`TTL_ENV` (default :data:`DEFAULT_TTL_SEC`); ``0`` (or any
  non-positive value) disables the cache entirely.
* **Breaker-invalidated.** Any connection failure the per-host circuit
  breaker recorded AFTER the verdict — or an open circuit — invalidates it:
  the path demonstrably degraded since the probe passed.
* **Honest.** Replayed checks carry a ``(cached: probe passed Ns ago)``
  detail suffix, so an envelope reader can tell a replay from a live probe.
* **Fail-open.** A broken cache dir/file degrades to "no cache" (probe
  live), never a raise — same posture as the breaker.

State lives at ``<journal home>/_preflight_probe_cache/<host>.json`` and
mutations use the repo's ``advisory_flock`` + ``atomic_write_json`` idiom,
mirroring ``infra.ssh_circuit`` (CLI invocations, detached workers, and the
MCP server are separate processes; the cache only saves handshakes if they
share one view).

This is the CROSS-STAGE sibling of ``write_preflight_marker``'s 24h
per-experiment marker: the marker gates the skill-level "re-run setup?"
question, this cache elides the per-invocation ssh round-trip itself.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

from hpc_agent.infra.ssh_circuit import _safe_name, circuit_state_path

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = ["DEFAULT_TTL_SEC", "TTL_ENV", "cache_path", "load_fresh", "probe_ttl_sec", "store"]

#: Env var overriding the verdict TTL in seconds; non-positive disables.
TTL_ENV = "HPC_PREFLIGHT_PROBE_TTL_SEC"

#: Default verdict TTL. Matches the breaker's BASE_COOLDOWN_SEC scale: long
#: enough to cover one funnel stage's composite preflights, short enough that
#: a genuinely degraded host is re-probed within minutes.
DEFAULT_TTL_SEC = 300.0


def probe_ttl_sec() -> float:
    """The effective TTL (seconds); ``0.0`` means the cache is disabled."""
    raw = os.environ.get(TTL_ENV, "")
    if not raw.strip():
        return DEFAULT_TTL_SEC
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_TTL_SEC


def cache_path(host: str) -> Path:
    """State file for *host* under the journal home (test-isolatable)."""
    from hpc_agent.state.run_record import _current_homedir

    return _current_homedir() / "_preflight_probe_cache" / f"{_safe_name(host)}.json"


def _lock_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".lock")


def _read_doc(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _host_degraded_since(host: str, verdict_at: float) -> bool:
    """True when the breaker holds evidence NEWER than the cached verdict.

    An open circuit, or a recorded connection failure after ``verdict_at``,
    means the ssh path demonstrably degraded since the probe passed — the
    verdict is stale regardless of TTL. Fail-open: unreadable breaker state
    reads as "no evidence".
    """
    doc = _read_doc(circuit_state_path(host))
    if doc is None:
        return False
    if doc.get("state") == "open":
        return True
    last = doc.get("last_failure")
    if isinstance(last, dict):
        try:
            return float(last.get("at") or 0.0) > verdict_at
        except (TypeError, ValueError):
            return False
    return False


def load_fresh(
    host: str, *, key: str, clock: Callable[[], float] = time.time
) -> list[dict[str, Any]] | None:
    """The cached passing checks for (*host*, *key*), or ``None`` to probe live.

    ``None`` when the cache is disabled, the entry is absent/expired/
    malformed, or the breaker recorded a failure since the verdict. A hit
    returns COPIES of the stored checks with the honest ``(cached: ...)``
    detail suffix appended.
    """
    ttl = probe_ttl_sec()
    if ttl <= 0:
        return None
    doc = _read_doc(cache_path(host))
    entries = (doc or {}).get("entries")
    entry = entries.get(key) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        return None
    try:
        at = float(entry["at"])
        checks = list(entry["checks"])
    except (KeyError, TypeError, ValueError):
        return None
    now = clock()
    if now - at > ttl or _host_degraded_since(host, at):
        return None
    if not checks or not all(isinstance(c, dict) and c.get("ok") is True for c in checks):
        return None
    suffix = f" (cached: probe passed {max(0.0, now - at):.0f}s ago)"
    return [{**c, "detail": f"{c.get('detail', '')}{suffix}"} for c in checks]


def store(
    host: str,
    *,
    key: str,
    checks: list[dict[str, Any]],
    clock: Callable[[], float] = time.time,
) -> None:
    """Record a fully-PASSING probe block for (*host*, *key*); never raises.

    Any non-passing check in the block means nothing is stored — a failure
    must always re-probe. Stale sibling entries are pruned on write.
    """
    ttl = probe_ttl_sec()
    if ttl <= 0 or not checks or not all(c.get("ok") is True for c in checks):
        return
    path = cache_path(host)
    now = clock()
    try:
        from hpc_agent.infra.io import advisory_flock, atomic_write_json

        path.parent.mkdir(parents=True, exist_ok=True)
        with advisory_flock(_lock_path(path)):
            doc = _read_doc(path) or {"schema_version": 1, "host": host, "entries": {}}
            entries = doc.get("entries")
            if not isinstance(entries, dict):
                entries = {}
            entries = {
                k: v
                for k, v in entries.items()
                if isinstance(v, dict) and now - _float_or(v.get("at"), 0.0) <= ttl
            }
            entries[key] = {"at": now, "checks": checks}
            doc["entries"] = entries
            atomic_write_json(path, doc)
    except OSError:
        # Fail-open: a broken cache dir must never break preflight.
        return


def _float_or(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
