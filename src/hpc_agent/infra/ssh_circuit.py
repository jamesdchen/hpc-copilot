"""Persistent per-host SSH circuit breaker — the fleet-level ban-hammer guard.

The 2026-07-04 incident: a wedged SSH preflight probe retried against one
cluster all night, piling up thousands of half-open connections until the
cluster's intrusion filter banned the box's source IP (login nodes first,
then the DTN). The per-call timeout fix bounds ONE call — but nothing
stopped a *fleet* of workers, detached runners, and retry ladders from
collectively hammering the host. This module is that stop.

Mechanism (classic circuit breaker, persisted per host):

* Every ssh-family attempt reports its outcome. **Consecutive
  connection-level failures** (connect timeout, banner-exchange timeout,
  connection refused/reset — see :data:`_CONNECTION_FAILURE_MARKERS`, plus
  the wrapper-level :class:`TimeoutError`) increment a per-host counter;
  any attempt that reaches the remote side (success, auth failure, remote
  command non-zero) proves the connection path works and resets it. The
  discriminator is the ssh client's OWN failure signal: OpenSSH exits 255
  when the client itself fails (connect/banner/kex) and otherwise
  propagates the REMOTE command's exit status — so a non-255 non-zero exit
  means the command RAN and its stderr is remote content, never transport
  evidence, no matter how marker-shaped it is (:func:`classify_connection_failure`).
* At :data:`CIRCUIT_THRESHOLD` consecutive failures the circuit **opens**:
  every subsequent attempt to that host fails FAST with
  :class:`hpc_agent.errors.SshCircuitOpen` (``ssh_circuit_open`` /
  ``network`` / ``retry_safe=False``) instead of opening another
  connection the intrusion filter will count.
* After the cooldown exactly ONE caller may claim the **half-open probe
  slot**; its success closes the circuit, its failure re-opens with the
  NEXT cycle's cooldown. The slot is claimed under the state file's lock so
  a fleet waking up together cannot stampede. A caller that supplies a
  ``probe_fn`` runs a cheap ``ssh true``-class liveness probe INLINE on
  claiming the slot (success closes + the caller proceeds; a failed probe
  re-opens at the next cycle) — see :func:`check_circuit`.

Cooldown is GRADUATED by re-open cycle, not flat (2026-07-18 incident: a
kill-drill connection storm tripped 3 timeouts against a MaxStartups-class
host that healed seconds after the burst stopped, yet the flat 300s cooldown
fenced a harvest arriving 60s later for ~5 minutes). The schedule keyed on
:data:`reopen_cycles <_fresh_doc>` — cycle 1 → :data:`CYCLE1_COOLDOWN_SEC`,
cycle 2 → :data:`CYCLE2_COOLDOWN_SEC`, cycle 3+ → :data:`CYCLE3_PLUS_COOLDOWN_SEC`
(:func:`cooldown_for_cycle`) — retries a transient blip fast and only escalates
if the host stays down. These change WAIT TIME, never verdicts: the evidence
rules (what counts as a failure, the single-flight probe, positive-evidence
closes) are untouched.

Storm-aware attribution (DISCLOSURE only): a bounded per-host ring of recent
connection-establishment (failure) timestamps lets the OPEN transition detect a
SELF-inflicted burst — :data:`STORM_ESTABLISHMENT_THRESHOLD`+ local
establishments in the trailing :data:`STORM_WINDOW_SEC` — and stamp
``suspected_cause="self-storm"``, surfaced in the refusal message and holding
the short cycle-1 cooldown while the correlation persists (a self-storm heals
the instant the local hammering stops). No correlation ⇒ normal escalation. The
stamp never changes a verdict or the evidence standard.

State lives at ``<journal home>/_ssh_circuit/<host>.json`` (journal home =
:func:`hpc_agent.state.run_record.current_homedir`, so ``HPC_JOURNAL_DIR``
redirection — and therefore the test suite's autouse isolation — applies).
File-based on purpose: CLI invocations, detached workers, and the MCP
server are separate processes, and the breaker only works if they share
one view of the host's health. Mutations use the repo's standard
``advisory_flock`` + ``atomic_write_json`` idiom (same as
``state/canary_cache.py``) for cross-process safety on POSIX and win32.

Override: ``HPC_SSH_CIRCUIT_OVERRIDE=<host>[,<host>...]`` bypasses the
fail-fast for exactly the named hosts — explicit and per-host, for the
operator who knows why the failures happened (e.g. a VPN flap) and accepts
the ban risk. Failures are still recorded while overridden.

Fail-open posture: a broken state dir / lock must never block SSH — every
read/write error here degrades to "breaker inactive", loudly where useful.
The breaker is a protection layer, not a correctness gate.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from hpc_agent.errors import SshCircuitOpen

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable
    from pathlib import Path

__all__ = [
    "BASE_COOLDOWN_SEC",
    "CIRCUIT_THRESHOLD",
    "CYCLE1_COOLDOWN_SEC",
    "CYCLE2_COOLDOWN_SEC",
    "CYCLE3_PLUS_COOLDOWN_SEC",
    "DEGRADATION_CYCLE_THRESHOLD",
    "INCIDENT_WINDOW_SEC",
    "MAX_COOLDOWN_SEC",
    "PROBE_CLAIM_TTL_SEC",
    "SELF_STORM_CAUSE",
    "STORM_ESTABLISHMENT_THRESHOLD",
    "STORM_WINDOW_SEC",
    "check_circuit",
    "circuit_state_path",
    "classify_connection_failure",
    "cooldown_for_cycle",
    "degradation_advice",
    "degradation_advice_for_host",
    "effective_state",
    "guarded_call",
    "hanging_stage",
    "host_circuit_ok",
    "is_preamble_degraded",
    "liveness_probe",
    "open_deadline",
    "record_connection_failure",
    "record_connection_success",
    "sibling_clusters",
]

#: Consecutive connection-level failures that open the circuit.
CIRCUIT_THRESHOLD = 3

#: Legacy flat cooldown (seconds). NO LONGER the live first-open value — that is
#: now :data:`CYCLE1_COOLDOWN_SEC` via the graduated :func:`cooldown_for_cycle`
#: schedule. Retained as (a) the ``cooldown_sec`` fallback in
#: :func:`open_deadline` — so a pre-schedule state file (which stored a flat
#: ``300``) reads with its own value and a doc missing the field reads 300, the
#: byte-compat default — and (b) the closed-doc placeholder in :func:`_fresh_doc`
#: (meaningless until an open recomputes it). Equal to :data:`CYCLE3_PLUS_COOLDOWN_SEC`.
BASE_COOLDOWN_SEC = 300.0

#: Graduated per-reopen-cycle cooldown schedule (seconds). Cycle 1 retries fast
#: (a MaxStartups-class storm heals seconds after the burst stops); cycle 2 and
#: cycle 3+ escalate only if the host stays down. Constants, not a formula, so
#: the schedule is auditable and each rung is independently pinned.
CYCLE1_COOLDOWN_SEC = 15.0
CYCLE2_COOLDOWN_SEC = 60.0
CYCLE3_PLUS_COOLDOWN_SEC = 300.0

#: Incident-window anchor (seconds). Once the exponential-doubling cooldown
#: ceiling; now the schedule tops out at :data:`CYCLE3_PLUS_COOLDOWN_SEC`, and
#: this value only sizes :data:`INCIDENT_WINDOW_SEC` (kept so that window — the
#: run-13 degradation cycle counter's expiry — is unchanged by the schedule).
MAX_COOLDOWN_SEC = 3600.0

#: Trailing window over which local connection establishments are counted to
#: detect a SELF-inflicted connection storm (seconds).
STORM_WINDOW_SEC = 30.0

#: Local establishments within :data:`STORM_WINDOW_SEC` at the OPEN transition
#: that classify the trip as self-inflicted (tunable; a lone
#: :data:`CIRCUIT_THRESHOLD`-consecutive trip stays well under it).
STORM_ESTABLISHMENT_THRESHOLD = 6

#: Hard cap on the establishment ring's length (bounds the state file's size even
#: if the window is widened). The window prune is the primary bound.
_ESTABLISHMENT_RING_MAX = 64

#: The ``suspected_cause`` stamp value for a self-inflicted connection burst.
SELF_STORM_CAUSE = "self-storm"

#: A claimed half-open probe slot older than this is considered abandoned
#: (claimant crashed / was killed) and may be reclaimed. Comfortably larger
#: than one ssh attempt's worst case (SSH_TIMEOUT_SEC=60s default).
PROBE_CLAIM_TTL_SEC = 120.0

#: Env var naming hosts whose OPEN circuit is explicitly bypassed
#: (comma-separated). Per-host and explicit by design — no global kill switch.
OVERRIDE_ENV = "HPC_SSH_CIRCUIT_OVERRIDE"

#: Re-opens WITHIN one incident window that classify the host as PREAMBLE/NODE
#: DEGRADED rather than transport-faulted (run-13 finding 10 + 10-addendum).
#: The livelock signature: a cheap connection probe keeps CLOSING the circuit
#: while the process-spawning ``module load … && source …/conda.sh`` preamble
#: times out every real attempt (discovery2's degraded /apps mount) — so the
#: breaker cycles open→(probe closes)→open forever. At this many opens inside
#: :data:`INCIDENT_WINDOW_SEC` the classifier stops treating it as a transient
#: transport blip and names the degradation + suggests ``host-retarget``. Two
#: is deliberately early: it means the circuit has ALREADY re-opened after a
#: close at least once — a VPN flap (which fails the probe too) never produces
#: it, only the probe-OK/command-timeout split does.
DEGRADATION_CYCLE_THRESHOLD = 2

#: Opens farther apart than this belong to SEPARATE incidents, so the cycle
#: counter (which a connection-level success deliberately does NOT reset, only
#: this expiry does) starts fresh. Generous relative to the livelock's own
#: cadence — each deceptive close wipes the cooldown back to
#: :data:`BASE_COOLDOWN_SEC`, so the run-13 cycles were ~minutes apart — but far
#: tighter than the days/weeks between genuinely unrelated opens.
INCIDENT_WINDOW_SEC = 2 * MAX_COOLDOWN_SEC

#: Substrings (matched case-insensitively against the timed-out command carried
#: in ``last_failure.detail``) that NAME the hanging preamble stage. The wrapper
#: ``TimeoutError`` message embeds the command
#: (``ssh to … timed out after 60s: <cmd>``), so the stage that hung is
#: recoverable by parsing it — no new plumbing. Ordered most-specific first.
_PREAMBLE_STAGE_MARKERS: tuple[tuple[str, str], ...] = (
    ("conda.sh", "the conda activation (`source …/conda.sh`)"),
    ("conda activate", "the conda activation (`conda activate`)"),
    ("module load", "the module subsystem (`module load`)"),
    ("module purge", "the module subsystem (`module purge`)"),
)

# stderr markers that mean the CONNECTION itself failed — no TCP/SSH session
# was established (or it was torn down at banner/kex time). Auth failures
# ("Permission denied") and remote-command non-zero exits deliberately do NOT
# appear here: they prove the host accepted a connection, which is the
# opposite of ban-risk evidence. Matched case-insensitively against
# stderr+stdout, mirroring remote._SSH_THROTTLE_MARKERS' matching style —
# and ONLY consulted for ``returncode == 255`` (the ssh client's own failure
# code): at any other non-zero exit the remote command ran and marker-shaped
# text in its stderr is REMOTE content (see classify_connection_failure).
_CONNECTION_FAILURE_MARKERS: tuple[str, ...] = (
    "connection refused",
    "connection reset by peer",
    "connection timed out",
    "timed out during banner exchange",
    "operation timed out",
    "no route to host",
    "network is unreachable",
    # sshd-side teardown before auth (MaxStartups, fail2ban) — suffix-trimmed
    # so both "... by remote host" and the bare form match.
    "ssh_exchange_identification: connection closed",
    "kex_exchange_identification: connection closed",
    "kex_exchange_identification: read: connection reset",
)


def _host(ssh_target: str) -> str:
    """Host key for *ssh_target* (``user@host`` or a bare alias) — the same
    normalization :func:`hpc_agent.infra.ssh_throttle._host` uses, so the
    throttle and the breaker key identically."""
    return ssh_target.rsplit("@", 1)[-1].strip()


def _safe_name(host: str) -> str:
    """Filesystem-safe filename component for *host*."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", host)


def circuit_state_path(host: str) -> Path:
    """State file for *host* under the journal home (test-isolatable)."""
    from hpc_agent.state.run_record import current_homedir

    return current_homedir() / "_ssh_circuit" / f"{_safe_name(host)}.json"


def _lock_path(target: Path) -> Path:
    """Sibling ``.lock`` path — the repo-wide ``<name>.lock`` convention."""
    return target.with_suffix(target.suffix + ".lock")


def _read_doc(path: Path) -> dict[str, Any] | None:
    """Parse the state file; ``None`` on absent/unreadable/malformed."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _overridden(host: str) -> bool:
    """True when *host* is named in :data:`OVERRIDE_ENV`."""
    raw = os.environ.get(OVERRIDE_ENV, "")
    if not raw.strip():
        return False
    return host in {part.strip() for part in raw.split(",") if part.strip()}


def _fresh_doc(host: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "host": host,
        "state": "closed",
        "consecutive_failures": 0,
        "cooldown_sec": BASE_COOLDOWN_SEC,
        "opened_at": None,
        "probe_claimed_at": None,
        "last_failure": None,
        # run-13 finding 10: opens within one incident window. A connection-level
        # success (the deceptive cheap probe) preserves this; only INCIDENT_WINDOW
        # expiry resets it. >= DEGRADATION_CYCLE_THRESHOLD ⇒ preamble-degraded.
        # Also indexes the graduated cooldown schedule (cooldown_for_cycle).
        "reopen_cycles": 0,
        "incident_started_at": None,
        # 2026-07-18 storm attribution (additive; a pre-schedule file lacking
        # these reads with the None/[] defaults everywhere via .get()):
        # a self-inflicted burst stamp (disclosure only) + the bounded ring of
        # recent connection-establishment timestamps that detects it.
        "suspected_cause": None,
        "recent_establishments": [],
    }


def _float_or(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cooldown_for_cycle(cycle: int) -> float:
    """The graduated cooldown (seconds) for re-open *cycle* (1-based).

    Cycle 1 → :data:`CYCLE1_COOLDOWN_SEC`, cycle 2 → :data:`CYCLE2_COOLDOWN_SEC`,
    cycle 3+ → :data:`CYCLE3_PLUS_COOLDOWN_SEC`. A degenerate ``cycle <= 0``
    (a doc missing/zeroed ``reopen_cycles``) reads as cycle 1 — fail-open toward
    the SHORT lane (a misparsed counter must never fence a host longer than the
    evidence warrants).
    """
    if cycle <= 1:
        return CYCLE1_COOLDOWN_SEC
    if cycle == 2:
        return CYCLE2_COOLDOWN_SEC
    return CYCLE3_PLUS_COOLDOWN_SEC


def _select_cooldown(doc: dict[str, Any]) -> float:
    """The cooldown to write on an open/re-open transition.

    While a self-storm correlation holds (``suspected_cause`` stamped by
    :func:`_stamp_suspected_cause` earlier in the same transition), the SHORT
    cycle-1 lane is held even as ``reopen_cycles`` climbs — a self-inflicted
    burst heals the instant the local hammering stops, so escalating would fence
    a host that is already fine. No stamp ⇒ the normal graduated schedule.
    """
    if doc.get("suspected_cause") == SELF_STORM_CAUSE:
        return CYCLE1_COOLDOWN_SEC
    return cooldown_for_cycle(int(_float_or(doc.get("reopen_cycles"), 0)))


def _record_establishment(doc: dict[str, Any], *, now: float) -> None:
    """Append *now* to the bounded per-host establishment ring, pruning the
    trailing-:data:`STORM_WINDOW_SEC` window (and hard-capping the length).

    Called on every connection-level FAILURE — the ban-relevant establishments
    (a connection we opened that the host/intrusion filter counts, dropped at
    connect/banner). Successes take the hot no-op path and are not ban-risk
    evidence, so they are not ringed; a self-storm severe enough to OPEN the
    circuit necessarily produced a burst of these failures.
    """
    raw = doc.get("recent_establishments")
    ring = list(raw) if isinstance(raw, list) else []
    ring.append(now)
    cutoff = now - STORM_WINDOW_SEC
    ring = [t for t in ring if _is_ts(t) and t >= cutoff]
    if len(ring) > _ESTABLISHMENT_RING_MAX:
        ring = ring[-_ESTABLISHMENT_RING_MAX:]
    doc["recent_establishments"] = ring


def _is_ts(value: Any) -> bool:
    """A real numeric timestamp (``bool`` is an ``int`` subclass — exclude it)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _recent_establishment_count(doc: dict[str, Any], *, now: float) -> int:
    """How many ringed establishments fall in the trailing :data:`STORM_WINDOW_SEC`."""
    raw = doc.get("recent_establishments")
    if not isinstance(raw, list):
        return 0
    cutoff = now - STORM_WINDOW_SEC
    return sum(1 for t in raw if _is_ts(t) and t >= cutoff)


def _stamp_suspected_cause(doc: dict[str, Any], *, now: float) -> None:
    """Stamp (or clear) ``suspected_cause`` on an open/re-open transition.

    :data:`STORM_ESTABLISHMENT_THRESHOLD`+ local establishments in the trailing
    window ⇒ ``"self-storm"``; otherwise cleared to ``None`` (a later, unrelated
    trip must escalate normally). DISCLOSURE only — it selects the cooldown LANE
    (:func:`_select_cooldown`) and rides the refusal message, never a verdict.
    """
    if _recent_establishment_count(doc, now=now) >= STORM_ESTABLISHMENT_THRESHOLD:
        doc["suspected_cause"] = SELF_STORM_CAUSE
    else:
        doc["suspected_cause"] = None


def open_deadline(doc: dict[str, Any], *, now: float) -> float:
    """When *doc*'s open cooldown ends (``opened_at + cooldown_sec``).

    *now* backstops a malformed/missing ``opened_at`` (fail-open: an
    unparseable open looks freshly opened rather than crashing a renderer).
    """
    return _float_or(doc.get("opened_at"), now) + _float_or(
        doc.get("cooldown_sec"), BASE_COOLDOWN_SEC
    )


def effective_state(
    doc: dict[str, Any] | None, *, now: float
) -> Literal["closed", "open", "half_open_eligible"]:
    """The honest READ-side breaker state for *doc* at *now* — the single
    definition every renderer routes through (doctor's ``open_ssh_circuits``,
    status-snapshot, net-triage's verdict).

    The state FILE keeps saying ``"open"`` after the cooldown lapses: nothing
    rewrites it until traffic triggers the half-open probe, so a raw read
    shows a stale OPEN indefinitely (2026-07-05: the discovery breaker read
    "open" long past its 300s cooldown). This reports what the breaker will
    actually DO on the next attempt:

    * ``"open"`` — genuinely cooling; SSH to the host fails fast until the
      :func:`open_deadline`.
    * ``"half_open_eligible"`` — cooldown lapsed, no probe has run yet; the
      next connection attempt will claim the single half-open probe slot
      (success closes the circuit, failure re-opens it with a doubled
      cooldown). Nothing is failing fast anymore.
    * ``"closed"`` — healthy (also for a missing/unreadable doc: fail-open).

    Read-side ONLY: never writes state and never claims the probe slot —
    :func:`check_circuit` owns every transition.
    """
    if doc is None or doc.get("state") != "open":
        return "closed"
    return "open" if now < open_deadline(doc, now=now) else "half_open_eligible"


# ── preamble / node degradation classification (run-13 finding 10) ───────────


def _bump_cycle(doc: dict[str, Any], *, now: float) -> None:
    """Increment the incident's open-cycle counter, resetting on window expiry.

    Called on every transition INTO the open state (a fresh open or a half-open
    re-open). The counter deliberately survives a connection-level success
    (:func:`record_connection_success` carries it forward) — the run-13 livelock
    is exactly a cheap probe closing the circuit between preamble timeouts, so
    resetting on that success would erase the signal. Only the incident window
    lapsing (a genuinely separate later incident) starts the count fresh.
    """
    prev = int(_float_or(doc.get("reopen_cycles"), 0))
    started = doc.get("incident_started_at")
    fresh_incident = (
        prev <= 0 or started is None or (now - _float_or(started, now)) > INCIDENT_WINDOW_SEC
    )
    if fresh_incident:
        doc["reopen_cycles"] = 1
        doc["incident_started_at"] = now
    else:
        doc["reopen_cycles"] = prev + 1


def hanging_stage(doc: dict[str, Any] | None) -> str | None:
    """Name the timed-out preamble stage from ``last_failure.detail``, or ``None``.

    The wrapper ``TimeoutError`` embeds the command it timed out on
    (``ssh to … timed out after 60s: <cmd>``), so a degraded module/conda
    preamble is recoverable by string-matching the recorded detail against
    :data:`_PREAMBLE_STAGE_MARKERS`. ``None`` when no preamble marker survived
    (the upstream ``remote._truncate`` caps the embedded command, so a very long
    ``cd`` prefix can elide the marker — the caller falls back to a generic
    "remote command preamble" phrasing).
    """
    if not isinstance(doc, dict):
        return None
    last = doc.get("last_failure")
    detail = ""
    if isinstance(last, dict):
        detail = str(last.get("detail") or "")
    blob = detail.lower()
    for marker, stage in _PREAMBLE_STAGE_MARKERS:
        if marker in blob:
            return stage
    return None


def is_preamble_degraded(doc: dict[str, Any] | None, *, now: float) -> bool:
    """True when *doc* shows the run-13 livelock: the circuit has re-opened
    :data:`DEGRADATION_CYCLE_THRESHOLD`+ times inside one still-live incident
    window. A stale counter from an OLD incident (its ``incident_started_at``
    now beyond :data:`INCIDENT_WINDOW_SEC`) reads as NOT degraded — the host may
    have recovered and no traffic has reset the file yet (the same read-seam
    honesty as :func:`effective_state`)."""
    if not isinstance(doc, dict):
        return False
    if int(_float_or(doc.get("reopen_cycles"), 0)) < DEGRADATION_CYCLE_THRESHOLD:
        return False
    started = doc.get("incident_started_at")
    if started is None:
        return False
    return (now - _float_or(started, now)) <= INCIDENT_WINDOW_SEC


# MIRROR: hpc_agent.ops.host_retarget::_cluster_scheduler_scratch pinned-by tests/infra/test_ssh_circuit.py::test_cluster_scheduler_scratch_lockstep_with_host_retarget  # noqa: E501
def _cluster_scheduler_scratch(cfg: dict[str, Any]) -> tuple[str, str]:
    """The ``(scheduler, scratch)`` pair from a raw ``clusters.yaml`` entry —
    the failover-equivalence signature: a sibling login node must serve the
    SAME scheduler + scratch, or it is a cluster MOVE, not a failover."""
    return (
        str(cfg.get("scheduler") or "").strip(),
        str(cfg.get("scratch") or "").strip(),
    )


def sibling_clusters(host: str) -> list[str]:
    """Cluster keys that are ``host-retarget`` siblings of *host*.

    A sibling is any OTHER ``clusters.yaml`` entry whose ``(scheduler, scratch)``
    matches the cluster(s) that resolve to *host* but whose login ``host``
    differs — i.e. a healthy login node you can fail an in-flight run over to
    without re-staging (``host-retarget`` keeps the same jobs/run_id/scratch).
    Read-only and fail-open: any config error yields ``[]`` (→ the caller
    suggests ``settle-run`` instead). The resolver itself is UNCHANGED — this
    only NAMES candidates for the operator; no automatic rotation (deferred)."""
    if not host:
        return []
    try:
        from hpc_agent.infra.clusters import load_clusters_config

        clusters = load_clusters_config()
    except Exception:
        return []
    if not isinstance(clusters, dict):
        return []
    target_keys = {
        name
        for name, cfg in clusters.items()
        if isinstance(cfg, dict) and str(cfg.get("host") or "").strip() == host
    }
    signatures = {
        _cluster_scheduler_scratch(clusters[name])
        for name in target_keys
        if isinstance(clusters.get(name), dict)
    }
    if not signatures:
        return []
    sibs: set[str] = set()
    for name, cfg in clusters.items():
        if not isinstance(cfg, dict) or name in target_keys:
            continue
        other_host = str(cfg.get("host") or "").strip()
        if other_host and other_host != host and _cluster_scheduler_scratch(cfg) in signatures:
            sibs.add(str(name))
    return sorted(sibs)


def _retarget_suggestion(host: str) -> str:
    """The mid-run remedy line: name real siblings, or fall back to settle-run."""
    sibs = sibling_clusters(host)
    if sibs:
        joined = ", ".join(f"`host-retarget {s}`" for s in sibs)
        return (
            f"prefer failing over to a healthy sibling login node of the same "
            f"cluster ({joined}) — same scheduler+scratch, jobs keep running"
        )
    return (
        "no sibling login node shares this cluster's scheduler+scratch, so "
        "`host-retarget` has no target — use `settle-run` with directed sacct "
        "evidence to close the run out-of-preamble instead"
    )


def degradation_advice(host: str, doc: dict[str, Any] | None, *, now: float) -> str | None:
    """The one-line degradation classification for *host*, or ``None`` if not
    degraded. Names the hanging preamble stage, the cycle count, and the
    ``host-retarget``/``settle-run`` remedy — the single string every surface
    (the fail-fast envelope, the monitor tick, net-triage) appends."""
    if not is_preamble_degraded(doc, now=now):
        return None
    cycles = int(_float_or((doc or {}).get("reopen_cycles"), 0))
    stage = hanging_stage(doc) or "the remote command preamble"
    return (
        f"probe-OK but {stage} times out ({cycles} cycles) — likely node-local "
        f"degradation (module subsystem / degraded mount), NOT a transport fault; "
        f"a bare connect/echo 'verifies' nothing here. {_retarget_suggestion(host)}."
    )


def degradation_advice_for_host(host: str, *, now: float | None = None) -> str | None:
    """:func:`degradation_advice` reading *host*'s breaker doc from disk — the
    read-only entry point for consumers (monitor, net-triage) that hold only a
    host, never the doc. Fail-open: an unreadable doc yields ``None``."""
    now = time.time() if now is None else now
    return degradation_advice(host, _read_doc(circuit_state_path(host)), now=now)


def host_circuit_ok(host: str, *, now: float | None = None) -> bool:
    """True when *host* looks USABLE right now: its breaker is not genuinely
    open and it is not preamble-degraded (run-13 finding 10).

    The read-only health predicate the login-pool failover uses to pick a
    healthy sibling: a member with no breaker doc (never contacted), a closed
    circuit, or a cooldown-lapsed ``half_open_eligible`` circuit is usable; a
    genuinely-open circuit or a preamble-degraded one is NOT. Fail-open: an
    unreadable doc reads as healthy (same read-seam honesty as
    :func:`effective_state`)."""
    now = time.time() if now is None else now
    doc = _read_doc(circuit_state_path(host))
    if effective_state(doc, now=now) == "open":
        return False
    return not is_preamble_degraded(doc, now=now)


def _open_error(
    host: str, doc: dict[str, Any], *, now: float, probe_in_flight: bool
) -> SshCircuitOpen:
    """Build the fail-fast envelope error: why, until when, how to override."""
    failures = int(_float_or(doc.get("consecutive_failures"), 0))
    deadline = open_deadline(doc, now=now)
    if probe_in_flight:
        detail = "a half-open probe is already in flight; failing fast until it resolves"
    else:
        detail = f"failing fast until {_iso(deadline)} (~{max(0.0, deadline - now):.0f}s)"
    advice = degradation_advice(host, doc, now=now)
    degradation = f" DEGRADATION: {advice}" if advice else ""
    storm = (
        " ATTRIBUTION: likely self-inflicted connection burst — pacing applies; "
        "retrying sooner (short cooldown held while the burst correlates)."
        if doc.get("suspected_cause") == SELF_STORM_CAUSE
        else ""
    )
    err = SshCircuitOpen(
        f"ssh circuit for host '{host}' is OPEN after {failures} consecutive "
        f"connection-level failures (ban-risk protection: refusing to open more "
        f"connections that the cluster's intrusion filter would count); {detail}. "
        f"Override for this host only with {OVERRIDE_ENV}={host}.{storm}{degradation}"
    )
    # Structured context so consumers (harvest_on_terminal's bounded
    # wait-and-retry) never parse the message for the deadline.
    err.host = host
    err.deadline = deadline
    return err


def classify_connection_failure(cp: subprocess.CompletedProcess[str]) -> str | None:
    """The matched connection-level marker for *cp*, or ``None``.

    ``None`` means "the connection reached the host" — a success, an auth
    failure, or a remote command's non-zero exit. Only a returned marker
    counts toward the breaker. (A raised :class:`TimeoutError` never gets
    here; :func:`guarded_call` counts it directly — at this seam a wrapper
    timeout is indistinguishable from a banner-exchange hang, and the
    incident's all-night retries were exactly those timeouts.)

    The exit code decides WHO is speaking before stderr is believed: OpenSSH
    reserves 255 for the ssh CLIENT's own failure (connect / banner / kex)
    and otherwise propagates the REMOTE command's exit status. A non-255
    non-zero exit therefore means the remote command RAN — the transport
    demonstrably worked — and any marker-shaped text in its stderr is remote
    content, never transport evidence (2026-07-19 scheduler-integration
    incident: a dead qmaster made every qsub leg exit 1 with ``error:
    commlib error: got select error (Connection refused)`` in REMOTE stderr
    over a HEALTHY ssh channel; the un-gated marker match counted all three
    as connection-level and opened the circuit). A remote command that
    itself exits 255 is the accepted residual — ssh collapses it onto the
    client's own code — and the marker match below is the remaining guard.
    """
    if cp.returncode == 0:
        return None
    if cp.returncode != 255:
        return None
    blob = ((cp.stderr or "") + "\n" + (cp.stdout or "")).lower()
    for marker in _CONNECTION_FAILURE_MARKERS:
        if marker in blob:
            return marker
    return None


def check_circuit(
    ssh_target: str,
    *,
    clock: Callable[[], float] = time.time,
    probe_fn: Callable[[], subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Gate one ssh attempt to *ssh_target*; raise :class:`SshCircuitOpen` to refuse.

    Fast path (closed / no state / overridden): one lock-free file read, no
    writes. When the circuit is open and the cooldown has ended (its min-wait
    elapsed), this claims the single half-open probe slot under the file lock —
    concurrent claimants keep failing fast (no thundering herd) and callers
    before the min-wait fail fast unchanged. *clock* is injectable for tests
    (wall clock: state is cross-process, so ``time.monotonic`` cannot be shared).

    Demand-driven probe: with no *probe_fn* the caller that returns normally IS
    the probe (its own next command decides the outcome — the historical
    behavior). With a *probe_fn* (a cheap ``ssh true``-class liveness check),
    the claimant runs it INLINE — outside the file lock, through the same
    recording seam — the moment it wins the slot: a success closes the circuit
    and this returns so the caller proceeds; a connection-level failure (marker
    or :class:`TimeoutError`) re-opens at the NEXT cycle's cooldown and raises
    :class:`SshCircuitOpen` so the caller fails fast without gambling an
    expensive real command as the probe.
    """
    host = _host(ssh_target)
    if not host or _overridden(host):
        return
    path = circuit_state_path(host)
    doc = _read_doc(path)
    if doc is None or doc.get("state") != "open":
        return

    from hpc_agent.infra.io import advisory_flock, atomic_write_json

    now = clock()
    probe_claimed = False
    try:
        with advisory_flock(_lock_path(path)):
            # Re-read under the lock: another process may have just closed
            # the circuit or claimed the probe slot.
            doc = _read_doc(path)
            if doc is None or doc.get("state") != "open":
                return
            if effective_state(doc, now=now) == "open":
                raise _open_error(host, doc, now=now, probe_in_flight=False)
            claimed_at = doc.get("probe_claimed_at")
            if claimed_at is not None and now - _float_or(claimed_at, 0.0) < PROBE_CLAIM_TTL_SEC:
                raise _open_error(host, doc, now=now, probe_in_flight=True)
            doc["probe_claimed_at"] = now
            atomic_write_json(path, doc)
            probe_claimed = True
    except OSError:
        # Fail open: a broken lock/state dir must never block SSH.
        return
    if probe_claimed:
        print(
            f"hpc-agent: ssh circuit for {host} half-open — allowing one probe "
            f"(success closes the circuit; failure re-opens at the next cycle's cooldown)",
            file=sys.stderr,
            flush=True,
        )
        if probe_fn is not None:
            _run_demand_probe(ssh_target, host, probe_fn, clock=clock)


def _run_demand_probe(
    ssh_target: str,
    host: str,
    probe_fn: Callable[[], subprocess.CompletedProcess[str]],
    *,
    clock: Callable[[], float],
) -> None:
    """Run the claimed half-open probe INLINE (outside the state-file lock).

    Routes the outcome through the same :func:`record_connection_success` /
    :func:`record_connection_failure` recorders every ssh attempt uses, so a
    failed probe re-opens at the next cycle's cooldown exactly as a real
    command's failure would. On failure this raises :class:`SshCircuitOpen`
    built from the freshly re-opened doc (accurate next-cycle deadline); on
    success it returns and the caller proceeds against a now-closed circuit.
    """
    try:
        cp = probe_fn()
    except TimeoutError as exc:
        record_connection_failure(ssh_target, detail=str(exc), clock=clock)
        raise _reopened_error(host, clock=clock) from exc
    marker = classify_connection_failure(cp)
    if marker is not None:
        stderr_snip = (cp.stderr or "").strip()[:200]
        record_connection_failure(ssh_target, detail=f"{marker}: {stderr_snip}", clock=clock)
        raise _reopened_error(host, clock=clock)
    record_connection_success(ssh_target)


def _reopened_error(host: str, *, clock: Callable[[], float]) -> SshCircuitOpen:
    """The fail-fast envelope for a host whose demand-probe just re-opened it —
    re-read from disk so the deadline reflects the new cycle's cooldown. Fail-open
    (unreadable doc) to a bare :class:`SshCircuitOpen`."""
    doc = _read_doc(circuit_state_path(host))
    if doc is None:
        return SshCircuitOpen(f"ssh circuit for host '{host}' re-opened after a failed probe")
    return _open_error(host, doc, now=clock(), probe_in_flight=False)


def liveness_probe(
    ssh_target: str,
    timeout_sec: float = 15.0,
) -> Callable[[], subprocess.CompletedProcess[str]]:
    """Build the cheap ``ssh <target> true`` half-open demand probe for *ssh_target*.

    The returned zero-arg callable is the production ``probe_fn`` for
    :func:`check_circuit` / :func:`guarded_call`: when the circuit is
    half-open-eligible the claimant runs it INLINE and only a passing probe lets
    the real (possibly expensive) command proceed — without one, every caller
    gambles that real command as the probe. Building the closure is free (no
    import, no I/O), so callers pass it unconditionally: the probe only ever
    RUNS on a claimed half-open slot — zero fast-path cost.

    Two hard boundaries keep the probe safe inside the breaker it serves:

    * It NEVER re-enters :func:`check_circuit` / :func:`guarded_call` — it
      invokes the bounded capture runner
      (:func:`hpc_agent.infra.remote.capture_via_select`, the same seam
      ``ssh_run`` funnels through) DIRECTLY. A nested breaker consult would
      raise probe-in-flight off the very ``probe_claimed_at`` stamp the caller
      just took (:func:`check_circuit`), so recursion is refused structurally,
      not by convention.
    * It takes NO per-host connection slot (:mod:`hpc_agent.infra.ssh_slots`).
      Its single connection is the bounded exception the half-open design
      already makes (:func:`_run_demand_probe`): the claim stamp single-flights
      it fleet-wide, so at most ONE probe connection per host exists at a
      moment — and slot-queueing the probe behind the very callers waiting on
      its verdict could self-deadlock the N-slot cap.

    ``BatchMode=yes`` (via :func:`hpc_agent.infra.ssh_options.ssh_argv`) fails
    fast on a missing key rather than hanging on a prompt; the whole attempt is
    hard-bounded by *timeout_sec*, with ``subprocess.TimeoutExpired`` translated
    to the built-in :class:`TimeoutError` :func:`_run_demand_probe` records as a
    connection failure. An auth failure / remote non-zero exit counts as
    "reached the host" (probe success) under :func:`classify_connection_failure`
    — the same evidence standard every real attempt is held to.
    """

    def _probe() -> subprocess.CompletedProcess[str]:
        # Deferred imports: the probe body only runs on a claimed half-open
        # slot (zero fast-path cost), and ``remote`` imports this module at
        # top level — a module-level import here would be a cycle.
        import subprocess

        from hpc_agent.infra import remote
        from hpc_agent.infra.ssh_options import ssh_argv

        argv = [*ssh_argv("ssh"), ssh_target, "true"]
        try:
            return remote.capture_via_select(argv, timeout=timeout_sec)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"ssh liveness probe to {ssh_target} timed out after {timeout_sec}s"
            ) from exc

    return _probe


def record_connection_failure(
    ssh_target: str,
    *,
    detail: str = "",
    clock: Callable[[], float] = time.time,
) -> None:
    """Record one connection-level failure; open / re-open the circuit as due.

    Closed: increments the consecutive counter; at :data:`CIRCUIT_THRESHOLD`
    the circuit opens at the graduated cycle-1 cooldown (:func:`cooldown_for_cycle`).
    Open with a claimed probe slot: the half-open probe failed — re-open at the
    NEXT cycle's cooldown. Open with NO claimed slot (a straggler that was
    already in flight when a peer opened the circuit): evidence only, no
    escalation — otherwise a concurrent burst would inflate the cooldown
    spuriously.

    Every failure is also stamped into the bounded establishment ring; on an
    open/re-open transition the trailing-window count decides
    ``suspected_cause`` (:func:`_stamp_suspected_cause`), which selects the
    cooldown lane (:func:`_select_cooldown`) — a self-storm holds the short
    cycle-1 lane while it correlates. Disclosure only: the failure/verdict rules
    are untouched.
    """
    host = _host(ssh_target)
    if not host:
        return
    path = circuit_state_path(host)
    opened = reopened = False
    cooldown = BASE_COOLDOWN_SEC
    failures = 0
    degradation = ""
    try:
        from hpc_agent.infra.io import advisory_flock, atomic_write_json

        path.parent.mkdir(parents=True, exist_ok=True)
        with advisory_flock(_lock_path(path)):
            doc = _read_doc(path) or _fresh_doc(host)
            now = clock()
            # Ring EVERY connection-level establishment failure first, so the
            # open/re-open transition below reads a ring that includes this
            # failure (and any recent ones a success reset out of the consecutive
            # counter but did NOT age out of the time window).
            _record_establishment(doc, now=now)
            if doc.get("state") == "open":
                if doc.get("probe_claimed_at") is not None:
                    doc["opened_at"] = now
                    doc["probe_claimed_at"] = None
                    doc["consecutive_failures"] = (
                        int(_float_or(doc.get("consecutive_failures"), 0)) + 1
                    )
                    _bump_cycle(doc, now=now)
                    _stamp_suspected_cause(doc, now=now)
                    cooldown = _select_cooldown(doc)
                    doc["cooldown_sec"] = cooldown
                    reopened = True
            else:
                failures = int(_float_or(doc.get("consecutive_failures"), 0)) + 1
                doc["consecutive_failures"] = failures
                if failures >= CIRCUIT_THRESHOLD:
                    doc["state"] = "open"
                    doc["opened_at"] = now
                    doc["probe_claimed_at"] = None
                    _bump_cycle(doc, now=now)
                    _stamp_suspected_cause(doc, now=now)
                    cooldown = _select_cooldown(doc)
                    doc["cooldown_sec"] = cooldown
                    opened = True
            doc["last_failure"] = {"at": now, "detail": detail[:300]}
            failures = int(_float_or(doc.get("consecutive_failures"), 0))
            if opened or reopened:
                advice = degradation_advice(host, doc, now=now)
                degradation = f" {advice}" if advice else ""
            atomic_write_json(path, doc)
    except OSError:
        return
    if opened or reopened:
        verb = "re-OPENED (half-open probe failed)" if reopened else "OPENED"
        print(
            f"hpc-agent: ssh circuit for {host} {verb} after {failures} consecutive "
            f"connection failure(s) — failing fast for {cooldown:.0f}s to avoid an IP ban. "
            f"Override with {OVERRIDE_ENV}={host}.{degradation}",
            file=sys.stderr,
            flush=True,
        )


def record_connection_success(ssh_target: str) -> None:
    """Reset the breaker after a connection that reached the host.

    Hot-path cheap: a lock-free read decides whether anything needs to
    change; the (locked) write happens only when the counter is nonzero or
    the circuit is open — the steady healthy state costs one file read and
    zero lock traffic.
    """
    host = _host(ssh_target)
    if not host:
        return
    path = circuit_state_path(host)
    doc = _read_doc(path)
    if doc is None:
        return
    if doc.get("state") != "open" and not _float_or(doc.get("consecutive_failures"), 0):
        return
    was_open = False
    try:
        from hpc_agent.infra.io import advisory_flock, atomic_write_json

        with advisory_flock(_lock_path(path)):
            doc = _read_doc(path)
            if doc is None:
                return
            was_open = doc.get("state") == "open"
            # Reset health, but CARRY the incident's open-cycle counter forward:
            # this success may be the deceptive cheap probe that keeps closing the
            # circuit between preamble timeouts (run-13 finding 10). A
            # connection-level success cannot be told apart from a real
            # full-command success at this seam, so it never resets the counter —
            # only INCIDENT_WINDOW expiry (in _bump_cycle) does. A future
            # command-class flag threaded through guarded_call could distinguish
            # them and reset on a genuine command success (deferred).
            fresh = _fresh_doc(host)
            fresh["reopen_cycles"] = int(_float_or(doc.get("reopen_cycles"), 0))
            fresh["incident_started_at"] = doc.get("incident_started_at")
            # CARRY the establishment ring forward too: a deceptive cheap probe
            # that closes the circuit between storm bursts must not erase the
            # storm signal (the run-13 livelock's twin for self-storm attribution)
            # — the ring is time-windowed, so stale entries age out on their own.
            # ``suspected_cause`` stays cleared: a closed circuit has no active
            # cooldown lane; it is re-decided on the next open.
            prior_ring = doc.get("recent_establishments")
            fresh["recent_establishments"] = (
                list(prior_ring) if isinstance(prior_ring, list) else []
            )
            atomic_write_json(path, fresh)
    except OSError:
        return
    if was_open:
        print(
            f"hpc-agent: ssh circuit for {host} closed (probe succeeded)",
            file=sys.stderr,
            flush=True,
        )


def guarded_call(
    ssh_target: str,
    fn: Callable[[], subprocess.CompletedProcess[str]],
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], object] = time.sleep,
    probe_fn: Callable[[], subprocess.CompletedProcess[str]] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one ssh-family attempt under the breaker, rate limiter AND slot cap.

    Consults :func:`check_circuit` BEFORE the attempt (so a retry ladder's
    next rung against an open circuit fails fast instead of proceeding),
    then PACES the new establishment (:mod:`hpc_agent.infra.ssh_pacing` — a
    cross-process per-host token bucket bounding the connection-open RATE, the
    orthogonal axis to the slot cap's concurrency: the run-15 MaxStartups
    burst was sequential fast connects under the cap), then holds one
    cross-process per-host connection slot (:mod:`hpc_agent.infra.ssh_slots`)
    for the whole attempt — every ssh/scp/rsync/tar path funnels through this
    seam (each spawns a fresh outbound process, i.e. a new establishment), so
    detached-worker startup probes and the driving agent's own probes share
    one fleet-wide rate + concurrency cap per host instead of bursting together
    (2026-07-05 + 2026-07-17 incidents). Ordering matters: the breaker gate
    runs first, so an open circuit fails fast without pacing or queueing for a
    slot; the pace runs BEFORE the slot is claimed so a paced wait never hoards
    a concurrency slot (waiters also re-check the breaker each poll). Finally it
    records the outcome: a raised :class:`TimeoutError` or a connection-marked
    ``CompletedProcess`` counts as a connection failure; anything that reached
    the host resets the counter. A slot-wait give-up
    (:class:`hpc_agent.errors.SshSlotWaitTimeout`) is local contention,
    not host evidence — it never counts toward the breaker.

    *probe_fn* (optional) is a cheap ``ssh true``-class liveness check handed to
    :func:`check_circuit`: on a half-open-eligible circuit the claimant runs it
    inline first, and only a passing probe lets *fn* (the real, possibly
    expensive command) proceed — a failed probe re-opens and raises before *fn*.
    """
    from hpc_agent.infra import ssh_pacing, ssh_slots

    check_circuit(ssh_target, clock=clock, probe_fn=probe_fn)
    ssh_pacing.pace_establishment(ssh_target, clock=clock, sleep=sleep)
    with ssh_slots.connection_slot(ssh_target, clock=clock, sleep=sleep):
        try:
            cp = fn()
        except TimeoutError as exc:
            record_connection_failure(ssh_target, detail=str(exc), clock=clock)
            raise
        marker = classify_connection_failure(cp)
        if marker is not None:
            stderr_snip = (cp.stderr or "").strip()[:200]
            record_connection_failure(ssh_target, detail=f"{marker}: {stderr_snip}", clock=clock)
        else:
            record_connection_success(ssh_target)
    return cp
