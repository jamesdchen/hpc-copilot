"""Per-run journal — read-modify-write operations on individual run records.

Composes the layout / locking / atomic-write primitives in
:mod:`.run_record` into the four operations the rest of the framework
calls: :func:`load_run`, :func:`upsert_run`, :func:`update_run_status`,
:func:`mark_run`. Index-side maintenance (refreshing the
``index.json`` cache after each write) lives here too because the
write paths and the index update are paired — splitting them across
modules invited skew when a writer landed but the index update lost
its lock race.

Pure scan / rebuild / query helpers live in :mod:`.index`.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent._kernel.contract.vocabulary import TERMINAL_STATUSES, JournalStatus
from hpc_agent.state.run_record import (
    _UPDATABLE_FIELDS,
    RunRecord,
    _atomic_write_json,
    _locked,
    _read_json,
    _run_path,
    journal_dir,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "load_run",
    "upsert_run",
    "upsert_run_compare_and_mint",
    "update_run_status",
    "update_run_record",
    "mark_run",
    "mark_pending_verdict",
    "clear_pending_verdict",
    "is_held",
    "mark_pending_decision",
    "clear_pending_decision",
    "compare_and_clear_pending_decision",
    "compare_and_repark_pending_decision",
    "read_pending_decision",
    "is_awaiting_decision",
    "is_resubmittable_terminal",
    "is_kill_confirmed",
    "stamp_drive_attempt",
    "stamp_tick",
    "stamp_watchdog_tick",
    "stamp_poll_health",
    "clear_poll_health",
    "mark_seen_by_human",
    "record_kill_request",
    "record_kill_confirmed",
]

# Terminal journal statuses from which a fresh submit should PROCEED rather than
# dedup against the prior record (#276) — every terminal status EXCEPT
# ``complete``. ``complete`` still dedups: re-submitting a finished experiment is
# a replay, not a new run (idempotency). ``timeout`` is deliberately absent — it
# is a LifecycleState (the monitor-flow envelope field), NOT a JournalStatus, so
# a record's status is never ``timeout``: a timed-out run stays ``in_flight`` in
# the journal (its cluster jobs may still be live), which correctly keeps it
# blocking a double-submit.
_RESUBMITTABLE_TERMINAL_STATUSES = TERMINAL_STATUSES - {JournalStatus.COMPLETE}


def load_run(experiment_dir: Path, run_id: str) -> RunRecord | None:
    """Read one run record. Returns ``None`` if missing or schema mismatch."""
    path = _run_path(experiment_dir, run_id)
    payload = _read_json(path)
    if payload is None:
        return None
    # B8: route reader-side check through the cross-domain manifest in
    # hpc_agent._kernel.extension.version. Writer still emits SCHEMA_VERSION;
    # the manifest declares the *supported* range so back-compat is one
    # one-line edit if/when v2 ships.
    from hpc_agent._kernel.extension.version import is_compatible

    found = payload.get("schema_version")
    if not isinstance(found, int) or not is_compatible("session", found):
        warnings.warn(
            f"session: schema_version={payload.get('schema_version')!r} "
            f"unsupported; skipping {path.name}",
            stacklevel=2,
        )
        return None
    try:
        return RunRecord.from_dict(payload)
    except TypeError:
        # A structurally-incomplete v1 record (e.g. an older record
        # written before a now-required field existed, or a truncated
        # file) makes the dataclass constructor raise. ``load_run`` is
        # documented to return None on an unusable record — skip it
        # rather than letting the TypeError escape into callers.
        warnings.warn(
            f"session: run record {path.name} is structurally incomplete; skipping",
            stacklevel=2,
        )
        return None


def upsert_run(experiment_dir: Path, record: RunRecord) -> None:
    """Atomically write the run record and refresh the index entry."""
    path = _run_path(experiment_dir, record.run_id)
    with _locked(path):
        _atomic_write_json(path, record.to_dict())
    _refresh_index_entry(experiment_dir, record.run_id, record.status)


def update_run_status(experiment_dir: Path, run_id: str, **fields: Any) -> RunRecord:
    """Read-modify-write a single run record. Whitelisted fields only."""
    bad = set(fields) - _UPDATABLE_FIELDS
    if bad:
        raise ValueError(f"update_run_status: unknown field(s) {sorted(bad)}")
    path = _run_path(experiment_dir, run_id)
    with _locked(path):
        existing = _read_json(path)
        if existing is None:
            raise FileNotFoundError(f"no run record for {run_id!r}")
        existing.update(fields)
        record = RunRecord.from_dict(existing)
        _atomic_write_json(path, record.to_dict())
    _refresh_index_entry(experiment_dir, record.run_id, record.status)
    return record


def update_run_record(
    experiment_dir: Path,
    run_id: str,
    mutate: Callable[[RunRecord], None],
) -> RunRecord:
    """Locked read-modify-write of a run record via a mutation callback.

    Unlike :func:`update_run_status` — which overwrites whitelisted
    fields with caller-supplied *values* — this reads the record, hands
    the live :class:`RunRecord` to *mutate*, and writes it back, all
    inside the per-run flock. Use it when the new value depends on the
    current on-disk value (e.g. appending to ``combined_waves``): passing
    a snapshot computed from an earlier unlocked ``load_run`` read would
    silently drop a concurrent writer's update.

    Raises :class:`FileNotFoundError` if no record exists for *run_id*.
    """
    path = _run_path(experiment_dir, run_id)
    with _locked(path):
        existing = _read_json(path)
        if existing is None:
            raise FileNotFoundError(f"no run record for {run_id!r}")
        record = RunRecord.from_dict(existing)
        mutate(record)
        _atomic_write_json(path, record.to_dict())
    _refresh_index_entry(experiment_dir, record.run_id, record.status)
    return record


def upsert_run_compare_and_mint(
    experiment_dir: Path,
    run_id: str,
    decide: Callable[[RunRecord | None], RunRecord | None],
) -> tuple[RunRecord, bool]:
    """Locked compare-and-mint — read the existing record and mint atomically.

    Under ONE per-run flock (the critical section that closes the dedup-read →
    mint window), read the current record via :func:`load_run` and hand it (or
    ``None`` if absent) to *decide*:

    * *decide* returns a :class:`RunRecord` → write it atomically, refresh the
      index, and return ``(record, True)`` (minted);
    * *decide* returns ``None`` → the existing record stands; return
      ``(existing, False)`` (not minted);
    * *decide* may RAISE to refuse the mint — the exception propagates with the
      lock released.

    This is the state-layer primitive behind the submit-once single-attempt-in-
    flight invariant (:func:`hpc_agent.ops.submit.runner.mint_submitting_record`,
    premortem Δ1): the dedup read and the mint share ONE lock, so two genuinely
    concurrent same-run_id callers serialize — the first mints, the second reads
    the minted record and *decide* routes it (refuse / dedup). The decision logic
    lives with the caller (ops); only the lock + I/O live here (state).

    The lock is NOT re-entrant (:func:`hpc_agent.infra.io.advisory_flock`), so
    *decide* MUST NOT call back into a locking journal writer for THIS ``run_id``
    (it may read via ``load_run``, which takes no lock).
    """
    path = _run_path(experiment_dir, run_id)
    with _locked(path):
        existing = load_run(experiment_dir, run_id)
        record = decide(existing)
        if record is None:
            if existing is None:
                raise ValueError(
                    "upsert_run_compare_and_mint: decide returned None with no "
                    "existing record — nothing to mint and nothing to return"
                )
            return existing, False
        _atomic_write_json(path, record.to_dict())
    _refresh_index_entry(experiment_dir, record.run_id, record.status)
    return record, True


def _resolve_experiment_dir(experiment_dir: Path | None) -> Path:
    """Journal home for the §5 watchdog / kill setters.

    Their pinned cross-unit signatures deliberately omit ``experiment_dir`` (so
    other units call ``stamp_tick(run_id, last_tick_at=..., next_tick_due=...)``
    directly). The journal is per-experiment-dir, and these setters fire from
    within that dir's context — the campaign driver, the ``doctor``/``kill``
    verbs, and ``drive_once`` all run in it — so ``None`` resolves to the current
    working directory. An explicit dir (``drive_once`` passes its own) overrides.
    """
    return Path(experiment_dir) if experiment_dir is not None else Path.cwd()


def stamp_tick(
    run_id: str,
    *,
    last_tick_at: str,
    next_tick_due: str,
    experiment_dir: Path | None = None,
) -> None:
    """Stamp the driver dead-man's-switch fields on *run_id* (§5 watchdog).

    Records the pace the driver tick chose: ``last_tick_at`` (when this tick
    ran) and ``next_tick_due`` (the absolute deadline by which the next tick
    must run, the caller having derived it from the cadence the tick itself
    picked). A ``next_tick_due`` in the past on a live run is what
    :func:`hpc_agent.state.index.find_stalled_runs` / the ``doctor`` verb read to
    detect a stalled driver. Both are ISO-8601 UTC strings.

    Locked read-modify-write via :func:`update_run_record`; raises
    :class:`FileNotFoundError` if no record exists for *run_id*.
    """

    def _mutate(record: RunRecord) -> None:
        record.last_tick_at = last_tick_at
        record.next_tick_due = next_tick_due

    update_run_record(_resolve_experiment_dir(experiment_dir), run_id, _mutate)


#: The ``BlockDriveAction`` values that mean the tick MOVED the chain. Anything
#: outside this set (``awaiting_decision`` — still waiting on the human;
#: ``skip`` — nothing drivable at this position) moved nothing, and a drain loop
#: that keeps relaying such a tick is spinning. Written out here rather than
#: derived from the ``Literal`` because the partition is a POLICY (which outcomes
#: count as progress), not the vocabulary itself: ``state`` must not import
#: ``_wire``, and a future action word must be classified deliberately rather
#: than defaulting into "progress" and silently disarming retryable(n).
DRIVE_PROGRESS_ACTIONS: frozenset[str] = frozenset(
    {"advanced", "reran", "chained", "detached", "terminal"}
)


def stamp_drive_attempt(
    run_id: str,
    *,
    progressed: bool,
    experiment_dir: Path | None = None,
) -> int:
    """Record one block-drive tick against *run_id*; return the new futile count.

    THE one definition of the retryable(n) counter
    (``docs/plans/run-queue-placement-2026-07-28.md`` §7). *progressed* is
    ``action in`` :data:`DRIVE_PROGRESS_ACTIONS` — the caller classifies, this
    seat only writes, so there is exactly one place the number changes:

    * ``progressed=True`` → reset to 0. A tick that moved the chain has EARNED
      the item a fresh budget; a monotonic counter would eventually refuse to
      drive a perfectly healthy long run, which is worse than the spin it
      guards against.
    * ``progressed=False`` → ``+1``. Consecutive futile ticks are exactly what
      a drain pass must stop relaying.

    Consecutive rather than cumulative, and durable rather than in-plan, is the
    whole point: the count must survive the pass that observed it dying, and a
    relaunched pass must read the SAME number off disk (§7's relaunch-cheapness
    reflex depends on a relaunch losing nothing). ``queue-status`` projects it
    per item as ``drive_attempts``.

    Which spins it actually bounds, traced through the one counting caller
    (``ops/block_drive_op``) and the one policy that reads it (the drain plan's
    ``maxAttempts``): NOT "a genuinely-wedged run" in general. The plan drives
    only ``dispatched ∧ ¬terminal ∧ (¬parked ∨ greenlight_unadvanced)``, so a run
    that parks with no committed greenlight is counted exactly ONCE — by the
    ``awaiting_decision`` tick that parked it — and then frozen out by the
    ¬parked clause until a human answers. The two spins that CAN accumulate are
    the ones with no human in them: a committed-but-unadvanced greenlight that
    re-admits the item every pass while the tick keeps answering
    ``awaiting_decision``, and a drivable item whose tick keeps answering
    ``skip``. State the budget that way when quoting it; "wedged run" promises a
    guard against a case the drivability formula already removes.

    Locked read-modify-write via :func:`update_run_record`; raises
    :class:`FileNotFoundError` if no record exists for *run_id* — a tick that
    drove no run has nothing to count against, and inventing a record here would
    put a journal write on a path that never dispatched anything.
    """

    def _mutate(record: RunRecord) -> None:
        record.drive_attempts = 0 if progressed else int(record.drive_attempts) + 1

    written = update_run_record(_resolve_experiment_dir(experiment_dir), run_id, _mutate)
    return int(written.drive_attempts)


#: Grace (seconds) added to the chosen inter-tick cadence when deriving the
#: watchdog ``next_tick_due`` deadline. A poll merely mid-round-trip at the
#: boundary must not read as a dead driver; a driver that actually dies leaves
#: the last stamp behind and the deadline lapses → the §5 doctor /
#: ``find_stalled_runs`` flags it. THE one definition — both the monitor poll
#: loop and the canary poll loop stamp through :func:`stamp_watchdog_tick`.
_WATCHDOG_DEADLINE_GRACE = 3.0


def stamp_watchdog_tick(
    run_id: str,
    *,
    next_tick_seconds: float,
    experiment_dir: Path | None = None,
) -> None:
    """THE one definition of a driver watchdog liveness stamp (§5 dead-man's switch).

    Computes ``now`` + the next-tick deadline (*next_tick_seconds* +
    :data:`_WATCHDOG_DEADLINE_GRACE`) and records both via :func:`stamp_tick`, so
    the §5 watchdog (in-session timer / out-of-session ``doctor``) covers a dead
    poller via a lapsed ``next_tick_due``. BOTH poll loops route through here —
    ``ops.monitor_flow._stamp_watchdog`` (a thin re-point) and the
    ``ops.verify_canary`` canary loop — so the two can never disagree on what a
    tick means (the #351-#4 "two loops, two definitions" class; a frozen canary
    sidecar false-flagged a stalled driver in proving run #5, finding 12).

    Best-effort and loud: a stamp failure must NEVER break a poll loop (the run
    keeps being monitored), but a missing stamp blinds the watchdog, so warn.
    """
    try:
        from datetime import timedelta

        from hpc_agent.infra.time import utcnow

        _now = utcnow()
        deadline = _now + timedelta(seconds=next_tick_seconds + _WATCHDOG_DEADLINE_GRACE)
        stamp_tick(
            run_id,
            last_tick_at=_now.isoformat(timespec="seconds"),
            next_tick_due=deadline.isoformat(timespec="seconds"),
            experiment_dir=experiment_dir,
        )
    except Exception:  # noqa: BLE001 — a watchdog stamp must never fail the poll loop
        import logging

        logging.getLogger(__name__).warning(
            "watchdog tick stamp failed for run %s — the doctor / find_stalled_runs "
            "cannot see this poller until the next successful stamp",
            run_id,
            exc_info=True,
        )


def stamp_poll_health(
    run_id: str,
    *,
    error_class: str,
    consecutive: int,
    returncode: int | None = None,
    experiment_dir: Path | None = None,
) -> None:
    """Record poll-failure EVIDENCE under ``last_status.poll_health`` (§5, finding 12).

    So ``status-snapshot`` / the doctor render "polling, last N polls rc=127"
    instead of a submit-time timestamp frozen for the whole wait budget while a
    canary poll loop grinds on a deterministic broken-env failure. Written under
    a DISTINCT nested key that the lifecycle classifiers NEVER read
    (:func:`hpc_agent.ops.monitor.classify.settle` /
    :func:`~hpc_agent.ops.monitor.classify.classify_polling` read only the
    ``complete``/``running``/``pending``/``failed``/``unknown`` counts), so this
    evidence can never perturb a complete/failed/abandoned verdict.

    Best-effort and loud, exactly like :func:`stamp_watchdog_tick`.
    """
    try:
        from hpc_agent.infra.time import utcnow_iso

        def _mutate(record: RunRecord) -> None:
            ls = dict(record.last_status or {})
            ls["poll_health"] = {
                "error_class": error_class,
                "consecutive": int(consecutive),
                "returncode": returncode,
                "at": utcnow_iso(),
            }
            record.last_status = ls

        update_run_record(_resolve_experiment_dir(experiment_dir), run_id, _mutate)
    except Exception:  # noqa: BLE001 — evidence stamping must never fail the poll loop
        import logging

        logging.getLogger(__name__).warning(
            "poll-health stamp failed for run %s", run_id, exc_info=True
        )


def clear_poll_health(
    run_id: str,
    *,
    experiment_dir: Path | None = None,
) -> None:
    """Drop a stale ``last_status.poll_health`` block once a poll succeeds again.

    A recovered poll must not leave "polling, last 3 polls rc=127" lingering on
    the record. Best-effort no-op when the key is absent. Mirrors
    :func:`stamp_poll_health`'s never-raise posture.
    """
    try:

        def _mutate(record: RunRecord) -> None:
            if record.last_status and "poll_health" in record.last_status:
                ls = dict(record.last_status)
                ls.pop("poll_health", None)
                record.last_status = ls

        update_run_record(_resolve_experiment_dir(experiment_dir), run_id, _mutate)
    except Exception:  # noqa: BLE001 — evidence clearing must never fail the poll loop
        import logging

        logging.getLogger(__name__).warning(
            "poll-health clear failed for run %s", run_id, exc_info=True
        )


def mark_seen_by_human(
    run_id: str,
    *,
    at: str,
    experiment_dir: Path | None = None,
) -> None:
    """Stamp when the human last looked at *run_id* (§5 attention marker).

    Lets the journal answer "what changed since the human last looked". *at* is
    an ISO-8601 UTC string. Locked RMW via :func:`update_run_record`; raises
    :class:`FileNotFoundError` if no record exists for *run_id*.
    """

    def _mutate(record: RunRecord) -> None:
        record.last_seen_by_human_at = at

    update_run_record(_resolve_experiment_dir(experiment_dir), run_id, _mutate)


def record_kill_request(
    run_id: str,
    *,
    requested_at: str,
    job_ids: list[str],
    experiment_dir: Path | None = None,
) -> None:
    """Journal a kill's INTENT before any scheduler mutation (§5 kill semantics).

    Stamps *requested_at* and the *job_ids* targeted, so a crash mid-kill still
    leaves a durable record of what was asked — the first half of the
    "request → journaled → verified" contract. Locked RMW via
    :func:`update_run_record`; raises :class:`FileNotFoundError` if no record
    exists for *run_id*.
    """

    def _mutate(record: RunRecord) -> None:
        record.kill_requested_at = requested_at
        record.kill_requested_job_ids = list(job_ids)

    update_run_record(_resolve_experiment_dir(experiment_dir), run_id, _mutate)


def record_kill_confirmed(
    run_id: str,
    *,
    confirmed_at: str,
    job_ids: list[str],
    experiment_dir: Path | None = None,
) -> None:
    """Journal the subset of a kill's job_ids VERIFIED gone (§5 kill semantics).

    Stamps *confirmed_at* and the *job_ids* the scheduler confirms are no longer
    known to it — the second half of the "N requested, N confirmed gone" honesty
    contract. Locked RMW via :func:`update_run_record`; raises
    :class:`FileNotFoundError` if no record exists for *run_id*.
    """

    def _mutate(record: RunRecord) -> None:
        record.kill_confirmed_at = confirmed_at
        record.kill_confirmed_job_ids = list(job_ids)

    update_run_record(_resolve_experiment_dir(experiment_dir), run_id, _mutate)


def mark_run(
    experiment_dir: Path,
    run_id: str,
    *,
    status: str,
    stage: str | None = None,
) -> RunRecord:
    """Terminal transition. Updates status (and optionally stage)."""
    # Validate against the canonical JournalStatus StrEnum (B2).
    from hpc_agent._kernel.contract.vocabulary import JournalStatus

    if status not in set(JournalStatus):
        raise ValueError(f"mark_run: invalid status {status!r}")
    path = _run_path(experiment_dir, run_id)
    with _locked(path):
        existing = _read_json(path)
        if existing is None:
            raise FileNotFoundError(f"no run record for {run_id!r}")
        existing["status"] = status
        if stage is not None:
            existing["stage"] = stage
        record = RunRecord.from_dict(existing)
        _atomic_write_json(path, record.to_dict())
    _refresh_index_entry(experiment_dir, record.run_id, record.status)
    return record


def mark_pending_verdict(
    experiment_dir: Path,
    run_id: str,
    *,
    escalation: dict[str, Any],
) -> RunRecord:
    """Park a run on an escalation awaiting a verdict (#231/#234).

    Stores the *escalation* block (an ``Escalation.model_dump()`` dict — the
    state layer stays pure I/O and does not import the ``_wire`` model) in the
    record's ``pending_verdict`` field. The run is now *held*: the deterministic
    resolver could not resolve its failure, so it waits for a verdict instead of
    blocking the campaign loop. The run's ``status`` is left untouched — a
    failed-but-held run stays ``failed`` (so it is not re-monitored as
    ``in_flight``) and is surfaced as parked by
    :func:`hpc_agent.state.index.find_held_runs`.

    The verdict's exit is :func:`clear_pending_verdict` followed by a
    ``resubmit_flow`` with the chosen overrides (resubmit-on-verdict).
    """
    if not escalation:
        raise ValueError("mark_pending_verdict: escalation block must be non-empty")
    return update_run_status(experiment_dir, run_id, pending_verdict=dict(escalation))


def clear_pending_verdict(
    experiment_dir: Path,
    run_id: str,
    *,
    verdict: dict[str, Any] | None = None,
) -> RunRecord:
    """Release a held run once its verdict has been applied (#231/#234).

    Clears ``pending_verdict`` back to ``{}``. Idempotent — clearing a run
    that is not held is a harmless no-op rewrite. The caller is expected to
    have already enacted the verdict (typically a ``resubmit_flow`` with the
    chosen overrides) before releasing the hold.

    When *verdict* is supplied it is appended — in the same locked write that
    releases the hold — to the run's append-only ``verdict_history``, so the
    rationale for the enacted control-flow branch survives the
    ``pending_verdict`` reset (which would otherwise discard it). This is the
    durable record of *why* a non-deterministic decision took the branch it
    did: the audit trail, and the ``source="history"`` recall input the
    deterministic resolver consults before re-escalating the same fingerprint.
    An ``applied_at`` timestamp is auto-stamped when the entry omits one.
    """
    from hpc_agent.infra.time import utcnow_iso

    def _mutate(record: RunRecord) -> None:
        record.pending_verdict = {}
        if verdict:
            entry = dict(verdict)
            entry.setdefault("applied_at", utcnow_iso())
            record.verdict_history = [*record.verdict_history, entry]

    return update_run_record(experiment_dir, run_id, _mutate)


def is_held(record: RunRecord) -> bool:
    """True when *record* is parked on a pending verdict (#231/#234).

    The holding state is the non-emptiness of ``pending_verdict`` — the same
    field-as-state idiom the journal already uses for ``pending_resubmit``.
    A held run is neither live (``in_flight``) nor done from the campaign
    loop's perspective; it is waiting on a decision.
    """
    return bool(record.pending_verdict)


def mark_pending_decision(
    run_id: str,
    *,
    block: str,
    workflow: str,
    brief: dict[str, Any],
    resume_cursor: dict[str, Any],
    awaiting_since: str,
    cmd_sha: str | None = None,
    experiment_dir: Path | None = None,
) -> None:
    """Park *run_id* on a human DECISION at a block's y/nudge boundary (§5).

    The durable "parked ≠ stalled" marker (block-drive.md §5): a driver span
    reached *block*'s decision boundary and is waiting for the human's ``y``/nudge.
    Stores the ``{block, workflow, brief, resume_cursor, awaiting_since, cmd_sha}``
    envelope in the record's ``pending_decision`` field (see
    :class:`~hpc_agent.state.run_record.RunRecord` for the full shape). The §5
    watchdog then reads a non-empty ``pending_decision`` as "awaiting your decision
    since *awaiting_since*" rather than false-alarming a stalled driver, and
    ``resume_cursor`` carries enough for a STATELESS tick (``doctor`` /
    ``block-drive``) to resume the chain.

    This is DISTINCT from :func:`mark_pending_verdict` (parked on an *escalation*
    the deterministic resolver could not act on): a run may legitimately hold on a
    decision without ever having escalated. The run's ``status`` is left untouched.

    Locked read-modify-write via :func:`update_run_status`; raises
    :class:`FileNotFoundError` if no record exists for *run_id*. Mirrors the §5
    watchdog setters' ``experiment_dir=None`` → cwd convention.
    """
    payload: dict[str, Any] = {
        "block": block,
        "workflow": workflow,
        "brief": dict(brief),
        "resume_cursor": dict(resume_cursor),
        "awaiting_since": awaiting_since,
        "cmd_sha": cmd_sha,
    }
    update_run_status(
        _resolve_experiment_dir(experiment_dir),
        run_id,
        pending_decision=payload,
    )


def clear_pending_decision(
    run_id: str,
    *,
    experiment_dir: Path | None = None,
) -> None:
    """Release a run parked on a decision once the driver advances (§5).

    Clears ``pending_decision`` back to ``{}``. Idempotent — clearing a run that
    is not parked is a harmless no-op rewrite. Called when the human answered
    ``y``/nudge and the next driver span consumed the resolved spec, so the run is
    no longer awaiting a decision. Locked RMW via :func:`update_run_status`;
    raises :class:`FileNotFoundError` if no record exists for *run_id*.

    A BLIND clear: it releases whatever marker is on disk. A driver CONSUMING a
    committed greenlight must instead use
    :func:`compare_and_clear_pending_decision`, whose compare-and-swap on
    ``(block, awaiting_since)`` is what keeps two concurrent drivers from both
    running the successor span (§8 S8). This seat stays for the callers that
    genuinely mean "release the hold, whatever it is" (test setup, the hook-side
    completer's post-advance release).
    """
    update_run_status(
        _resolve_experiment_dir(experiment_dir),
        run_id,
        pending_decision={},
    )


def _pending_boundary_identity(marker: Any) -> tuple[str, str] | None:
    """The ``(block, awaiting_since)`` identity of a pending-decision *marker*.

    THE one derivation of "which parked boundary is this marker" — the pair the
    compare-and-swap seats below compare. ``None`` for an absent / empty / non-dict
    marker (the run is not parked), which is itself a legal CAS expectation (the
    empty slot a re-park expects). Missing sub-fields normalize to ``""`` so a
    marker written without an ``awaiting_since`` still compares by value rather
    than raising or silently matching everything.
    """
    if not isinstance(marker, dict) or not marker:
        return None
    return (str(marker.get("block") or ""), str(marker.get("awaiting_since") or ""))


def _swap_pending_decision(
    experiment_dir: Path,
    run_id: str,
    *,
    expected: tuple[str, str] | None,
    payload: dict[str, Any],
) -> bool:
    """Locked compare-and-swap of the ``pending_decision`` slot; True iff it swapped.

    The shared engine under :func:`compare_and_clear_pending_decision` and
    :func:`compare_and_repark_pending_decision` — ONE critical section, the same
    per-run flock + read-modify-write shape :func:`update_run_status` /
    :func:`upsert_run_compare_and_mint` use, so the read of the marker and the
    write that replaces it cannot be split by another driver.

    *expected* is the ``(block, awaiting_since)`` identity the caller READ (or
    ``None`` for "the slot must still be empty"). When the on-disk identity does
    not match, NOTHING is written and the call returns ``False``: the caller lost
    the race (another driver consumed this boundary, or re-parked a new one).

    Raises :class:`FileNotFoundError` if no record exists for *run_id* — the same
    failure :func:`clear_pending_decision` raises for a record-less run, so the
    swap seats are drop-in for the blind writes they replace.
    """
    path = _run_path(experiment_dir, run_id)
    with _locked(path):
        existing = _read_json(path)
        if existing is None:
            raise FileNotFoundError(f"no run record for {run_id!r}")
        if _pending_boundary_identity(existing.get("pending_decision")) != expected:
            return False
        existing["pending_decision"] = dict(payload)
        record = RunRecord.from_dict(existing)
        _atomic_write_json(path, record.to_dict())
    _refresh_index_entry(experiment_dir, record.run_id, record.status)
    return True


def compare_and_clear_pending_decision(
    run_id: str,
    *,
    block: str | None,
    awaiting_since: str | None,
    experiment_dir: Path | None = None,
) -> bool:
    """CONSUME a parked boundary: clear the marker only if it is still the one read.

    The compare-and-swap counterpart of :func:`clear_pending_decision`, and the
    seat the block-drive tick consumes a committed greenlight through
    (``docs/plans/run-queue-placement-2026-07-28.md`` §8 S8). Two drivers may tick
    the same parked run concurrently BY DESIGN (the main session's inline first
    tick and an auto-launched drain pass): both read the same marker pre-clear,
    both see the same committed ``y``, and a blind clear let BOTH run the
    successor span — one clearing, the other re-parking a CONSUMED decision under
    a stale boundary. Comparing ``(block, awaiting_since)`` inside the per-run
    flock makes the consumption atomic: exactly one caller gets ``True``.

    ``False`` means the marker on disk is no longer the ``(block,
    awaiting_since)`` pair the caller read — either another driver already
    consumed it (empty slot) or the boundary moved on (a re-park stamps a new
    ``awaiting_since``; a chained span parks a new ``block``). The loser must NOT
    run the successor span, must NOT re-park, and must NOT raise: the chain
    advanced, just not by this caller.

    Raises :class:`FileNotFoundError` if no record exists for *run_id* (as
    :func:`clear_pending_decision` does).
    """
    return _swap_pending_decision(
        _resolve_experiment_dir(experiment_dir),
        run_id,
        # A consume always expects a PARKED marker: an empty slot (identity
        # ``None``) can never equal this pair, so a caller whose boundary was
        # already consumed correctly loses.
        expected=(str(block or ""), str(awaiting_since or "")),
        payload={},
    )


def compare_and_repark_pending_decision(
    run_id: str,
    *,
    block: str,
    workflow: str,
    brief: dict[str, Any],
    resume_cursor: dict[str, Any],
    awaiting_since: str,
    cmd_sha: str | None = None,
    experiment_dir: Path | None = None,
) -> bool:
    """RE-PARK a marker a failed resume span never consumed — only into an EMPTY slot.

    The compare-and-swap counterpart of :func:`mark_pending_decision` for the F14
    re-park (``block_drive._repark_marker``): the driver cleared the marker before
    the resumed span, the span failed, so the approval was NOT consumed and the
    marker must come back. But a BLIND re-write is the other half of the §8 S8
    double-driver hazard — it can resurrect a boundary another driver has since
    consumed and moved past, re-parking a decision that no longer exists. Expect
    the slot to still be EMPTY: ``False`` means someone else parked a NEWER
    boundary (or re-parked this one) in the meantime, and the newer marker wins.

    Same payload shape as :func:`mark_pending_decision` (one envelope definition);
    raises :class:`FileNotFoundError` if no record exists for *run_id*.
    """
    return _swap_pending_decision(
        _resolve_experiment_dir(experiment_dir),
        run_id,
        expected=None,
        payload={
            "block": block,
            "workflow": workflow,
            "brief": dict(brief),
            "resume_cursor": dict(resume_cursor),
            "awaiting_since": awaiting_since,
            "cmd_sha": cmd_sha,
        },
    )


def read_pending_decision(
    run_id: str,
    *,
    experiment_dir: Path | None = None,
) -> dict[str, Any]:
    """Return *run_id*'s ``pending_decision`` envelope, or ``{}`` if not parked.

    A pure read used by the §5 watchdog / ``doctor`` / ``block-drive`` resume path
    to recover the parked block, brief, and resume cursor. Returns ``{}`` when the
    run is not parked on a decision OR when no record exists (a missing run is not
    parked) — the caller distinguishes the two via :func:`load_run` when it needs
    to.
    """
    record = load_run(_resolve_experiment_dir(experiment_dir), run_id)
    if record is None:
        return {}
    return dict(record.pending_decision)


def is_awaiting_decision(
    run_id: str,
    *,
    experiment_dir: Path | None = None,
) -> bool:
    """True when *run_id* is parked on a human decision (§5 "parked ≠ stalled").

    The holding state is the non-emptiness of ``pending_decision`` — the same
    field-as-state idiom the journal uses for ``pending_verdict`` / ``is_held``. A
    parked-on-decision run is neither live nor terminal from the driver's
    perspective; it is waiting for the human's ``y``/nudge. False when the run is
    not parked or has no record.
    """
    return bool(read_pending_decision(run_id, experiment_dir=experiment_dir))


def is_resubmittable_terminal(record: RunRecord) -> bool:
    """True when *record* is terminal but NOT ``complete`` — i.e. ``failed`` or
    ``abandoned`` (#276).

    Such a record is neither a live run nor a successful one: the monitor reached
    a verdict that the run did not finish cleanly (``abandoned`` = it stopped
    tracking, often after a transient status-probe flake like the Windows
    named-pipe ``getsockname`` failure; ``failed`` = at least one failure with
    nothing left running). Its ``job_ids`` are forensic data, not an in-flight
    marker — so the submit path keys "is this a live run I must dedup / reuse /
    block on?" off this and lets a fresh submit PROCEED, instead of a single
    transient flake (or any prior failure) wedging every future submit for that
    run_id until the user deletes the journal directory.

    Excluded, by design:

    * ``complete`` — a finished experiment still dedups (idempotency: a
      same-``run_id`` resubmit is a replay, not a new run).
    * ``in_flight`` — a live run still blocks (don't double-submit). This is also
      where a *timed-out* run lands: ``timeout`` is a LifecycleState (envelope),
      never a JournalStatus, so a wall-clock-exceeded run whose cluster jobs may
      still be running stays ``in_flight`` and correctly keeps blocking.
    * a *held* run (``pending_verdict``, #231/#234) — parked awaiting a decision,
      even though it is ``failed``. The escalation flow owns its resubmission
      (clear the verdict, then ``resubmit_flow``); a plain submit must not silently
      clobber the hold, so a held run still blocks. (The dedup path protected held
      runs before #276 widened this predicate to ``failed``; preserve that.)
    """
    if is_held(record):
        return False
    return record.status in _RESUBMITTABLE_TERMINAL_STATUSES


def is_kill_confirmed(record: RunRecord) -> bool:
    """True when *record* names a deliberate kill the scheduler CONFIRMED gone.

    A kill is confirmed when ``kill_confirmed_at`` is stamped AND every requested
    scheduler job id is accounted for as gone — ``kill_confirmed_job_ids`` covers
    ``job_ids`` (:func:`hpc_agent.ops.monitor.kill.kill` journals the verified-gone
    subset there). Such a run is terminal from the KILL evidence ALONE: the status
    reporter's per-task counts are irrelevant to a deliberate kill. Reconcile keys
    its reporter-independent terminal short-circuit off this (proving run #5,
    finding 14) so a broken cluster env — which crashes the per-task reporter —
    can no longer strand a kill-confirmed run at ``in_flight``.

    Returns False when no kill was confirmed, the run has no job_ids, or the
    confirmation is only PARTIAL (some requested id is not yet confirmed gone) —
    a partial kill leaves the run live, so it must not settle terminal here.
    """
    if not record.kill_confirmed_at:
        return False
    if not record.job_ids:
        return False
    return set(record.job_ids) <= set(record.kill_confirmed_job_ids)


def _refresh_index_entry(
    experiment_dir: Path,
    run_id: str,
    status: str,
) -> None:
    """Bump a single ``index.json`` entry; called after every successful write.

    Re-reads the run file under the index lock and uses its freshly-read
    status (falling back to *status* only if the per-run file read fails).
    This closes a lost-update race: two writers A and B that each release
    the per-run lock before grabbing the index lock could otherwise install
    A's stale status over B's terminal-transition write.

    If the index read fails (transient OSError, partial-write torn JSON)
    AND the index file exists, we refuse to overwrite — the index will
    self-heal on the next ``_index_is_stale`` rebuild. Previously the
    helper treated a failed read as "treat the entire index as empty,"
    which clobbered every other entry with a single-key dict.
    """
    from hpc_agent.infra.time import utcnow_iso
    from hpc_agent.state.run_record import _read_json as _read_run
    from hpc_agent.state.run_record import _run_path

    idx_path = journal_dir(experiment_dir) / "index.json"
    with _locked(idx_path):
        # Re-read the current status from disk so a concurrent writer's
        # terminal transition can't get clobbered by our stale snapshot.
        run_path = _run_path(experiment_dir, run_id)
        fresh_status = status
        try:
            payload = _read_run(run_path)
        except OSError:  # transient read failure → fall back to caller-supplied value
            payload = None
        if isinstance(payload, dict):
            payload_status = payload.get("status")
            if isinstance(payload_status, str) and payload_status:
                fresh_status = payload_status

        idx_existed = idx_path.exists()
        idx = _read_json(idx_path)
        if idx is None:
            if idx_existed:
                # Read failed on a file that exists — likely transient.
                # Refuse to overwrite the whole index with a one-entry
                # dict; the staleness check will rebuild from per-run
                # files on the next find_in_flight_runs call.
                return
            idx = {}
        if not isinstance(idx, dict):
            return  # corrupt index — same self-heal logic applies
        idx[run_id] = {"status": fresh_status, "updated_at": utcnow_iso()}
        _atomic_write_json(idx_path, idx)
