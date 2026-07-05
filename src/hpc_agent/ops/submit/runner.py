"""Submit-time runner primitives."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.contract.vocabulary import JournalStatus
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.submit import SubmitSpec
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state.code_drift import detect_code_drift
from hpc_agent.state.journal import is_resubmittable_terminal, load_run, upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import find_run_by_cmd_sha, read_run_sidecar


def _resolve_current_executor(
    experiment_dir: Path,
    run_id: str,
    current_executor: str | None,
) -> str | None:
    """Resolve the about-to-submit per-task executor command.

    Mirrors the #351 sub-bug #5 resolution in the A5/cmd_sha lane: when the
    caller did not hand us one, read it from THIS run's own sidecar
    (``.hpc/runs/<run_id>.json``) — submit-flow's ``_ensure_run_sidecar``
    writes/validates the real per-task command there before rsync, so it is
    the current intended executor. Best-effort: an unreadable/absent sidecar
    leaves it None (executor-drift detection disabled — param-only fallback).
    """
    if current_executor is not None:
        return current_executor
    try:
        own_sidecar = read_run_sidecar(experiment_dir, run_id)
    except (
        FileNotFoundError,
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        errors.HpcError,
    ):
        return None
    raw_executor = own_sidecar.get("executor")
    return str(raw_executor) if raw_executor else None


def _resolve_current_tasks_py_sha(
    experiment_dir: Path,
    tasks_py_sha: str | None,
) -> str | None:
    """Resolve the about-to-submit code-provenance drift sha.

    Mirrors the A5/cmd_sha lane: when the caller did not hand us one, derive
    it from the on-disk ``<experiment>/.hpc/tasks.py`` — the same source
    ``write_run_sidecar`` stamps onto the sidecar — so even callers that only
    thread ``cmd_sha`` get the drift guard. An unreadable tasks.py disables
    drift detection (param-only fallback).
    """
    if tasks_py_sha is not None:
        return tasks_py_sha
    tasks_py = Path(experiment_dir) / ".hpc" / "tasks.py"
    if not tasks_py.is_file():
        return None
    from hpc_agent.state.run_sha import compute_tasks_py_sha

    try:
        return compute_tasks_py_sha(tasks_py)
    except OSError:
        return None


def _layer1_code_drift(
    existing: RunRecord,
    *,
    current_executor: str | None,
    current_tasks_py_sha: str | None,
) -> tuple[bool, str | None, str | None]:
    """Detect executor / tasks.py drift between a COMPLETE prior run and the
    about-to-submit code, at the LAYER-1 (run_id) dedup gate.

    LAYER 1 keys on ``run_id`` and a COMPLETE prior run dedups (idempotency)
    BEFORE the A5/cmd_sha LAYER-2 scan ever runs — so #207's tasks_py drift
    and #351 sub-bug #5's executor drift, both enforced only in
    :func:`find_run_by_cmd_sha`, are bypassed for the most common
    same-machine "redo this finished run with new code" case (changed
    executor / tasks.py, unchanged swept params → SAME run_id).

    The recorded values come from the PRIOR run's **journal RunRecord**, NOT
    its sidecar. This matters for fireability: an in-place redo with the same
    run_id (params unchanged) re-writes the per-run sidecar at
    ``.hpc/runs/<run_id>.json`` with the NEW executor/tasks_py_sha at Step 6d
    (``ops/resolve_submit_inputs.py`` → ``write_run_sidecar``) BEFORE
    ``submit_and_record`` runs, so by the time this gate fires the sidecar
    already holds the NEW code — reading it would compare new-vs-new and the
    guard could never fire. The journal record, by contrast, is only
    rewritten by ``upsert_run`` AFTER this dedup decision, so it still carries
    the PRIOR run's ``executor`` / ``tasks_py_sha`` (stamped onto it at the
    prior submit) — the one durable signal the redo's sidecar overwrite does
    not destroy.

    Returns ``(drift, recorded_executor, recorded_tasks_py_sha)``. ``drift``
    is True when EITHER the recorded executor or the recorded tasks_py_sha is
    non-empty AND differs from the current — exactly the symmetric drift
    predicate :func:`find_run_by_cmd_sha` applies at layer 2, now a single
    shared definition (:func:`hpc_agent.state.code_drift.detect_code_drift`) so
    a change to the rule can no longer land in one layer and miss the other
    (an empty/absent recorded value is NOT drift; we cannot prove it changed,
    e.g. a pre-#351 record that never stamped these fields).
    """
    drift = detect_code_drift(
        recorded_executor=existing.executor,
        recorded_tasks_py_sha=existing.tasks_py_sha,
        current_executor=current_executor,
        current_tasks_py_sha=current_tasks_py_sha,
    )
    return (drift.drifted, drift.drifted_executor, drift.drifted_tasks_py_sha)


def _warn_layer1_drift(
    run_id: str,
    *,
    recorded_executor: str | None,
    current_executor: str | None,
    recorded_tasks_py_sha: str | None,
    current_tasks_py_sha: str | None,
) -> None:
    """Emit the LAYER-1 drift warning(s) — same shape/wording as the layer-2
    warnings in :func:`find_run_by_cmd_sha`, so a complete-prior dedup that
    replays stale code/executor is now VISIBLE instead of silent."""
    import warnings

    if recorded_executor is not None:
        warnings.warn(
            f"deduping against COMPLETE run {run_id!r} (same run_id, i.e. "
            "identical swept parameters), but its recorded executor command "
            f"({recorded_executor!r}) differs from the current "
            f"({str(current_executor)!r}) — the entry point / executor changed "
            "since that run. The replay is a no-op against the PRIOR "
            "submission's executor (dedup keys on parameters by design, "
            "#207/#351). Pass --invalidate-on-code-change (or set "
            "invalidate_on_code_change=True) to force a fresh run.",
            UserWarning,
            stacklevel=3,
        )
    if recorded_tasks_py_sha is not None:
        warnings.warn(
            f"deduping against COMPLETE run {run_id!r} (same run_id, i.e. "
            "identical swept parameters), but its recorded tasks.py drift sha "
            f"{recorded_tasks_py_sha[:8]}… differs from the current "
            f"{str(current_tasks_py_sha)[:8]}… — the executor code changed "
            "since that run. The replay is a no-op against the PRIOR "
            "submission's code (dedup keys on parameters by design, #207). "
            "Pass --invalidate-on-code-change (or set "
            "invalidate_on_code_change=True) to force a fresh run.",
            UserWarning,
            stacklevel=3,
        )


# Layer-1 (run_id / journal) dedup actions. The LOOKUP (load_run) and the
# SIDE-EFFECTS (warn, return, upsert) stay in submit_and_record; this names the
# DECISION so it is unit-testable without I/O and carries a provenance reason.
_DEDUP = "dedup"  # the existing record stands; do not submit
_PROCEED = "proceed"  # fall through to a fresh / in-place submit
_REFUSE = "refuse"  # a live run on a DIFFERENT cluster — refuse loudly, never dedup


@dataclass(frozen=True)
class _Layer1Decision:
    """What layer-1 (the run_id journal lookup) decided about an existing run.

    ``action`` is ``_DEDUP`` (the call is a no-op replay — return the existing
    record) or ``_PROCEED`` (fall through to a fresh/in-place submit).
    ``reason`` is a stable provenance string. ``warn_drift`` + the recorded
    values are set only on the COMPLETE-redo-with-drift-but-lever-off case, so
    the caller emits the same visible-drift warning as before.
    """

    action: str
    reason: str
    warn_drift: bool = False
    recorded_executor: str | None = None
    recorded_tasks_py_sha: str | None = None


def _resolve_layer1(
    existing: RunRecord,
    *,
    invalidate_on_code_change: bool,
    current_executor: str | None,
    current_tasks_py_sha: str | None,
    current_cluster: str | None = None,
) -> _Layer1Decision:
    """Decide what an existing journal record means for this submit.

    Pure: same RunRecord + inputs always yield the same decision. The branches:

    * ``failed`` / ``abandoned`` (resubmittable-terminal, #276) -> ``_PROCEED``:
      a terminal-failure corpse is not a live run and must not block a retry.
      Terminal-failure proceeds cross-cluster too: redo-in-place is the legit
      recovery, and cluster placement is irrelevant to a dead run.
    * ``in_flight`` (and any other non-terminal, non-complete) on the SAME
      cluster (or when either recorded/current cluster is empty) -> ``_DEDUP``:
      a live run blocks a duplicate submit.
    * ``in_flight`` where BOTH the recorded and the current cluster are
      non-empty AND differ -> ``_REFUSE`` (proving run #5): run_id keys on
      parameters only (#207), so a cluster retarget under the same run_id would
      otherwise silently re-attach this submit to the OLD cluster's in-flight
      canary. One run_id cannot be live on two clusters — refuse loudly. An
      empty recorded/current cluster proves nothing, so it is never a refusal
      (the "cannot prove it changed" precedent, mirroring code-drift).
    * ``complete`` with no code/executor drift -> ``_DEDUP`` (idempotency).
      Complete dedups cross-cluster too: the results already exist, so where
      they were produced is irrelevant to a replay.
    * ``complete`` with drift + ``invalidate_on_code_change`` -> ``_PROCEED``:
      the finished run's code changed, so it is NOT a valid replay; redo in
      place (same run_id, #207).
    * ``complete`` with drift + lever off -> ``_DEDUP`` with ``warn_drift`` so
      the no-op replay against stale code is VISIBLE (#351 sub-bug #5).
    """
    if is_resubmittable_terminal(existing):
        return _Layer1Decision(_PROCEED, "terminal_failure_resubmittable")
    if existing.status != JournalStatus.COMPLETE:
        # A live run on a DIFFERENT cluster is not a duplicate — it is a retarget
        # under a parameter-only run_id (#207). Dedup would re-attach this submit
        # to the OLD cluster's in-flight canary (proving run #5). Refuse, but only
        # when BOTH clusters are known: an empty recorded/current value cannot
        # prove a change (mirrors _layer1_code_drift's "cannot prove it changed").
        if existing.cluster and current_cluster and existing.cluster != current_cluster:
            return _Layer1Decision(_REFUSE, "in_flight_cluster_mismatch")
        return _Layer1Decision(_DEDUP, "in_flight_blocks_duplicate")
    drift, recorded_executor, recorded_tasks_py_sha = _layer1_code_drift(
        existing,
        current_executor=current_executor,
        current_tasks_py_sha=current_tasks_py_sha,
    )
    if not drift:
        return _Layer1Decision(_DEDUP, "complete_idempotent_replay")
    if invalidate_on_code_change:
        return _Layer1Decision(_PROCEED, "complete_code_drift_invalidated")
    return _Layer1Decision(
        _DEDUP,
        "complete_code_drift_warned",
        warn_drift=True,
        recorded_executor=recorded_executor,
        recorded_tasks_py_sha=recorded_tasks_py_sha,
    )


def _submit_spec_handler(ns):  # type: ignore[no-untyped-def]
    """Tier 2 handler — delegates to the hand-written cmd_submit shim.

    The submit-spec primitive's CLI adapter has branching that the
    auto-dispatcher cannot model: a manual required-field check + a
    dry-run path that emits a different envelope shape than the
    success path. The hand-written body lives in
    :mod:`hpc_agent.cli.submit`; this thunk wires it to the registry.
    """
    from hpc_agent.cli.submit import cmd_submit

    return cmd_submit(ns)


@primitive(
    name="submit-spec",
    verb="submit",
    side_effects=[
        SideEffect("writes-journal", "~/.claude/hpc/<repo_hash>/runs/<run_id>.json"),
        SideEffect("scheduler-submit", "<cluster>"),
    ],
    # ``SchedulerThrottled`` was declared phantom — nothing raises it;
    # throttling surfaces as ``RemoteCommandFailed``. Replaced.
    error_codes=[
        errors.SpecInvalid,
        errors.ClusterUnknown,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
    ],
    idempotent=True,
    idempotency_key="spec.run_id",
    cli=CliShape(
        help=(
            "Record a submission in the journal. Idempotent on run_id: "
            "the bundled atomic-ops layer dedups so a retry on transient "
            "network errors does not double-submit."
        ),
        verb="submit",
        requires_ssh=True,
        spec_arg=True,
        spec_model=None,
        spec_required=True,
        experiment_dir_arg=True,
        args=(
            CliArg(
                "--dry-run",
                action="store_true",
                help="Validate the spec and report what would be launched; no SSH/qsub.",
            ),
        ),
        handler=_submit_spec_handler,
    ),
    agent_facing=True,
)
def submit_and_record(
    experiment_dir: Path,
    *,
    spec: SubmitSpec,
    cmd_sha: str | None = None,
    node_sha: str | None = None,
    tasks_py_sha: str | None = None,
    current_executor: str | None = None,
    invalidate_on_code_change: bool = False,
    script: str = "",
    backend: str = "",
    job_env: dict[str, str] | None = None,
    auto_resume_on_kill: bool = False,
    max_auto_resumes: int = 2,
    auto_recover_on_failure: bool = False,
    max_auto_recovers: int = 2,
) -> tuple[RunRecord, bool]:
    """Build a fresh ``RunRecord`` and upsert it to the journal.

    The journal entry is keyed by *run_id* — the per-run sidecar at
    ``.hpc/runs/<run_id>.json`` is the source of truth for everything
    the cluster-side dispatcher and combiner consume; the journal record
    is the laptop-side bookkeeping that lets a future ``/status`` resume
    monitoring without re-asking the user for cluster / job_ids.

    *campaign_id* tags the run as part of a closed-loop campaign so
    :func:`hpc_agent.state.index.find_runs_by_campaign` can pick it up on resume.
    Defaults to an empty string for open-loop submits.

    Returns ``(record, deduped)`` where ``deduped`` is True if a record
    with this ``run_id`` already existed and the call was a no-op replay.
    Submissions are deterministic in ``run_id``, so a retry on transient
    network errors gets dedup for free — the cluster does not see
    duplicate ``qsub``/``sbatch`` calls because the caller checks the
    returned ``deduped`` flag before issuing them.

    *cmd_sha* / *tasks_py_sha* / *invalidate_on_code_change* drive the
    cross-machine (journal-wiped) dedup fallback below. *node_sha* is the
    DAG-lineage refinement of that key: when the run declared parents,
    the caller passes the composed identity from
    :func:`hpc_agent.state.runs.resolve_node_sha` and the fallback keys
    on params AND ancestry instead of bare params. ``cmd_sha`` is
    PARAMETER identity (#207): an executor-body edit with unchanged swept
    params keeps the same ``cmd_sha`` and dedups against the prior run by
    design. Supplying *invalidate_on_code_change* (the opt-in
    ``--invalidate-on-code-change`` lever) folds the run's
    ``tasks_py_sha`` — the code-provenance drift sha — into that dedup
    decision so a code-only change forces a fresh run. When the lever is
    off but a drift is detected, :func:`find_run_by_cmd_sha` emits a
    warning and still dedups (default behaviour is unchanged). When
    *tasks_py_sha* is None it is computed from
    ``<experiment>/.hpc/tasks.py`` (the same source the run sidecar
    records), so callers that already pass ``cmd_sha`` get the drift
    guard for free.

    *current_executor* (#351 sub-bug #5) is the about-to-submit run's
    per-task ``executor`` command. It rides the SAME code-drift lane as
    *tasks_py_sha*: a matched prior sidecar whose recorded ``executor``
    differs warns-and-dedups by default and forces a fresh run under
    *invalidate_on_code_change*. The executor is in NO identity sha
    (``cmd_sha`` stays pure parameter identity, #207), so without this an
    entry-point / executor change with unchanged swept params was a silent
    replay on the PRE-change executor. When None it is read from THIS run's
    own sidecar (``.hpc/runs/<run_id>.json``, written by submit-flow before
    rsync), so callers that already thread ``cmd_sha`` get the executor
    drift guard for free.
    """
    profile = spec.profile
    cluster = spec.cluster
    ssh_target = spec.ssh_target
    remote_path = spec.remote_path
    job_name = spec.job_name
    run_id = spec.run_id
    job_ids = list(spec.job_ids)
    total_tasks = spec.total_tasks
    campaign_id = spec.campaign_id or ""

    # Resolve the about-to-submit code-provenance ONCE — the per-task executor
    # command and the tasks.py drift sha. Both feed three consumers below: the
    # LAYER-1 COMPLETE-redo drift gate, the A5/cmd_sha LAYER-2 lookup, and the
    # journal record we stamp them onto so a FUTURE same-run_id redo can detect
    # drift against this run (the sidecar is overwritten before that gate runs;
    # the journal record is the durable copy). When the caller didn't thread a
    # value, derive it from THIS run's own sidecar / on-disk tasks.py — the same
    # sources submit-flow stamps the sidecar from (see the helper docstrings).
    resolved_executor = _resolve_current_executor(experiment_dir, run_id, current_executor)
    resolved_tasks_py_sha = _resolve_current_tasks_py_sha(experiment_dir, tasks_py_sha)

    # LAYER 1 — run_id / journal dedup. The decision tree (terminal-failure
    # falls through; in_flight blocks — or REFUSES on a cross-cluster retarget,
    # proving run #5; COMPLETE dedups, redoes-in-place under the lever on drift,
    # or warns-and-dedups, #276 / #351 sub-bug #5) lives in the pure
    # ``_resolve_layer1`` so it is unit-testable without I/O. The COMPLETE-redo
    # drift compares the PRIOR run's recorded executor/tasks_py_sha — read from
    # its journal RunRecord, since the same-run_id sidecar is already overwritten
    # with the NEW code by now (see ``_layer1_code_drift``) — against the
    # about-to-submit code; an in-place redo keeps the run_id (#207). The cluster
    # mismatch matters ONLY on the in_flight branch: a run_id keys on parameters
    # alone (#207), so a retarget to a new cluster under the same run_id would
    # otherwise silently re-attach to the OLD cluster's live canary. The
    # side-effects (warn / raise / return / fall-through) stay here.
    existing = load_run(experiment_dir, run_id)
    if existing is not None:
        decision = _resolve_layer1(
            existing,
            invalidate_on_code_change=invalidate_on_code_change,
            current_executor=resolved_executor,
            current_tasks_py_sha=resolved_tasks_py_sha,
            current_cluster=cluster,
        )
        if decision.action == _REFUSE:
            raise errors.SpecInvalid(
                f"run {run_id!r} is already live on cluster "
                f"{existing.cluster!r} (status={existing.status}), but this "
                f"submit targets {cluster!r} — one run_id cannot be live on two "
                "clusters at once. The run_id keys on swept parameters only "
                "(#207), so a cluster retarget under the same run_id would "
                "silently re-attach this submit to the other cluster's live "
                "run. Wait for or kill the live attempt, or make the retarget a "
                "NEW attempt: re-resolve with a distinct run_name and name the "
                "old attempt via supersedes."
            )
        if decision.action == _DEDUP:
            if decision.warn_drift:
                _warn_layer1_drift(
                    run_id,
                    recorded_executor=decision.recorded_executor,
                    current_executor=resolved_executor,
                    recorded_tasks_py_sha=decision.recorded_tasks_py_sha,
                    current_tasks_py_sha=resolved_tasks_py_sha,
                )
            return existing, True
        # _PROCEED: a terminal-failure corpse or an invalidated COMPLETE redo —
        # fall through to a fresh / in-place submit (overwrites the old record +
        # cluster dir); run-id minting is untouched.

    # A5: cmd_sha-based dedup. Covers the case where the journal at
    # ~/.claude/hpc/<repo_hash>/runs/ has been wiped (rm -rf, machine
    # swap) but the per-experiment sidecar at <exp>/.hpc/runs/<id>.json
    # still exists. Without this fallback, submit_and_record would
    # generate a fresh RunRecord and the caller would re-submit a job
    # the cluster already has running.
    #
    # cmd_sha is PARAMETER identity, not code identity (#207). When the
    # caller wants an executor-body edit (unchanged swept params) to be
    # treated as a NEW experiment, it passes invalidate_on_code_change;
    # we fold the current tasks.py drift sha into the lookup. Default
    # path (lever off) is unchanged — find_run_by_cmd_sha still matches
    # on cmd_sha alone and only warns on detected drift.
    if cmd_sha:
        # ``resolved_executor`` / ``resolved_tasks_py_sha`` were resolved once
        # at the top of the function (the same on-disk-sidecar / on-disk-tasks.py
        # sources, with the caller's threaded values taking precedence). Reuse
        # them for the LAYER-2 drift lane so layer 1, layer 2, and the stamped
        # journal record all agree on the about-to-submit code.
        sidecar_path = find_run_by_cmd_sha(
            experiment_dir,
            cmd_sha,
            # DAG lineage (docs/design/dag-kernel.md): when the caller
            # composed a node_sha (params + ancestry), the lookup keys on
            # the effective identity so a parented submit never dedups
            # against a run computed from different/changed parents. None
            # (the default, and every pre-DAG caller) keeps the historical
            # bare-cmd_sha key.
            node_sha=node_sha,
            tasks_py_sha=resolved_tasks_py_sha,
            # #351 sub-bug #5: the executor command rides the same code-drift
            # lane as tasks_py_sha — see find_run_by_cmd_sha. Default path
            # warns + dedups (the change is now VISIBLE); the
            # invalidate_on_code_change opt-in forces a fresh run.
            current_executor=resolved_executor,
            invalidate_on_code_change=invalidate_on_code_change,
            # Campaign iterations deliberately re-run (a stochastic strategy
            # may re-propose identical params), so a same-campaign sidecar is
            # NOT a dedup target — without this a repeated point would
            # silently recover the prior iteration instead of submitting.
            # Empty campaign_id → None → unchanged non-campaign dedup.
            campaign_id=campaign_id or None,
        )
        if sidecar_path is not None:
            existing_run_id = sidecar_path.stem
            sidecar_data = None
            try:
                sidecar_data = read_run_sidecar(experiment_dir, existing_run_id)
            except (
                FileNotFoundError,
                OSError,
                json.JSONDecodeError,
                UnicodeDecodeError,
                errors.HpcError,
            ):
                sidecar_data = None
            if sidecar_data is not None and not (sidecar_data.get("job_ids") or []):
                # Orphan sidecar: written by ``write_run_sidecar`` BEFORE qsub,
                # so ``job_ids`` was never finalized. It is not a completed
                # prior submission, so it must NOT be a dedup target — returning
                # it as ``deduped`` would emit empty ``job_ids`` and fail
                # submit's own output schema (``job_ids`` minItems:1). Fall
                # through to a real submission instead.
                sidecar_data = None
            if sidecar_data is not None:
                # All sidecars produced by the framework are live records
                # we should dedup against — the journal lifecycle has no
                # "cancelled" status, so any historical guard for that
                # value was dead code.
                # ssh_target and job_name are NOT v2 sidecar fields (see
                # _V2_CONFIG_FIELDS in state/runs.py) — they live on the
                # journal RunRecord. The earlier sidecar.get(...) reads
                # for these always returned None and fell through to the
                # caller-supplied args, so the dict-reads were dead.
                reconstructed = RunRecord(
                    run_id=existing_run_id,
                    profile=str(sidecar_data.get("profile") or profile),
                    cluster=str(sidecar_data.get("cluster") or cluster),
                    ssh_target=ssh_target,
                    remote_path=str(sidecar_data.get("remote_path") or remote_path),
                    job_name=job_name,
                    job_ids=list(sidecar_data.get("job_ids") or []),
                    total_tasks=int(sidecar_data.get("task_count") or total_tasks),
                    submitted_at=str(sidecar_data.get("submitted_at") or utcnow_iso()),
                    experiment_dir=str(Path(experiment_dir).resolve()),
                    campaign_id=str(sidecar_data.get("campaign_id") or campaign_id),
                    # Carry the caller's #299 auto-resume keystone (and the
                    # #240 resolve-and-recover opt-in) onto the journal-wiped
                    # reconstruction too (the sidecar does not store them), so a
                    # cross-machine resubmit keeps the opt-in alive instead of
                    # silently reverting to default-OFF.
                    script=script,
                    backend=backend,
                    job_env=dict(job_env or {}),
                    auto_resume_on_kill=auto_resume_on_kill,
                    max_auto_resumes=int(max_auto_resumes),
                    auto_recover_on_failure=auto_recover_on_failure,
                    max_auto_recovers=int(max_auto_recovers),
                    # #351 layer-1 companion: mirror the matched sidecar's
                    # executor / tasks_py_sha onto the reconstructed journal
                    # record so a FUTURE same-run_id redo can detect drift
                    # against it (the sidecar will have been overwritten by
                    # then; the journal record is the durable copy). The
                    # journal-wiped sidecar IS the prior run's record here, so
                    # its own values are the right provenance to carry forward.
                    executor=str(sidecar_data.get("executor") or ""),
                    tasks_py_sha=str(sidecar_data.get("tasks_py_sha") or ""),
                )
                # Repair the journal so future load_run calls hit it
                # directly without re-doing the cmd_sha scan.
                upsert_run(experiment_dir, reconstructed)
                return reconstructed, True

    record = RunRecord(
        run_id=run_id,
        profile=profile,
        cluster=cluster,
        ssh_target=ssh_target,
        remote_path=remote_path,
        job_name=job_name,
        job_ids=list(job_ids),
        total_tasks=int(total_tasks),
        submitted_at=utcnow_iso(),
        experiment_dir=str(Path(experiment_dir).resolve()),
        campaign_id=campaign_id,
        # #299 auto-resume keystone: the inputs a monitor-side auto-resume
        # re-submits *with*, plus the opt-in policy + cap. Empty/False
        # defaults mean a caller that does not thread these gets the
        # zero-blast-radius baseline (auto-resume never fires).
        script=script,
        backend=backend,
        job_env=dict(job_env or {}),
        auto_resume_on_kill=auto_resume_on_kill,
        max_auto_resumes=int(max_auto_resumes),
        # #240 resolve-and-recover opt-in + cap, mirroring the auto-resume
        # keystone above. Default-OFF: a caller that does not thread these gets
        # the zero-blast-radius baseline (resolve-and-recover never auto-acts).
        auto_recover_on_failure=auto_recover_on_failure,
        max_auto_recovers=int(max_auto_recovers),
        # #351 layer-1 companion: the durable record of WHAT this run actually
        # ran. The sidecar carries these too, but a future same-run_id in-place
        # redo overwrites the sidecar with its NEW code BEFORE the layer-1
        # COMPLETE-dedup gate reads it — so only this journal copy lets that
        # redo detect drift against the run we are recording now (see
        # ``_layer1_code_drift``). Empty strings when unresolved → "cannot prove
        # drift" (never a false invalidation).
        executor=resolved_executor or "",
        tasks_py_sha=resolved_tasks_py_sha or "",
    )
    upsert_run(experiment_dir, record)

    # Stamp an INITIAL watchdog deadline the moment the in_flight record lands,
    # so a run whose driver dies BEFORE its first monitor tick is still
    # detectable as stalled (§5). Without it, ``next_tick_due`` stays unset until
    # the first tick and :func:`hpc_agent.state.index.find_stalled_runs`
    # permanently skips a never-ticked run — an undetectable stall. INITIAL_GRACE
    # reuses the driver's fallback cadence
    # (``_kernel.lifecycle.drive._DEFAULT_DRIVER_TICK_CADENCE_SECONDS``): the
    # deadline by which the first real tick must have landed. Best-effort and
    # loud, mirroring ``drive._stamp_driver_tick``: a stamp failure must never
    # fail the submit, but a missing stamp blinds the watchdog, so warn.
    try:
        from datetime import timedelta

        from hpc_agent._kernel.lifecycle.drive import _DEFAULT_DRIVER_TICK_CADENCE_SECONDS
        from hpc_agent.infra.time import utcnow
        from hpc_agent.state.journal import stamp_tick

        _now = utcnow()
        stamp_tick(
            run_id,
            last_tick_at=_now.isoformat(timespec="seconds"),
            next_tick_due=(
                _now + timedelta(seconds=_DEFAULT_DRIVER_TICK_CADENCE_SECONDS)
            ).isoformat(timespec="seconds"),
            experiment_dir=experiment_dir,
        )
    except Exception:  # noqa: BLE001 — the initial stamp must never fail the submit
        import logging

        logging.getLogger(__name__).warning(
            "initial watchdog stamp failed for run %s — the doctor / "
            "find_stalled_runs cannot see this driver until a tick lands "
            "next_tick_due",
            run_id,
            exc_info=True,
        )

    # Post-qsub finalize: stamp the per-experiment sidecar with the job_ids
    # we just got back. This is what distinguishes a real run from the
    # half-baked sidecar Step 6d of /submit-hpc writes before rsync — see
    # :func:`hpc_agent.state.runs.is_orphan_sidecar`.
    #
    # The per-exp sidecar at ``.hpc/runs/<run_id>.json`` is what the
    # cluster-side dispatcher hard-requires (it reads ``executor`` +
    # ``result_dir_template`` from it). The journal record alone deflects
    # the *local* orphan check, but the cluster will fail every task if
    # the sidecar never shipped. A missing sidecar here therefore is NOT
    # a benign no-op — warn loudly so the caller skipping Step 6d /
    # wrap-entry-point sees it instead of discovering it only when every
    # cluster task dies with "run sidecar not found".
    try:
        from hpc_agent.state.runs import update_run_sidecar_job_ids

        update_run_sidecar_job_ids(experiment_dir, run_id, list(job_ids))
    except FileNotFoundError:
        import warnings

        warnings.warn(
            f"per-run sidecar .hpc/runs/{run_id}.json was not found when "
            "finalizing job_ids — the cluster dispatcher requires it "
            "(executor + result_dir_template) and every task will fail "
            "with 'run sidecar not found' if it does not ship. Ensure "
            "write_run_sidecar (Step 6d / wrap-entry-point) ran before "
            "submission.",
            UserWarning,
            stacklevel=2,
        )
    return record, False


def build_job_env(runtime_spec: dict[str, Any], base_env: dict[str, str]) -> dict[str, str]:
    """Return *base_env* augmented with runtime-derived env vars.

    *runtime_spec* is a small dict carrying any runtime selector the
    caller wants threaded into the cluster job — typically
    ``{"runtime": "uv"}`` taken from the submit-spec. When
    ``runtime_spec.get("runtime") == "uv"``, sets ``HPC_RUNTIME=uv`` so
    the cluster-side template's ``uv sync`` preamble fires. Any other
    value (or an empty dict) returns a plain copy of *base_env*. Never
    mutates either input.

    Add new branches as new runtime profiles land (``pixi``, ``poetry``,
    …); the contract — copy + augment — should stay invariant.
    """
    env = dict(base_env)
    if runtime_spec.get("runtime") == "uv":
        env["HPC_RUNTIME"] = "uv"
    return env
