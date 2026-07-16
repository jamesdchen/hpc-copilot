"""``kill`` — first-class run cancellation (§5 kill semantics).

A ``mutate`` primitive. Given a ``run_id``, it: (1) journals the kill INTENT
before any scheduler mutation (durable even if the process dies mid-kill), (2)
attempts scheduler cancellation *through the backend seam*
(:mod:`hpc_agent.infra.backends`) if a cancel affordance exists, (3) verifies
against the scheduler which requested job IDs are still alive (reusing
:func:`hpc_agent.ops.monitor.reconcile._ssh_alive_job_ids`), (4) journals the
subset verified gone, and (5) reports the honest "N requested, N confirmed gone".

Request → journaled → verified → surfaced. The count never claims more than the
scheduler confirms.

The backend seam exposes a cancel-command builder
(``build_cancel_cmd(job_ids, task_range=None) -> str``): whole-run cancel
(``scancel``/``qdel`` over every id) and — with a ``task_range`` — a range-scoped
PARTIAL cancel (SGE ``qdel <id> -t <range>``, SLURM ``scancel <id>_[<range>]``)
that leaves the array in the queue with its remaining tasks.
:func:`_attempt_backend_cancel` probes the builder off the *class* (never a
concrete backend) and dispatches over the shared SSH transport. A range cancel is
PARTIAL by construction: it never settles the run through reconcile (that is the
full-kill terminal transition), and the honest "N requested, N confirmed gone"
count still comes only from the alive-check verification, never from the cancel
command's exit code.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.kill import KillResult, KillSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.backends.query import _expand_task_range
from hpc_agent.infra.clusters import resolve_ssh_target
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.monitor.reconcile import _ssh_alive_job_ids, reconcile
from hpc_agent.state.journal import load_run, record_kill_confirmed, record_kill_request
from hpc_agent.state.run_record import TERMINAL_STATUSES, RunRecord
from hpc_agent.state.runs import read_job_task_spans


def _range_indices(task_range: str) -> list[int]:
    """Expand the submit-side task_range grammar ('4,8,13-15') into its indices.

    Reuses :func:`hpc_agent.infra.backends.query._expand_task_range` — the SAME
    per-token expander the scheduler ingest uses — so cancel and submit share one
    range vocabulary. Raises :class:`errors.SpecInvalid` on a token the grammar
    cannot parse (``KillSpec`` already rejects a malformed expression, so this is
    the belt-and-suspenders half for a directly-constructed spec).
    """
    out: list[int] = []
    for token in task_range.split(","):
        token = token.strip()
        if not token:
            continue
        expanded = _expand_task_range(token)
        if not expanded:
            raise errors.SpecInvalid(f"kill: unparseable task_range token {token!r}")
        out.extend(expanded)
    return out


def _range_cancel_cmd(
    builder: Any,
    job_ids: list[str],
    task_range: str,
    spans: dict[str, tuple[int, int]],
) -> str | None:
    """Per-job LOCAL-index range cancel for a WAVED run.

    A waved (over-cap, index-bounded) submit lays each batch down as its OWN
    LOCAL ``1-<size>`` array plus a ``TASK_OFFSET`` (``HPCBackend.submit_plan``,
    ``uses_global_array_index=False``), so a job's array subscripts are LOCAL —
    NOT the run-global task ids the ``kill`` range guard validates against
    ``[1, total_tasks]``. Fanning one global range across every wave job as a
    LOCAL subscript (the old ``builder(job_ids, task_range)`` shape) cancels the
    wrong local tasks in the wrong waves (or nothing) — the B4 over-cancel.

    ``job_task_spans`` maps ``job_id`` → 0-based INCLUSIVE ``(first, last)``
    global task-id window — the SAME map :func:`fetch_task_logs` routes log
    probes through. For each requested GLOBAL 1-based index, find the covering
    wave job and translate to its LOCAL 1-based ``ArrayIndex`` (``g0 - first +
    1``, mirroring ``fetch_task_logs``' ``tid - first`` local id). A job ABSENT
    from the map keeps the global index verbatim (single-array / resubmit-global
    replays GLOBAL ids, matching ``fetch_task_logs``' spanless fallback). Each
    job is then cancelled against ONLY its own undone locals via
    :func:`build_cancel_cmd` (which further decomposes the per-job range for
    single-range dialects), the per-job commands sequenced with ``;`` — the SAME
    join ``build_cancel_cmd`` uses for its SGE segments, so a non-zero cancel on
    an already-gone task never aborts a later wave's cancel.

    Returns ``None`` when no job covers any requested index (nothing to dispatch).
    """
    from hpc_agent.ops.recover.batching import compact_task_ids

    spanless = [j for j in job_ids if spans.get(j) is None]
    per_job: dict[str, list[int]] = {}
    for g1 in _range_indices(task_range):
        g0 = g1 - 1  # global 1-based ArrayIndex -> 0-based global task id
        matched = False
        for job_id in job_ids:
            span = spans.get(job_id)
            if span is None:
                continue
            first, last = span
            if first <= g0 <= last:
                per_job.setdefault(job_id, []).append(g0 - first + 1)
                matched = True
                break
        if not matched:
            # No spanned wave covers it — offer it, as its GLOBAL index, to any
            # spanless (global-indexed) job on the run.
            for job_id in spanless:
                per_job.setdefault(job_id, []).append(g1)
    cmds: list[str] = []
    for job_id in job_ids:  # preserve submit order
        locals_ = per_job.get(job_id)
        if not locals_:
            continue
        cmds.append(builder([job_id], compact_task_ids(sorted(set(locals_)))))
    if not cmds:
        return None
    return " ; ".join(cmds)


def _attempt_backend_cancel(
    *,
    scheduler: str,
    ssh_target: str,
    job_ids: list[str],
    task_range: str | None = None,
    job_task_spans: dict[str, tuple[int, int]] | None = None,
) -> tuple[bool, bool]:
    """Attempt scheduler cancellation THROUGH the backend seam, if one exists.

    Returns ``(attempted, available)``. Probes ``build_cancel_cmd`` off the
    *class* (never a concrete backend); a backend that has not migrated the seam
    (no callable builder) reports ``(False, False)`` — the honest no-op half —
    without fabricating a cancel string. When present, builds the command off the
    class and dispatches it over the shared SSH transport. *task_range*, when set,
    scopes the cancel to those array indices (a PARTIAL cancel); ``None`` cancels
    the whole array.

    *job_task_spans*, when supplied for a range cancel of a WAVED run, translates
    each GLOBAL task index to its wave job's LOCAL ``ArrayIndex`` before emitting
    the cancel (see :func:`_range_cancel_cmd`) so each wave cancels only its own
    undone tasks instead of over-cancelling with a global-as-local subscript.
    Absent (``None`` / empty — single-array, ≤cap, or resubmit-global runs) the
    verbatim range fans across the job ids exactly as before.
    """
    if not job_ids:
        return (False, False)
    from hpc_agent.infra.backends import get_backend_class

    backend_cls = get_backend_class(scheduler)
    builder = getattr(backend_cls, "build_cancel_cmd", None)
    if not callable(builder):
        return (False, False)  # no cancel affordance on the seam
    if task_range is not None and job_task_spans:
        cmd = _range_cancel_cmd(builder, job_ids, task_range, job_task_spans)
        if cmd is None:
            # No wave job covers any requested index. The seam IS available;
            # dispatch nothing rather than a global-as-local over-cancel.
            return (False, True)
    else:
        cmd = builder(job_ids, task_range)
    from hpc_agent.infra import remote

    remote.ssh_run(cmd, ssh_target=ssh_target)
    return (True, True)


@primitive(
    name="kill",
    verb="mutate",
    side_effects=[
        SideEffect(
            "writes-journal",
            "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (under flock)",
        ),
        SideEffect("ssh", "<cluster>"),
    ],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable, errors.RemoteCommandFailed],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Kill a run's scheduler jobs: journal the intent, attempt "
            "cancellation through the backend seam (if one exists), verify "
            "against the scheduler, journal the verified-gone subset, and report "
            "'N requested, N confirmed gone'. Request -> journaled -> verified."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        requires_ssh=True,
        spec_model=KillSpec,
        schema_ref=SchemaRef(input="kill"),
    ),
    agent_facing=True,
)
def kill(*, experiment_dir: Path, spec: KillSpec) -> dict[str, Any]:
    """Kill *spec.run_id*'s scheduler jobs and report the honest confirmed count.

    Journals the kill intent BEFORE any scheduler mutation, attempts cancellation
    through the backend seam, verifies which requested job IDs remain alive, and
    journals the subset confirmed gone. When verification cannot run (SSH /
    transport failure) NOTHING is counted as gone — the count never overstates.

    Raises :class:`errors.SpecInvalid` if no journal record exists for the run.
    """
    experiment_dir = Path(experiment_dir)
    record = load_run(experiment_dir, spec.run_id)
    if record is None:
        raise errors.SpecInvalid(f"kill: no journal record for run_id {spec.run_id!r}")
    job_ids = list(record.job_ids)

    # 0. Range guard (the ONE place an out-of-array index is caught). A
    #    range kill cancels only the named array indices; an index outside the
    #    run's array [1, total_tasks] is a request the scheduler cannot honor
    #    (1-based ArrayIndex space, like every scheduler ingest here), so refuse
    #    it BEFORE journaling any intent or touching the scheduler.
    if spec.task_range is not None:
        hi = record.total_tasks
        out_of_range = [i for i in _range_indices(spec.task_range) if i < 1 or i > hi]
        if out_of_range:
            raise errors.SpecInvalid(
                f"kill: task_range index {out_of_range[0]} is outside the array "
                f"[1, {hi}] of run {spec.run_id!r} ({hi} tasks)"
            )

    # 1. Journal the INTENT first — durable even if we die mid-kill (§5).
    requested_at = utcnow_iso()
    record_kill_request(
        spec.run_id,
        requested_at=requested_at,
        job_ids=job_ids,
        experiment_dir=experiment_dir,
    )

    # 2. Attempt cancellation through the backend seam. A ``task_range`` scopes
    #    the cancel to those array indices (a PARTIAL cancel); ``None`` cancels
    #    the whole array.
    #    A waved run stamps per-job GLOBAL task windows in its sidecar; a range
    #    cancel must translate the GLOBAL index to each wave's LOCAL ArrayIndex
    #    (see ``_range_cancel_cmd``). ``read_job_task_spans`` returns ``None`` for
    #    every non-waved run (single array / resubmit-global), leaving the cancel
    #    byte-identical to before. Read spans only for a range cancel.
    cancel_attempted, cancel_available = _attempt_backend_cancel(
        scheduler=spec.scheduler,
        ssh_target=resolve_ssh_target(record),
        job_ids=job_ids,
        task_range=spec.task_range,
        job_task_spans=(
            read_job_task_spans(experiment_dir, spec.run_id)
            if spec.task_range is not None
            else None
        ),
    )

    # 3. Verify against the scheduler: which requested ids are still alive?
    if job_ids:
        try:
            alive = _ssh_alive_job_ids(
                ssh_target=resolve_ssh_target(record), job_ids=job_ids, scheduler=spec.scheduler
            )
        except errors.RemoteCommandFailed:
            # Cannot verify — count NOTHING as gone rather than assume success.
            alive = set(job_ids)
    else:
        alive = set()
    confirmed_gone = [j for j in job_ids if j not in alive]
    still_alive = [j for j in job_ids if j in alive]

    # 4. Journal the verified-gone subset.
    confirmed_at = utcnow_iso()
    record_kill_confirmed(
        spec.run_id,
        confirmed_at=confirmed_at,
        job_ids=confirmed_gone,
        experiment_dir=experiment_dir,
    )

    # 5. Settle a FULL kill through reconcile — the single settle definition.
    #    A FULL kill (everything confirmed gone, nothing still alive) is a
    #    terminal transition, so route it through the ``reconcile`` primitive
    #    rather than harvesting here: reconcile decides the verdict ONCE
    #    (classify.settle), marks the journal terminal — no lingering
    #    ``in_flight`` that would make ``doctor`` emit a spurious "driver
    #    stalled — re-arm?" brief for a deliberately-killed run — and fires the
    #    terminal harvest EXACTLY once (its settle-arm harvest), so kill no
    #    longer double-harvests with reconcile. A PARTIAL kill leaves the run
    #    live and its status untouched: it is still running, and the eventual
    #    real terminal harvests it.
    #
    #    Best-effort: a reconcile failure must NOT mask the kill result just
    #    journaled — log a warning and carry ``settled=False`` on.
    #
    #    ``settled`` reports what reconcile actually DID, never that it merely
    #    returned: reconcile's unable_to_verify path (e.g. an SSH blip on its
    #    OWN alive probe) returns WITHOUT raising while leaving the journal
    #    in_flight, and the envelope contract (KillResult.settled: "journal
    #    marked terminal and the terminal harvest fired") must not claim a
    #    settle that didn't happen — callers would skip the re-reconcile the
    #    run still needs. So derive it from the reconciled record's status.
    #
    #    A RANGE kill (``spec.task_range``) is a PARTIAL cancel by construction:
    #    only some array indices were cancelled and the run keeps its remaining
    #    tasks in flight, so it NEVER settles through reconcile regardless of
    #    what the (job-id-granular) alive check reports.
    settled = False
    if spec.task_range is None and confirmed_gone and not still_alive:
        try:
            settled_record = reconcile(experiment_dir, spec.run_id, scheduler=spec.scheduler)
        except Exception as exc:  # noqa: BLE001 — reconcile is best-effort; never mask the kill
            logging.getLogger(__name__).warning(
                "kill: reconcile settle failed for run %s after a full kill "
                "(the kill result stands): %s",
                spec.run_id,
                exc,
            )
        else:
            settled = (
                isinstance(settled_record, RunRecord) and settled_record.status in TERMINAL_STATUSES
            )

    result = KillResult(
        run_id=spec.run_id,
        requested_job_ids=job_ids,
        confirmed_gone_job_ids=confirmed_gone,
        still_alive_job_ids=still_alive,
        requested_count=len(job_ids),
        confirmed_count=len(confirmed_gone),
        backend_cancel_attempted=cancel_attempted,
        backend_cancel_available=cancel_available,
        summary=f"{len(job_ids)} requested, {len(confirmed_gone)} confirmed gone",
        requested_at=requested_at,
        confirmed_at=confirmed_at,
        settled=settled,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped
