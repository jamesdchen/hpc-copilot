"""Reconcile + mark-terminal runner primitives."""

from __future__ import annotations

import dataclasses
import json
import shlex
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.contract.vocabulary import LifecycleState
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra import remote
from hpc_agent.infra.backends import backend_requires_ssh
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.monitor.classify import settle
from hpc_agent.ops.monitor.status import _ssh_status_report
from hpc_agent.state.journal import load_run, mark_run, update_run_status

if TYPE_CHECKING:
    from hpc_agent.state.run_record import RunRecord


@dataclasses.dataclass(frozen=True)
class OrphanedReconcile:
    """Benign verdict for a crashed-submit residue, NOT a corruption (#356).

    A run whose per-experiment sidecar exists, is valid JSON, carries
    **no** ``job_ids``, AND has **no** journal record is crashed-submit
    *residue* — ``submit-flow`` wrote the jobless sidecar (Step 6d) but the
    process died before the post-qsub ``submit_and_record`` minted the journal
    record and stamped the ids. It never reached the scheduler, so there is no
    run to reconcile; the sidecar is safe to discard or overwrite by a fresh
    submit (which the cmd_sha dedup in ``runner.submit_and_record`` already
    treats as an orphan and falls through, #356 AC2).

    This is the SAME invariant :func:`hpc_agent.state.runs.is_orphan_sidecar`
    keys on (jobless sidecar + no committed journal). Surfacing it as a
    benign envelope — not a :class:`errors.JournalCorrupt` — is the whole
    point: the operator no longer has to hand-``rm`` the residue before
    re-submitting.

    Deliberately NOT an orphan, still :class:`errors.JournalCorrupt` (#328 —
    the hint must never mask a real corruption):

    * sidecar carries ``job_ids`` but no journal record → stranded post-qsub
      ids; the operator must mint the record from THOSE ids (see the hint in
      :func:`_reconcile_one`).
    * sidecar missing, malformed, or schema-incompat → unreadable on-disk state.
    """

    run_id: str


def _ssh_list_combined_waves(*, ssh_target: str, remote_path: str) -> list[int]:
    """Derive ``combined_waves`` from cluster artifacts.

    The combiner writes ``_combiner/wave_<N>.json`` per successful run
    (see ``hpc_agent/execution/mapreduce/combiner.py``). We use the
    presence of that file as the success marker.
    """
    cmd = f"cd {shlex.quote(remote_path)} && ls _combiner/wave_*.json 2>/dev/null || true"
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    if proc.returncode != 0:
        # SSH transport failure (rc 255) — not "no waves combined yet",
        # which returns rc 0 thanks to the trailing ``|| true``. Raise so
        # reconcile keeps the journal's combined_waves instead of
        # overwriting it with an empty list on a connectivity blip.
        raise errors.RemoteCommandFailed(
            f"combined-wave list failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    waves: set[int] = set()
    for line in proc.stdout.splitlines():
        name = Path(line.strip()).name  # wave_<N>.json
        if not (name.startswith("wave_") and name.endswith(".json")):
            continue
        try:
            waves.add(int(name.removeprefix("wave_").removesuffix(".json")))
        except ValueError:
            continue
    return sorted(waves)


def _ssh_alive_job_ids(*, ssh_target: str, job_ids: list[str], scheduler: str) -> set[str]:
    """Return the subset of *job_ids* still known to the scheduler.

    "Alive" means *currently* known to the scheduler (queued, running,
    requeued).  Slurm's ``sacct`` reports historical jobs too — completed,
    cancelled, failed — so we deliberately skip it here; ``squeue``
    alone covers pending+running+requeued, which is what callers actually
    want when deciding whether a run has been abandoned.

    B5-PR2: the per-scheduler shell-command shape and the per-scheduler
    output parser both live on the backend class
    (``build_alive_check_cmd`` / ``parse_alive_output``); this function
    is now transport (SSH) only.
    """
    if not job_ids:
        return set()
    from hpc_agent.infra.backends import get_backend_class

    backend_cls = get_backend_class(scheduler)
    cmd = backend_cls.build_alive_check_cmd(job_ids)
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    if proc.returncode != 0:
        # SSH transport failure (rc 255), not "scheduler ran, found
        # nothing alive" — the alive-check commands append ``|| true``
        # so a reachable cluster always returns rc 0. Raise so
        # reconcile's guard sets alive_check_failed and does NOT mark a
        # healthy run abandoned on a connectivity blip.
        raise errors.RemoteCommandFailed(
            f"alive check failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    return backend_cls.parse_alive_output(proc.stdout, job_ids)


# The settle-path completion/failure predicates and their precedence now live
# in :mod:`hpc_agent.ops.monitor.classify` (``all_tasks_complete`` /
# ``run_failed`` / ``settle``) so the count-to-verdict rule has a single home
# shared with the monitor poll loop. ``_reconcile_one`` applies them via
# :func:`settle`, which also returns the provenance recorded in
# ``last_status.verdict_reason``.


def _gather_failure_features(
    *,
    ssh_target: str,
    remote_path: str,
    job_name: str,
    job_ids: list[str],
    scheduler: str,
) -> dict[str, Any]:
    """Fetch the failed run's cluster log tail for the envelope.

    A reconcile that routes to ``failed`` carries the readable cluster log tail
    (the ``exit_code``/traceback that proves this is a FAILURE, not a purge) so
    the operator sees the evidence inline instead of hand-fetching. Shape matches
    the ``failure_features`` ``verify_canary`` attaches — ``cluster_log_tail`` /
    ``log_path`` / ``classified_error``. The signature classifier now lives in
    ``infra.failure_signatures`` (shared substrate the cross-subject boundary
    lint allows), so reconcile classifies the tail inline just like
    ``verify_canary`` does — same ``error_class`` / ``suggested_fix`` /
    ``matched_pattern`` triple. Routes only through ``infra.*`` (allowed
    substrate: ``cluster_logs`` for the fetch, ``failure_signatures`` for the
    classify).

    Best-effort: an SSH blip fetching the log degrades to an empty tail and a
    ``None`` classification, and a ``classify`` failure likewise degrades to
    ``None`` — neither gates the verdict. The ``failed`` verdict still stands on
    the reporter's positive ``failed`` count, which already proved
    non-completion. Never raises.
    """
    from hpc_agent.infra.cluster_logs import fetch_task_logs
    from hpc_agent.infra.failure_signatures import classify

    stderr_tail = ""
    log_path: str | None = None
    try:
        logs = fetch_task_logs(
            ssh_target=ssh_target,
            remote_path=remote_path,
            job_name=job_name,
            job_ids=job_ids,
            scheduler=scheduler,
            task_ids=[0],
            lines=50,
        )
        if logs and isinstance(logs[0], dict):
            stderr_tail = str(logs[0].get("content") or "")
            raw_path = logs[0].get("path")
            log_path = raw_path if isinstance(raw_path, str) else None
    except Exception:  # noqa: BLE001 — log fetch is best-effort, never gates the verdict
        stderr_tail = ""
        log_path = None

    classified_error: dict[str, Any] | None = None
    if stderr_tail:
        try:
            classified_error = classify(stderr_tail, exit_code=None)
        except Exception:  # noqa: BLE001 — classify is best-effort, never gates the verdict
            classified_error = None

    return {
        "cluster_log_tail": stderr_tail,
        "log_path": log_path,
        "classified_error": classified_error,
    }


def _reconcile_envelope(record: RunRecord | OrphanedReconcile) -> dict[str, Any]:
    """Project the reconcile result into the ``reconcile.output.json`` shape.

    The envelope ``lifecycle_state`` is the journal status, EXCEPT when the
    cluster alive-check could not run (SSH/auth/network failure): the journal
    status is left untouched (we couldn't verify it), but the envelope surfaces
    ``unable_to_verify`` (#258) so callers can distinguish "cluster says it's
    still running" from "we couldn't ask" — different remediations. The marker
    lives in ``last_status.verify_state`` (set by :func:`_reconcile_one`).

    A ``failed`` verdict (#351 sub-bug #4 — the reporter showed positive
    ``failed >= 1`` task evidence) carries the classified error in
    ``last_status.failure_features`` (same shape ``verify_canary`` attaches),
    set by :func:`_reconcile_one` before it marks the run terminal-``failed``.
    The skill's ``failed`` branch surfaces that instead of mapping the run to a
    misleading ``run_abandoned``.

    A benign :class:`OrphanedReconcile` (#356) projects to the terminal-ish
    ``no_run_record`` state. ``last_status`` carries the ``orphaned`` verdict
    plus an actionable ``next_step`` — there is no cluster reading because the
    run never reached the scheduler. It is NOT an error envelope: the caller may
    discard/overwrite the residue and proceed with a fresh submit.
    """
    if isinstance(record, OrphanedReconcile):
        return {
            "run_id": record.run_id,
            "lifecycle_state": "no_run_record",
            "combined_waves": [],
            "failed_waves": [],
            "last_status": {
                "verdict": "orphaned",
                "next_step": (
                    "Crashed-submit residue: a valid jobless sidecar with no "
                    "journal record. Nothing reached the scheduler — proceed "
                    "with a fresh submit (it discards/overwrites the orphan; "
                    "the runner's cmd_sha dedup treats it as an orphan and "
                    "falls through). No manual rm required; "
                    "prune-orphan-sidecars cleans it up."
                ),
            },
        }
    last_status = record.last_status or {}
    state = record.status
    if last_status.get("verify_state") == "unable_to_verify":
        state = "unable_to_verify"
    return {
        "run_id": record.run_id,
        "lifecycle_state": state,
        "combined_waves": record.combined_waves,
        "failed_waves": record.failed_waves,
        "last_status": record.last_status,
    }


def _sibling_run_ids(run_id: str) -> list[str]:
    """Paired journal entries that share this submit's ``cmd_sha`` (#258).

    Every ``submit-flow`` writes TWO entries — the main run and its
    ``<run_id>-canary`` sibling — submitted together with one outcome. Reconcile
    must settle both in one call, or the next ``/submit-hpc`` is blocked by the
    untouched canary entry. The pairing is the ``-canary`` suffix; given either
    half, return the other.
    """
    suffix = "-canary"
    if run_id.endswith(suffix):
        return [run_id[: -len(suffix)]]
    return [f"{run_id}{suffix}"]


@primitive(
    name="reconcile-journal",
    verb="mutate",
    side_effects=[
        SideEffect(
            "writes-journal",
            "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (under flock)",
        ),
        SideEffect("ssh", "<cluster>"),
    ],
    # ``ClusterUnknown`` was declared but is never raised in this
    # primitive's body — kept here so callers' retry policy continues
    # to recognise it if a future change introduces the raise.
    error_codes=[errors.SshUnreachable, errors.ClusterUnknown, errors.JournalCorrupt],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        verb="reconcile",
        requires_ssh=True,
        experiment_dir_arg=True,
        args=(
            CliArg(flag="--run-id", required=True),
            CliArg(
                flag="--scheduler",
                required=True,
                # No static ``choices``: the valid set is the live backend
                # registry (built-ins + plugin backends), not a frozen list
                # (#337). ``get_backend_class`` in ``_ssh_alive_job_ids`` fails
                # loud (``SpecInvalid``) on an unregistered name.
                help="Backend name — needed to query alive job IDs.",
            ),
        ),
        result_post=_reconcile_envelope,
        help="Re-derive ground truth from the cluster (status, waves, alive jobs).",
    ),
    agent_facing=True,
)
def reconcile(
    experiment_dir: Path,
    run_id: str,
    *,
    scheduler: str,
    file_glob: str = "*",
) -> RunRecord | OrphanedReconcile:
    """Self-healing resume step — reconciles *run_id* AND its paired sibling.

    Re-derives ground truth from the cluster for *run_id* (see
    :func:`_reconcile_one`), then CASCADES to its ``-canary`` / parent sibling
    (#258) so one ``reconcile`` call settles both paired journal entries — a
    bare main-run reconcile used to leave the canary entry ``in_flight`` and
    block the next submit. Only non-terminal siblings are re-checked; the
    outcomes are recorded under the returned record's
    ``last_status.reconciled_siblings`` for visibility.

    Returns the requested run's reconciled record. Its envelope
    ``lifecycle_state`` becomes ``unable_to_verify`` when the cluster
    alive-check could not run (#258), or ``no_run_record`` when the run is a
    benign crashed-submit orphan (#356) — an :class:`OrphanedReconcile` is
    returned in that case, with no sibling cascade (there is no record).
    """
    from hpc_agent.state.run_record import TERMINAL_STATUSES

    primary, _primary_alive_failed = _reconcile_one(
        experiment_dir, run_id, scheduler=scheduler, file_glob=file_glob
    )

    # A benign orphan (#356) has no journal record to cascade FROM or merge the
    # sibling outcomes INTO — update_run_status on a non-existent record would
    # raise FileNotFoundError. There is nothing to settle: return the benign
    # verdict directly. (A genuine paired run always lands a record first.)
    if isinstance(primary, OrphanedReconcile):
        return primary

    sibling_outcomes: list[dict[str, Any]] = []
    for sib_id in _sibling_run_ids(run_id):
        sib = load_run(experiment_dir, sib_id)
        if sib is None:
            continue  # no paired entry — nothing to cascade to
        if sib.status in TERMINAL_STATUSES:
            # Already settled; report it but don't pay another SSH round-trip.
            sibling_outcomes.append(
                {"run_id": sib_id, "lifecycle_state": sib.status, "reconciled": False}
            )
            continue
        sib_rec, _ = _reconcile_one(
            experiment_dir, sib_id, scheduler=scheduler, file_glob=file_glob
        )
        # A sibling can itself be a benign orphan — surface no_run_record
        # rather than reaching for a record attribute it does not have.
        sib_state = "no_run_record" if isinstance(sib_rec, OrphanedReconcile) else sib_rec.status
        sibling_outcomes.append(
            {"run_id": sib_id, "lifecycle_state": sib_state, "reconciled": True}
        )

    if sibling_outcomes:
        merged = {**(primary.last_status or {}), "reconciled_siblings": sibling_outcomes}
        primary = update_run_status(experiment_dir, run_id, last_status=merged)
    return primary


def _reconcile_one(
    experiment_dir: Path,
    run_id: str,
    *,
    scheduler: str,
    file_glob: str = "*",
) -> tuple[RunRecord | OrphanedReconcile, bool]:
    """Reconcile a single run against the cluster; return ``(record, alive_check_failed)``.

    Returns an :class:`OrphanedReconcile` (#356) in place of the record when the
    run is crashed-submit residue: a valid jobless sidecar with no journal
    record. That verdict is benign — there is no cluster round-trip because the
    run never reached the scheduler — and ``alive_check_failed`` is ``False``.

    Re-derives ground truth from the cluster:
      A. Fresh status report -> ``last_status``.
      B. List ``_combiner/wave_*.json`` -> canonical ``combined_waves``
         (cluster wins; journal overwritten on drift).
      C. Cross-check ``job_ids`` against the scheduler; if zero are alive,
         route the verdict by the reporter's per-task evidence — ``complete``
         (all tasks complete), ``failed`` (positive ``failed >= 1`` evidence,
         #351), or ``abandoned`` (no evidence on disk at all), never a blind
         flip to ``"abandoned"``.

    All three SSH calls run concurrently. Writes the reconciled record
    back atomically.

    Two failure modes route through ``unable_to_verify`` instead of
    abandoned (the journal status is left untouched in both):

    - **Alive-check SSH failure** (#258 — the original case). We couldn't
      ask the scheduler whether the job is alive; treating "no alive jobs
      returned" as confirmed-dead would mark a healthy run abandoned on a
      connectivity blip.
    - **Status reporter SSH failure** (0.10.12). When the cluster-side
      reporter can't run — e.g. the reconcile shells under bare ``python``
      because the activation prefix wasn't threaded through (pre-0.10.12
      bug) — we can't confirm whether results exist on disk. Routing
      through ``abandoned`` would mask a "completed-but-reporter-broken"
      run. The ``record_status`` / monitor path already threads
      ``remote_activation_for_sidecar`` (see ``ops/monitor/status.py``);
      reconcile now does the same.

    The bool return mirrors ``alive_check_failed`` (kept for
    backward-compat with ``reconcile``'s caller; the reporter-failed
    signal lives in ``last_status.verify_state``).
    """
    # Activate the run's cluster env (conda/modules) for the control-plane
    # reporter — same shape as record_status (ops/monitor/status.py:109-125).
    # Without this, the reporter shells under the login node's bare
    # /usr/bin/python which has no hpc_agent → the cluster-side reduce
    # module fails to import → reporter raises RemoteCommandFailed → the
    # verdict (pre-0.10.12) silently routed through abandoned because only
    # alive_check_failed gated unable_to_verify.
    from hpc_agent.infra.clusters import remote_activation_for_sidecar
    from hpc_agent.state.runs import read_run_sidecar

    record = load_run(experiment_dir, run_id)
    if record is None:
        # No journal record. Three sub-cases, distinguished by the sidecar
        # (#356) — and the #328 invariant holds throughout: the benign branch
        # only fires on a PROVABLY benign read, never by masking a failed one.
        #
        #   (a) sidecar valid JSON + NO job_ids  → benign ``orphaned`` residue.
        #       submit-flow wrote the jobless sidecar (Step 6d) but the process
        #       died before submit_and_record minted the record + stamped ids,
        #       so the run never reached the scheduler. Same invariant as
        #       ``is_orphan_sidecar``. Report a benign verdict, not a corruption
        #       — the operator no longer hand-``rm``s residue before re-submitting.
        #   (b) sidecar valid JSON WITH job_ids  → stranded post-qsub ids; the
        #       process died after qsub pre-stamped them (empirical 2026-06-11
        #       demo). Name the REAL ids so the caller mints the record from
        #       THEM via `hpc-agent submit-spec --spec <file>`, never an invented
        #       placeholder (submit-spec refuses non-scheduler-shaped ids).
        #   (c) sidecar missing/malformed/schema-incompat → unreadable on-disk
        #       state, genuinely loud. read_run_sidecar can raise SchemaIncompat
        #       (a too-new sidecar) on top of OSError/JSONDecodeError; a failed
        #       read drops to the bare JournalCorrupt — it must NEVER read as a
        #       benign orphan (that would mask a real corruption, #328).
        sidecar_read_ok = False
        _stranded: list[str] = []
        try:
            _stranded = list(read_run_sidecar(experiment_dir, run_id).get("job_ids") or [])
            sidecar_read_ok = True
        except Exception:
            # (c): no readable sidecar → no hint, no benign reclassification.
            sidecar_read_ok = False
        if sidecar_read_ok and not _stranded:
            # (a) benign orphan: valid sidecar, jobless, no journal record.
            return OrphanedReconcile(run_id=run_id), False
        # (b)/(c): stranded-ids hint when the read succeeded with ids; bare
        # message otherwise. Either way this stays a loud JournalCorrupt.
        hint = (
            f" Sidecar .hpc/runs/{run_id}.json carries job_ids {_stranded} from the "
            "post-qsub pre-stamp; mint the journal record with `hpc-agent submit-spec "
            "--spec <file>` using those ids, then re-run."
            if _stranded
            else ""
        )
        raise errors.JournalCorrupt(f"no run record for {run_id!r}.{hint}")

    try:
        _sidecar = read_run_sidecar(experiment_dir, run_id)
    except (OSError, json.JSONDecodeError, errors.HpcError):
        # Missing/malformed sidecar → bare-python reporter call → the
        # reporter-failed routing below will catch the resulting error and
        # surface unable_to_verify rather than silent abandon.
        _sidecar = {}

    warnings: list[str] = []
    report: dict[str, Any] = {}
    summary: dict[str, Any]
    alive: list[str] | set[str]
    if not backend_requires_ssh(scheduler):
        # Pure-API path (#337 Increment 4): no login node, no shared
        # ``_combiner/`` dir. Liveness comes from the backend's ``alive_job_ids``
        # instance hook (a pure-API backend holds its own authenticated client);
        # there is no SSH status reporter and no wave-listing to do, so the
        # combiner waves stay as the journal recorded them.
        from hpc_agent.infra.backends.remote_factory import backend_for_record

        summary = {"checked_at": utcnow_iso()}
        reporter_failed = False
        combined = list(record.combined_waves)
        try:
            alive = backend_for_record(record, scheduler=scheduler).alive_job_ids(record.job_ids)
            alive_check_failed = False
        except Exception as exc:
            alive = list(record.job_ids)  # treat as still alive on error
            warnings.append(f"alive check: {exc}")
            alive_check_failed = True
    else:
        with ThreadPoolExecutor(max_workers=3) as pool:
            fut_status = pool.submit(
                _ssh_status_report,
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
                run_id=run_id,
                job_ids=record.job_ids,
                job_name=record.job_name,
                file_glob=file_glob,
                remote_activation=remote_activation_for_sidecar(_sidecar),
            )
            fut_waves = pool.submit(
                _ssh_list_combined_waves,
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
            )
            fut_alive = pool.submit(
                _ssh_alive_job_ids,
                ssh_target=record.ssh_target,
                job_ids=record.job_ids,
                scheduler=scheduler,
            )

            try:
                report = fut_status.result()
                summary = dict(report.get("summary", {}))
                reporter_failed = False
            except Exception as exc:
                summary = {"error": str(exc)}
                warnings.append(f"status reporter: {exc}")
                reporter_failed = True
            summary["checked_at"] = utcnow_iso()
            if isinstance(report.get("waves"), dict) and report["waves"]:
                summary["waves"] = report["waves"]

            # Each future has its own try/except: an SSH blip on any of them
            # must not abort the journal update.  In particular, falling
            # back to the *current* job_ids on the alive-check path is
            # essential — defaulting to empty would mark a healthy run
            # ``abandoned`` whenever the SSH check itself failed.
            try:
                combined = fut_waves.result()
            except Exception as exc:
                combined = list(record.combined_waves)
                warnings.append(f"wave list: {exc}")
                alive_check_failed = False
            else:
                alive_check_failed = False

            try:
                alive = fut_alive.result()
            except Exception as exc:
                alive = list(record.job_ids)  # treat as still alive on error
                warnings.append(f"alive check: {exc}")
                alive_check_failed = True

    if warnings:
        summary["warnings"] = warnings

    # #258 + 0.10.12: when either the alive-check or the status reporter
    # couldn't run, the run's true state is unknown. Mark the snapshot so
    # the envelope surfaces ``unable_to_verify`` instead of masquerading the
    # stale journal status as a confirmed reading.
    if alive_check_failed or reporter_failed:
        summary["verify_state"] = "unable_to_verify"

    fields: dict[str, Any] = {
        "last_status": summary,
        "combined_waves": combined,
        # Drop any failed_waves entries that are now combined.
        "failed_waves": [w for w in record.failed_waves if w not in set(combined)],
    }
    updated = update_run_status(experiment_dir, run_id, **fields)

    # Verdict routing when no recorded job is alive on the scheduler.
    #
    # "abandoned" must require EVIDENCE OF NON-COMPLETION *WITHOUT* evidence of
    # what happened — it is not the default for "no alive jobs". THREE distinct
    # cases share the "nothing alive" observation, and absence must not collapse
    # failure into it (#351 sub-bug #4):
    #
    #   * All tasks complete + records purged. SGE/Slurm drop a finished
    #     job's records post-completion; the reporter's per-task counts still
    #     prove every result is on disk. This run is COMPLETE, not abandoned
    #     (the demo-bug class: a FINISHED run read as abandoned because its
    #     job records were purged). Classified by ``settle``'s strict
    #     all-complete arm, alongside the existing ``alive_check_failed`` guard.
    #   * Ran and FAILED + records purged. The reporter shows ``failed >= 1``:
    #     a task reached the cluster, ran, and exited non-zero with a readable
    #     ``exit_code``/traceback on disk. That is POSITIVE failure evidence, the
    #     symmetric counterpart to the all-complete arm — categorically NOT a
    #     vanished scratch. Pre-#351 this routed through ``abandoned`` ("scratch
    #     purged, no recovery; re-submit") because the binary verdict keyed only
    #     on completeness, hiding the fixable error. Now ``settle``'s
    #     ``run_failed`` arm routes it to ``failed`` and the FAILED branch below
    #     carries the classified error out via ``last_status``.
    #   * Incomplete-but-not-failed + records gone. Tasks merely missing/unknown
    #     (NO positive ``failed`` count) AND nothing alive AND both probes ran
    #     cleanly → genuine abandon: no evidence on disk at all.
    #
    # Both probes must have run cleanly first: either failing routes through
    # ``unable_to_verify`` (set above) — confirmed-dead-on-scheduler +
    # reporter-dead-so-results-unknown is not a provable verdict either way.
    if record.job_ids and not alive and not alive_check_failed and not reporter_failed:
        # One verdict from the shared settle-path classifier (strict completion,
        # failure outranks absence); the side-effects stay local to each arm. The
        # decision's ``reason`` is recorded in ``last_status.verdict_reason`` so
        # WHY reconcile reached this terminal state is readable from the envelope
        # (the "abandoned-vs-failed" confusion class, #351 #4) instead of having
        # to re-derive it from the raw counts.
        decision = settle(summary, record.total_tasks)
        recorded = {**summary, "verdict_reason": decision.reason}
        if decision.verdict == LifecycleState.FAILED:
            # Positive failure evidence — surface the classified error in
            # ``last_status.failure_features`` so ``_reconcile_envelope`` carries
            # it out (the skill's ``failed`` branch reads it), then mark terminal
            # ``failed`` (a valid JournalStatus + reconcile lifecycle_state).
            features = _gather_failure_features(
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
                job_name=record.job_name,
                job_ids=list(record.job_ids),
                scheduler=scheduler,
            )
            update_run_status(
                experiment_dir, run_id, last_status={**recorded, "failure_features": features}
            )
            updated = mark_run(experiment_dir, run_id, status="failed")
        else:
            update_run_status(experiment_dir, run_id, last_status=recorded)
            updated = mark_run(experiment_dir, run_id, status=str(decision.verdict))
    return updated, alive_check_failed


@primitive(
    name="mark-run-terminal",
    verb="mutate",
    side_effects=[
        SideEffect(
            "writes-journal",
            "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (under flock)",
        ),
    ],
    # ``mark_terminal`` delegates to ``journal.mark_run`` which currently
    # does not raise ``JournalCorrupt`` — leaving the prior declaration
    # would be a phantom that callers wire retry policy against in vain.
    error_codes=[],
    idempotent=True,
    idempotency_key="run_id",
    cli=None,  # Python-only primitive
)
def mark_terminal(
    experiment_dir: Path,
    run_id: str,
    *,
    status: str,
    stage: str | None = None,
) -> RunRecord:
    """Thin pass-through to ``journal.mark_run`` for symmetry."""
    return mark_run(experiment_dir, run_id, status=status, stage=stage)
