"""Reconcile + mark-terminal runner primitives."""

from __future__ import annotations

import dataclasses
import json
import logging
import shlex
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.contract.vocabulary import (
    NEVER_DISPATCHED_VERDICT_REASON,
    JournalStatus,
    LifecycleState,
)
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra import remote
from hpc_agent.infra.backends import backend_requires_ssh
from hpc_agent.infra.clusters import resolve_ssh_target
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.monitor.announce import read_announcements
from hpc_agent.ops.monitor.classify import settle
from hpc_agent.ops.monitor.harvest_guard import harvest_on_terminal, harvest_receipt_exists
from hpc_agent.ops.monitor.status import _ssh_status_report
from hpc_agent.state.journal import (
    is_kill_confirmed,
    load_run,
    mark_run,
    update_run_status,
)

if TYPE_CHECKING:
    from hpc_agent.state.run_record import RunRecord

_log = logging.getLogger(__name__)


def _harvest_if_owed(
    experiment_dir: Path,
    run_id: str,
    *,
    terminal_cause: str,
    record: RunRecord,
    pre_reconcile_status: str,
) -> None:
    """Fire the guaranteed terminal harvest on a verdict TRANSITION, OR as a
    journal-evidence backstop when the run is terminal but carries NO receipt.

    The transition gate (``terminal_cause != pre_reconcile_status``) covers the
    normal path and keeps an idempotent re-reconcile from re-paying the pull +
    reduce + ledger append. But a session-death BETWEEN ``mark_run(terminal)``
    and ``harvest_on_terminal`` leaves the run terminal-with-no-harvest, and the
    next reconcile sees NO transition (the journal already reads terminal) — so a
    transition-ONLY gate would drop the guaranteed harvest forever (audit U8 /
    rank 2, ``docs/plans/transport-robustness-2026-07-17``). Unlike
    ``monitor_flow`` (which harvests from a ``finally``), reconcile is invoked
    directly by drivers / the skill and has no such backstop.

    "Harvest owed" is therefore derived from DURABLE JOURNAL EVIDENCE, never
    in-process state: :func:`harvest_receipt_exists` reads the run's
    ``<run_id>.harvest.jsonl`` ledger, so a terminal run with no receipt re-fires
    EXACTLY once and a run whose receipt already landed does not — idempotent both
    ways (the harvest itself is idempotent: an ``ensure_all_combined=False``
    aggregate re-run over atomic cluster writes + an append-only ledger). This is
    NOT a "terminal is sticky" guard: the verdict stays revisable
    (engineering-principles — the verdict is revisable, the evidence is durable);
    a legit complete→failed downgrade IS a transition and still harvests.
    """
    if terminal_cause != pre_reconcile_status:
        harvest_on_terminal(experiment_dir, run_id, terminal_cause=terminal_cause, record=record)
        return
    if not harvest_receipt_exists(experiment_dir, run_id):
        # No verdict transition, but the run is terminal with NO harvest receipt:
        # a session-death in the mark_run→harvest window dropped the guaranteed
        # harvest. Re-drive it now, loudly (the harvest appends its own durable
        # marker, so a repair is never silent — disclosure discipline).
        _log.warning(
            "reconcile: run %s is terminal (%s) with NO harvest receipt — a "
            "session-death between mark_run and harvest dropped the guaranteed "
            "harvest; re-firing it now (journal-evidence backstop, audit U8).",
            run_id,
            terminal_cause,
        )
        harvest_on_terminal(experiment_dir, run_id, terminal_cause=terminal_cause, record=record)


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


# Sentinel-ack for the combined-wave listing (docs/design/connection-broker.md,
# 2026-07-10). The listing is a POSITIVE success-marker scan: an empty result
# must mean "the shell reached the remote dir and found no wave files", never
# "the cd silently failed / the channel returned nothing". Echoed right after a
# successful ``cd`` so its PRESENCE proves the query ran; its ABSENCE (a failed
# cd, or a truncated read) is UNKNOWN → reconcile keeps the journal's
# combined_waves rather than blanking them.
_WAVE_ACK = "__HPC_WAVE_ACK__"


def _ssh_list_combined_waves(*, ssh_target: str, remote_path: str) -> list[int]:
    """Derive ``combined_waves`` from cluster artifacts.

    The combiner writes ``_combiner/wave_<N>.json`` per successful run
    (see ``hpc_agent/execution/mapreduce/combiner.py``). We use the
    presence of that file as the success marker.
    """
    # cd (&&) → ack echo, THEN the listing (a bare ``;`` so an empty glob's
    # non-zero ``ls`` never masks the ack); trailing ``true`` keeps the remote
    # rc 0 so the ``rc != 0`` guard stays a pure SSH-transport (rc 255) check.
    cmd = (
        f"cd {shlex.quote(remote_path)} && printf '%s\\n' {shlex.quote(_WAVE_ACK)}; "
        f"ls _combiner/wave_*.json 2>/dev/null; true"
    )
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    if proc.returncode != 0:
        # SSH transport failure (rc 255) — not "no waves combined yet". Raise so
        # reconcile keeps the journal's combined_waves instead of overwriting it
        # with an empty list on a connectivity blip.
        raise errors.RemoteCommandFailed(
            f"combined-wave list failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    lines = proc.stdout.splitlines()
    if _WAVE_ACK not in {ln.strip() for ln in lines}:
        # Positive-evidence transport verdict: no affirmative ack means the
        # ``cd`` into remote_path failed (bad path / not yet deployed) or the
        # read was silently truncated — UNKNOWN, not "zero waves combined".
        # Raise so reconcile preserves the journal's combined_waves rather than
        # blanking a run's combine history on a silent blip.
        raise errors.RemoteCommandFailed(
            "combined-wave list returned no positive-evidence ack (cd into "
            f"{remote_path!r} failed, or a silent/truncated read); refusing to "
            "read absence as 'zero waves combined'."
        )
    waves: set[int] = set()
    for line in lines:
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
        # SSH transport failure (rc 255), not "scheduler ran, found nothing
        # alive". Raise so reconcile's guard sets alive_check_failed and does
        # NOT mark a healthy run abandoned on a connectivity blip.
        raise errors.RemoteCommandFailed(
            f"alive check failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    # Positive-evidence transport verdict (docs/design/connection-broker.md,
    # sentinel-ack ruling): the alive query proves it RAN by echoing an
    # affirmative ack token. An empty read WITHOUT it is a silently truncated /
    # never-run channel (or, on SGE/PBS, the scheduler binary itself failed) —
    # UNKNOWN, not "no jobs alive". Reading absence as "not alive" is exactly
    # what routes a healthy run to `abandoned`; raise so the guard sets
    # alive_check_failed → unable_to_verify instead. (A genuinely empty queue
    # still carries the ack, so it returns an empty set as before.)
    clean, ran_ok = backend_cls.scheduler_query_ran(proc.stdout)
    if not ran_ok:
        raise errors.RemoteCommandFailed(
            "alive check returned no positive-evidence ack (silent/empty read — "
            "the query did not run to completion, or the scheduler binary itself "
            "failed); refusing to read absence as 'no jobs alive'."
        )
    return backend_cls.parse_alive_output(clean, job_ids)


# The settle-path completion/failure predicates and their precedence now live
# in :mod:`hpc_agent.ops.monitor.classify` (``all_tasks_complete`` /
# ``run_failed`` / ``settle``) so the count-to-verdict rule has a single home
# shared with the monitor poll loop. ``_reconcile_one`` applies them via
# :func:`settle`, which also returns the provenance recorded in
# ``last_status.verdict_reason``.


def _failed_evidence_task_ids(report: dict[str, Any]) -> list[int]:
    """The 0-based task id(s) whose stderr evidences a FAILED verdict.

    The reporter's per-task statuses (``report["tasks"]``, keyed by 0-based
    ``HPC_TASK_ID``) name which task(s) actually failed; fetching task 0's log
    unconditionally attached a possibly-SUCCESSFUL task's stderr as the run's
    failure evidence — correct only for 1-task canaries. Bounded to the FIRST
    (lowest-id) failed task: one log tail is the evidence shape
    ``verify_canary`` attaches, and one fetch keeps the SSH cost flat. Falls
    back to ``[0]`` when the report carries no per-task ``failed`` entry (the
    verdict then stands on the summary counts alone).
    """
    failed: list[int] = []
    tasks = report.get("tasks")
    if isinstance(tasks, dict):
        for tid_str, info in tasks.items():
            if not (isinstance(info, dict) and str(info.get("status")) == "failed"):
                continue
            try:
                failed.append(int(tid_str))
            except (TypeError, ValueError):
                continue
    return [min(failed)] if failed else [0]


def _gather_failure_features(
    *,
    ssh_target: str,
    remote_path: str,
    job_name: str,
    job_ids: list[str],
    scheduler: str,
    task_ids: list[int],
    job_task_spans: dict[str, tuple[int, int]] | None = None,
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

    *task_ids* are the 0-based ids whose stderr to tail — the caller selects
    the actually-failed task(s) via :func:`_failed_evidence_task_ids` so the
    evidence never quotes a successful task's log.

    *job_task_spans* is the sidecar's per-job global task-window map for a
    WAVED run (``state.runs.read_job_task_spans``), threaded through to
    ``fetch_task_logs`` so the evidence tail is read from the covering job
    with the job-LOCAL log index. ``None`` (old sidecar / single array /
    resubmit job) keeps the global-index probe.

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
    failed_task_id: int | None = None
    failed_job_id: str | None = None
    try:
        logs = fetch_task_logs(
            ssh_target=ssh_target,
            remote_path=remote_path,
            job_name=job_name,
            job_ids=job_ids,
            scheduler=scheduler,
            task_ids=task_ids,
            lines=50,
            job_task_spans=job_task_spans,
        )
        if logs and isinstance(logs[0], dict):
            stderr_tail = str(logs[0].get("content") or "")
            raw_path = logs[0].get("path")
            log_path = raw_path if isinstance(raw_path, str) else None
            # Node identity (item 15): surface WHICH task+job the evidence tail
            # came from — the fetch already resolved these, so it's free here.
            raw_tid = logs[0].get("task_id")
            failed_task_id = raw_tid if isinstance(raw_tid, int) else None
            raw_jid = logs[0].get("job_id")
            failed_job_id = raw_jid if isinstance(raw_jid, str) else None
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
        # Node identity (notebook-audit Addendum 10, item 15): the contentless
        # ``cluster_env_init`` shape ("Unable to initialize environment") is a
        # per-task/per-node flake that is only diagnosable when the envelope
        # names WHICH scheduler + task the failure landed on. ``scheduler`` is a
        # parameter and ``task_id`` / ``job_id`` are already resolved by the log
        # fetch, so they are surfaced here for free. The remote HOST/node is the
        # remaining leg: it is not cleanly in scope at reconcile time (the job
        # has left the scheduler, and ``fetch_task_logs`` tails the LAST lines,
        # which for Grid Engine do not carry the exec-node header) — recovering
        # it needs a dedicated ``qstat -j`` / ``sacct --format=NodeList`` probe
        # or a GE ``.o`` header parse, so ``node`` is an explicit ``None``
        # placeholder until that probe is plumbed rather than a silent omission.
        "scheduler": scheduler,
        "task_id": failed_task_id,
        "job_id": failed_job_id,
        "node": None,
    }


def _census_progress_summary(progress: dict[str, int]) -> dict[str, Any]:
    """5-key mid-flight ``last_status`` derived from a PARTIAL announce census.

    Rank 19 (``docs/plans/latency-audit-2026-07-15``): when a partial
    announcement is present and the run is still in flight, the census
    (``complete``/``failed``/``missing``) plus the cheap alive probe already
    answer the lifecycle question, so reconcile SKIPS the per-task status-reporter
    walk and builds ``last_status`` from these counts instead. Every not-yet-
    terminal task is ``pending`` (``missing``) so the shared :func:`settle`
    classifier reads the census as still-in-flight — a partial census must NEVER
    settle terminal. ``status_source``/``verdict_source`` record that the
    announcement census STOOD IN for the walk (the fallback-disclosure
    semantics), so the brief and downstream readers can see the walk was skipped.

    Shape parity with ``ops.monitor_flow._announce_status`` (the Phase-2 poll leg)
    is deliberate — both project the same census onto the same 5-key summary.
    """
    missing = int(progress["missing"])
    return {
        "complete": int(progress["complete"]),
        "failed": int(progress["failed"]),
        "running": 0,
        "pending": missing,
        "unknown": 0,
        "verdict_source": "task_announcements",
        "status_source": "task_announcements",
    }


def _settle_from_announcements(
    experiment_dir: Path,
    run_id: str,
    *,
    scheduler: str,
    record: RunRecord,
    announce: dict[str, int],
    pre_reconcile_status: str,
) -> RunRecord | None:
    """Settle a run terminal from a FULL per-task announcement, or return None.

    Crash-only-monitoring Phase-1 fast path (``docs/design/crash-only-monitoring.md``).
    When every task has announced its terminal state
    (``announce["announced"] == record.total_tasks``), build the canonical
    5-key count summary the shared :func:`settle` classifier consumes and route
    the verdict through the SAME ``mark_run`` + transition-gated
    ``harvest_on_terminal`` the reporter-backed settle arm uses — so the same
    counts settle to the same lifecycle state whether the evidence came from the
    dispatcher's announcements or the status reporter's walk. The announcements
    replace the reporter walk for the LIFECYCLE verdict ONLY; the aggregate
    integrity gate still verifies every output independently.

    Returns the reconciled record on a terminal settle; returns ``None`` for a
    PARTIAL announcement (caller keeps probing) or a zero-task run — a partial
    announcement must NEVER settle terminal.
    """
    total = record.total_tasks
    if total <= 0 or int(announce["announced"]) != total:
        return None
    summary: dict[str, Any] = {
        "complete": int(announce["complete"]),
        "failed": int(announce["failed"]),
        "running": 0,
        "pending": 0,
        "unknown": 0,
        "checked_at": utcnow_iso(),
        "verdict_source": "task_announcements",
    }
    decision = settle(summary, total)
    recorded = {**summary, "verdict_reason": decision.reason}
    if decision.verdict == LifecycleState.FAILED:
        # Positive failure evidence — attach the classified error like the
        # reporter-backed arm. Without a reporter report to name the failed
        # task, tail the lowest-id task's log (the ``_failed_evidence_task_ids``
        # fallback); the fetch is best-effort and never gates the verdict.
        from hpc_agent.state.runs import read_job_task_spans

        features = _gather_failure_features(
            ssh_target=resolve_ssh_target(record),
            remote_path=record.remote_path,
            job_name=record.job_name,
            job_ids=list(record.job_ids),
            scheduler=scheduler,
            task_ids=[0],
            job_task_spans=read_job_task_spans(experiment_dir, run_id),
        )
        update_run_status(
            experiment_dir, run_id, last_status={**recorded, "failure_features": features}
        )
        updated = mark_run(experiment_dir, run_id, status="failed")
    else:
        update_run_status(experiment_dir, run_id, last_status=recorded)
        updated = mark_run(experiment_dir, run_id, status=str(decision.verdict))
    # Guaranteed harvest (§5) — fire on a verdict TRANSITION, OR as a
    # journal-evidence backstop when the run is terminal with no harvest receipt
    # (a death in the mark_run→harvest window). Identical policy to the
    # reporter-backed settle arm; an idempotent re-reconcile of an
    # already-harvested run does NOT re-fire.
    _harvest_if_owed(
        experiment_dir,
        run_id,
        terminal_cause=str(decision.verdict),
        record=updated,
        pre_reconcile_status=pre_reconcile_status,
    )
    return updated


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


# The canary-pairing suffix FAMILY (#258 + the double canary): a canary-gated
# ``submit-flow`` writes ``<run_id>`` and ``<run_id>-canary``; when the
# determinism fingerprint mints its n=2 prior (``submit-and-verify``), a SECOND
# canary ``<run_id>-canary2`` also lands. This module owns the ONE suffix
# definition; other call sites (``ops/status_blocks``, ``ops/aggregate_flow``,
# ``ops/supersession``, ``ops/resolve_submit_inputs``) import the helpers below
# rather than re-inlining any ``-canary`` literal (engineering-principles.md:
# one definition per identity decision). Ordered PRIMARY-canary FIRST so the
# call sites that report a single "the canary" for a lease-only attempt name
# ``-canary`` (not ``-canary2``); ``canary_parent_of`` strips longest-suffix
# first (below) so ``-canary2`` still maps to the main run, never ``…-canary``.
_CANARY_SUFFIXES: tuple[str, ...] = ("-canary", "-canary2")


def canary_parent_of(run_id: str) -> str | None:
    """The parent run's id when *run_id* is a ``-canary`` FAMILY sibling, else None.

    The single is-this-a-canary predicate (#258 pairing, widened for the double
    canary): a non-None return means *run_id* names one of the 1-task canary
    journal entries (``-canary`` or ``-canary2``) and the returned id names the
    main run it was submitted alongside. Suffixes are tried LONGEST-first so a
    ``-canary2`` id strips to the main run, never to ``…-canary`` -> ``…2``.
    """
    for suffix in sorted(_CANARY_SUFFIXES, key=len, reverse=True):
        if run_id.endswith(suffix):
            return run_id[: -len(suffix)]
    return None


def canary_family(parent_run_id: str) -> list[str]:
    """Every ``-canary`` FAMILY id for a MAIN *parent_run_id* (``-canary``, ``-canary2``).

    The ONE place the suffix family expands, so the double-canary exclusion,
    the reconcile sibling-settle, and the supersession/resolve call sites all
    agree on exactly which sub-records a main run owns.
    """
    return [f"{parent_run_id}{suffix}" for suffix in _CANARY_SUFFIXES]


def sibling_run_ids(run_id: str) -> list[str]:
    """Paired journal entries that share this submit's ``cmd_sha`` (#258 + double canary).

    A canary-gated ``submit-flow`` writes the main run and its ``<run_id>-canary``
    sibling; ``submit-and-verify``'s fingerprint prior adds ``<run_id>-canary2``.
    Reconcile must settle EVERY paired entry in one call, or the next
    ``/submit-hpc`` is blocked by an untouched canary entry (the run-#7
    unsettled-sibling stall class, re-opened by the second canary). Given the
    MAIN id, return the whole canary family; given ANY canary-family id, return
    the main plus the OTHER family members. Callers that single-unpacked the old
    one-element return were widened in the same commit.
    """
    parent = canary_parent_of(run_id)
    if parent is not None:
        return [parent, *[c for c in canary_family(parent) if c != run_id]]
    return canary_family(run_id)


_sibling_run_ids = sibling_run_ids  # back-compat alias


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
    for sib_id in sibling_run_ids(run_id):
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


# ── submit-once recovery (U3-d): reconcile is the SOLE transition-out owner ────
#
# A ``submitting`` record is a submit caught in — or orphaned in — its
# dispatch→job-id window (``mint_submitting_record`` ran, the id-read/promote did
# not). No monitor poll, no plain resubmit, no prune touches it: reconcile is the
# ONE path that transitions it out (submit-once §3.3 containment). The recovery
# read re-derives truth from the cluster-durable jobmap MARKER (the id the
# dispatching shell persisted) + the U3-c correlation-key query, landing every
# rung on POSITIVE evidence — an acked read, never absence (SUBMIT-ONCE-DESIGN §4).


def _adoptable_wave_ids(parsed: Any, backend_cls: Any) -> dict[str, str]:
    """The jobmap waves that pass the Δ4 adopt gate → ``{wave_key: job_id}``.

    A wave is adoptable ONLY when BOTH hold (premortem Δ4, the phantom-id guard):
    the recorded ``rc == 0`` (the ``qsub`` did NOT fail) AND the raw scheduler
    stdout blob yields a job id under the SAME ``JOB_ID_REGEX`` the client applies
    on the happy path (one id-parsing source — the recovery reader never
    re-implements it). A wave that fails EITHER (``rc≠0`` = confirmed failed
    dispatch, or a blob with no parseable id) is dropped here and the run falls to
    rung-1b disambiguation, never a blind adopt of a job that names no live array.
    """
    out: dict[str, str] = {}
    for wkey, (blob, rc) in parsed.waves.items():
        if rc != 0:
            continue
        match = backend_cls.JOB_ID_REGEX.search(blob or "")
        if match:
            out[wkey] = match.group(1)
    return out


def _clear_jobmap(*, ssh_target: str, remote_path: str, run_id: str) -> None:
    """Best-effort remove the jobmap marker + wave id-files for *run_id*.

    Called on a safe-resubmit transition (the orphan is resolved: never
    dispatched / confirmed-not-landed) so a stale marker cannot ``attempt``-match
    and be falsely adopted onto a future same-run_id submit. Best-effort by
    contract — a failed clear never gates the transition (the ``attempt+1`` bump
    is the real guard, OPEN-4); the ``rm -f`` glob keeps ``run_id`` quoted and the
    ``.jobmap*`` suffix literal so it expands.
    """
    from hpc_agent.infra.jobmap import jobmap_dir

    d = shlex.quote(jobmap_dir(remote_path))
    rid = shlex.quote(run_id)
    try:
        remote.ssh_run(
            f"rm -f {d}/{rid}.jobmap {d}/{rid}.jobmap.*.id 2>/dev/null; true", ssh_target=ssh_target
        )
    except Exception:  # noqa: BLE001 — clearing is hygiene, never gates the transition
        _log.warning("reconcile: jobmap clear failed for %s (non-fatal)", run_id, exc_info=True)


def _recover_submitting(
    experiment_dir: Path,
    run_id: str,
    *,
    record: RunRecord,
    scheduler: str,
) -> RunRecord:
    """The submit-once recovery ladder for a ``submitting`` record (§3.4 / §4).

    Every rung lands on POSITIVE evidence; absence is NEVER trusted:

    * **rung 3 (severed)** — the jobmap read's SSH transport failed (rc≠0), or the
      ack sentinel was absent (:attr:`JobmapRead.present` False under a rc-0 read
      is a ``cd`` that failed) with no cross-evidence: UNKNOWN → leave
      ``submitting``, re-census next tick. Never a settle.
    * **rung 1a (adopt)** — the marker carries ≥1 wave id passing the Δ4 gate
      (``rc==0`` + ``JOB_ID_REGEX``): ADOPT — promote ``submitting → in_flight``
      with those ids, then Δ6 cross-check the announce census, and hand to the
      normal monitor/announce path.
    * **rung 1b (disambiguate)** — marker present + ack + no adoptable id: query
      the scheduler by the U3-c correlation token. A severed / not-run query stays
      UNKNOWN (``submitting``). A hit ADOPTs the recovered id; a clean miss (query
      ran, ack fired, token absent) is proof the array never entered the queue →
      SAFE RE-SUBMIT.
    * **rung 2 (never dispatched)** — jobmap dir absent under a clean read AND the
      announce census also absent (Δ6 shared-FS cross-check): the pre-dispatch
      marker write never ran → SAFE RE-SUBMIT.

    SAFE RE-SUBMIT transitions ``submitting → abandoned`` (a resubmittable-terminal
    verdict, ``is_resubmittable_terminal``) and clears the jobmap, so the operator
    /campaign's next submit PROCEEDs and mints ``attempt+1`` (``allocate_attempt``)
    — a stale marker can never adopt onto it. Returns the (possibly transitioned)
    record; the ``bool`` alive-check-failed the caller expects is always ``False``
    here (recovery owns its own UNKNOWN posture).
    """
    from hpc_agent.infra.backends import get_backend_class
    from hpc_agent.infra.jobmap import build_read_shell, jobmap_token, parse_jobmap_read

    ssh_target = resolve_ssh_target(record)
    attempt = int(record.attempt)

    # --- Read the jobmap marker (ack-gated, one bounded ssh exec). ---
    try:
        proc = remote.ssh_run(
            build_read_shell(remote_path=record.remote_path, run_id=run_id),
            ssh_target=ssh_target,
        )
    except Exception as exc:  # noqa: BLE001 — a raised transport error is UNKNOWN, not a settle
        return _stay_submitting(experiment_dir, run_id, reason=f"jobmap read severed: {exc}")
    if proc.returncode != 0:
        # SSH transport failure (rc 255) — severed. UNKNOWN, stay submitting.
        return _stay_submitting(
            experiment_dir, run_id, reason=f"jobmap read transport rc {proc.returncode}"
        )
    parsed = parse_jobmap_read(proc.stdout)

    backend_cls = get_backend_class(scheduler)

    if parsed.present:
        marker_attempt = parsed.attempt if parsed.attempt is not None else attempt
        # rung 1a — adopt any wave whose id passes the Δ4 gate.
        adoptable = _adoptable_wave_ids(parsed, backend_cls)
        if adoptable:
            return _adopt_and_promote(
                experiment_dir,
                run_id,
                job_ids=sorted(set(adoptable.values())),
                record=record,
                ssh_target=ssh_target,
                source="cluster jobmap marker",
            )
        # rung 1b — marker pending, no adoptable id: query the scheduler by token.
        return _disambiguate_by_token(
            experiment_dir,
            run_id,
            record=record,
            scheduler=scheduler,
            ssh_target=ssh_target,
            token=jobmap_token(run_id, int(marker_attempt)),
        )

    # parsed.present is False under a clean (rc-0) read: the ``.hpc/submit/`` dir
    # was never created ⇒ the pre-dispatch marker write never ran ⇒ candidate
    # rung-2 "never dispatched". Δ6: cross-check the announce census before
    # trusting absence — on a non-shared FS a marker written on another login node
    # would read absent here yet the array be live. Require the announce dir ALSO
    # absent before a safe re-submit.
    return _rung2_never_dispatched(experiment_dir, run_id, record=record, ssh_target=ssh_target)


def _stay_submitting(experiment_dir: Path, run_id: str, *, reason: str) -> RunRecord:
    """Leave the run ``submitting`` (UNKNOWN) and record why — never a settle."""
    _log.info("reconcile: run %s stays submitting (recovery UNKNOWN): %s", run_id, reason)
    rec = update_run_status(
        experiment_dir,
        run_id,
        last_status={
            "verdict": "submitting",
            "verdict_reason": "recovery_unknown_recensus",
            "recovery_note": reason,
        },
    )
    return rec


def _adopt_and_promote(
    experiment_dir: Path,
    run_id: str,
    *,
    job_ids: list[str],
    record: RunRecord,
    ssh_target: str,
    source: str,
) -> RunRecord:
    """ADOPT the recovered id: promote ``submitting → in_flight`` + Δ6 cross-check.

    The promote is the SAME two locked writes ``ops.submit.runner.
    promote_submitting_record`` performs (stamp ``job_ids``, then transition
    ``in_flight``) — MIRRORED here over the public ``state.journal`` seam rather
    than imported, because ``ops/monitor`` reaching into ``ops/submit`` is a
    cross-subject layering violation (``lint_subject_imports``); the settle_run /
    ``_harvest_if_owed`` mirror precedent. A crash BETWEEN the two writes leaves a
    ``submitting`` record that already carries ``job_ids`` — still recoverable
    (this same rung re-adopts it next tick), strictly better than losing it.
    """
    _log.warning(
        "reconcile: adopted orphaned array %s: jobs %s (recovered from %s) — "
        "promoting submitting→in_flight, no re-qsub.",
        run_id,
        job_ids,
        source,
    )
    update_run_status(experiment_dir, run_id, job_ids=list(job_ids))
    promoted = mark_run(experiment_dir, run_id, status=str(JournalStatus.IN_FLIGHT))

    # Δ6 — cross-check the adoption against the announce census where available
    # (best-effort, positive-only: the marker id is already positive evidence, so
    # a severed census never un-adopts; a census that CONFIRMS the dispatcher
    # started is recorded for visibility).
    census_note = "unavailable"
    try:
        announce = read_announcements(
            ssh_target=ssh_target,
            remote_path=record.remote_path,
            run_id=run_id,
            task_count=record.total_tasks,
        )
        census_note = "confirmed" if announce.get("present") else "dir-absent"
    except Exception:  # noqa: BLE001 — cross-check is best-effort; the marker id stands
        census_note = "unavailable"
    merged = {
        **(promoted.last_status or {}),
        "verdict_reason": "submit_once_adopted_from_marker",
        "adopted_job_ids": list(job_ids),
        "announce_crosscheck": census_note,
    }
    return update_run_status(experiment_dir, run_id, last_status=merged)


def _disambiguate_by_token(
    experiment_dir: Path,
    run_id: str,
    *,
    record: RunRecord,
    scheduler: str,
    ssh_target: str,
    token: str,
) -> RunRecord:
    """rung 1b: the marker is pending with no id — ask the scheduler by token."""
    from hpc_agent.infra.backends import get_backend_class

    backend_cls = get_backend_class(scheduler)
    try:
        proc = remote.ssh_run(backend_cls.build_token_query_cmd(), ssh_target=ssh_target)
    except Exception as exc:  # noqa: BLE001 — a raised query error is UNKNOWN, not a miss
        return _stay_submitting(experiment_dir, run_id, reason=f"token query severed: {exc}")
    if proc.returncode != 0:
        return _stay_submitting(
            experiment_dir, run_id, reason=f"token query transport rc {proc.returncode}"
        )
    clean, ran_ok = backend_cls.scheduler_query_ran(proc.stdout)
    if not ran_ok:
        # The query did NOT run to completion (silent/truncated read, or the
        # scheduler binary itself failed) — UNKNOWN, never "token absent".
        return _stay_submitting(
            experiment_dir, run_id, reason="token query no positive-evidence ack"
        )
    token_map = backend_cls.parse_token_query(clean)
    hit = token_map.get(token)
    if hit:
        return _adopt_and_promote(
            experiment_dir,
            run_id,
            job_ids=[hit],
            record=record,
            ssh_target=ssh_target,
            source=f"scheduler token query ({token})",
        )
    # Clean miss: the query RAN, the ack fired, and the token is absent from the
    # user's queue → the array was never accepted → safe to re-submit.
    return _safe_resubmit(
        experiment_dir,
        run_id,
        record=record,
        ssh_target=ssh_target,
        reason="token query clean-miss: array never entered the scheduler queue",
    )


def _rung2_never_dispatched(
    experiment_dir: Path,
    run_id: str,
    *,
    record: RunRecord,
    ssh_target: str,
) -> RunRecord:
    """rung 2: jobmap dir absent under a clean read — Δ6-cross-check then resubmit."""
    # Δ6 shared-FS cross-check: if the announce dir is PRESENT (the dispatcher
    # started) OR the census read is severed, we cannot trust "never dispatched"
    # — stay submitting. Only a jobmap-absent AND announce-absent pair is proof
    # the submit never actuated.
    try:
        announce = read_announcements(
            ssh_target=ssh_target,
            remote_path=record.remote_path,
            run_id=run_id,
            task_count=record.total_tasks,
        )
    except Exception as exc:  # noqa: BLE001 — a severed census is UNKNOWN, never "never dispatched"
        return _stay_submitting(
            experiment_dir, run_id, reason=f"announce cross-check severed: {exc}"
        )
    if announce.get("present") or int(announce.get("announced", 0)) > 0:
        # The dispatcher DID start (announce dir exists / tasks announced) but no
        # jobmap — a non-shared-FS split or a marker write that lost the race.
        # Adopting is impossible (no id) and resubmitting would duplicate a live
        # array — stay submitting (UNKNOWN), surface for the operator.
        return _stay_submitting(
            experiment_dir,
            run_id,
            reason="jobmap absent but announce census present — possible non-shared-FS "
            "split; refusing to resubmit a possibly-live array",
        )
    return _safe_resubmit(
        experiment_dir,
        run_id,
        record=record,
        ssh_target=ssh_target,
        reason="jobmap dir absent and announce census absent: submit never dispatched",
    )


def _safe_resubmit(
    experiment_dir: Path,
    run_id: str,
    *,
    record: RunRecord,
    ssh_target: str,
    reason: str,
) -> RunRecord:
    """Transition ``submitting → abandoned`` (resubmittable) and clear the jobmap.

    The next plain submit sees a resubmittable-terminal record
    (:func:`is_resubmittable_terminal`) → ``_PROCEED`` and mints ``attempt+1``
    (:func:`allocate_attempt`); the cleared marker cannot adopt onto it. Reconcile
    is the ONLY path that performs this transition (the sole-owner invariant).
    """
    _log.warning(
        "reconcile: run %s was never accepted by the scheduler (%s) — transitioning "
        "submitting→abandoned (safe to re-submit as attempt %d); clearing jobmap.",
        run_id,
        reason,
        int(record.attempt) + 1,
    )
    _clear_jobmap(ssh_target=ssh_target, remote_path=record.remote_path, run_id=run_id)
    update_run_status(
        experiment_dir,
        run_id,
        last_status={
            "verdict": "abandoned",
            "verdict_reason": NEVER_DISPATCHED_VERDICT_REASON,
            "recovery_note": reason,
        },
    )
    return mark_run(experiment_dir, run_id, status=str(JournalStatus.ABANDONED))


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

    # Capture the PRE-reconcile journal status so the settle-arm harvest can
    # fire only on a real verdict TRANSITION (see the gate below). ``record`` is
    # never reassigned in this function (only ``updated`` is), so this stays the
    # status as loaded even after ``update_run_status`` writes fresh fields.
    pre_reconcile_status = str(record.status)

    # Submit-once entry condition (U3-d): a ``submitting`` record is caught in /
    # orphaned in its dispatch→id window. Reconcile is the SOLE transition-out
    # owner — route to the recovery ladder (adopt from the cluster jobmap marker,
    # disambiguate by the U3-c correlation token, or safe-resubmit) instead of the
    # normal alive/reporter probe path, which has nothing to probe (no job_ids
    # yet) and would leave the run stranded ``submitting`` forever.
    if record.status == str(JournalStatus.SUBMITTING):
        return _recover_submitting(
            experiment_dir, run_id, record=record, scheduler=scheduler
        ), False

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
    # Crash-only Phase-1 progress evidence from a PARTIAL announcement (set in
    # the ssh branch below); threaded into the persisted ``last_status`` after
    # the probe builds ``summary`` so it survives the probe's own write.
    announce_progress: dict[str, int] | None = None
    if not backend_requires_ssh(scheduler):
        # Pure-API path (#337 Increment 4): no login node, no shared
        # ``_combiner/`` dir. Liveness comes from the backend's ``alive_job_ids``
        # instance hook (a pure-API backend holds its own authenticated client);
        # there is no SSH status reporter and no wave-listing to do, so the
        # combiner waves stay as the journal recorded them.
        from hpc_agent.infra.backends.remote_factory import backend_for_record
        from hpc_agent.ops.monitor.status import _pure_api_status_summary

        reporter_failed = False
        combined = list(record.combined_waves)
        backend = backend_for_record(record, scheduler=scheduler)
        try:
            alive = backend.alive_job_ids(record.job_ids)
            alive_check_failed = False
        except Exception as exc:
            alive = list(record.job_ids)  # treat as still alive on error
            warnings.append(f"alive check: {exc}")
            alive_check_failed = True
        # #44: settle on REAL per-task counts, not a count-less summary. A
        # finished pure-API run whose jobs aged out of the live queue
        # (``alive == []``) would otherwise hit the settle guard below with a
        # ``{"checked_at": ...}`` summary — where ``all_tasks_complete`` is False
        # by construction — and be flipped ``abandoned`` (the #351 "finished run
        # read as abandoned" class, re-opened on the pure-API path). Mirror
        # ``record_status``'s pure-API branch: pull ``task_statuses`` and build
        # the SAME summary shape (``_pure_api_status_summary``), so ``settle``
        # sees the completion/failure evidence the backend can prove.
        try:
            per_task = backend.task_statuses(record.job_ids, total_tasks=record.total_tasks)
        except NotImplementedError:
            # Liveness-only backend: no per-task counts exist. Keep the
            # count-less summary — ``settle`` then yields the pinned ``abandoned``
            # for a genuinely evidence-less record (the liveness-only contract).
            summary = {"checked_at": utcnow_iso()}
        except Exception as exc:  # noqa: BLE001 — a real query failure is UNKNOWN, not abandon
            # A task_statuses failure (auth/network) is not evidence of
            # abandonment — route through ``unable_to_verify`` (reporter_failed)
            # exactly like an ssh reporter failure, never silent abandon.
            summary = {"checked_at": utcnow_iso()}
            warnings.append(f"task statuses: {exc}")
            reporter_failed = True
        else:
            summary = _pure_api_status_summary(per_task)
    else:
        # --- Crash-only Phase-1 announce fast path (docs/design/crash-only-monitoring.md) ---
        # The dispatcher writes ONE per-task marker file on its terminal
        # bookkeeping; read them in a SINGLE bounded ssh exec BEFORE the heavy
        # 3-way probe. Presence of ANY marker is the capability signal — zero
        # markers (a pre-announce wheel/run) falls through to the probe path
        # BYTE-IDENTICALLY. A FULL announcement (announced == total_tasks)
        # settles the lifecycle exactly as the reporter-backed settle arm does
        # for the same counts, WITHOUT the 20-25 min reporter walk (run-12
        # findings 20/24); a PARTIAL one is progress evidence only and NEVER
        # settles terminal. A kill-confirmed run is left to the
        # reporter-independent kill arm below (a deliberate kill's verdict does
        # not come from task announcements). Best-effort: any failure (ssh blip,
        # truncated read) falls through to the probes unchanged.
        if not is_kill_confirmed(record):
            try:
                _announce = read_announcements(
                    ssh_target=resolve_ssh_target(record),
                    remote_path=record.remote_path,
                    run_id=run_id,
                    task_count=record.total_tasks,
                )
            except Exception:  # noqa: BLE001 — fast path is best-effort; fall through to probes
                _announce = None
            if _announce is not None and _announce["announced"] > 0:
                terminal = _settle_from_announcements(
                    experiment_dir,
                    run_id,
                    scheduler=scheduler,
                    record=record,
                    announce=_announce,
                    pre_reconcile_status=pre_reconcile_status,
                )
                if terminal is not None:
                    # Full announcement settled the run terminal — done, no probe.
                    return terminal, False
                # Partial announcement: surface the counts as progress evidence
                # and continue to the existing probes (never settle from partial).
                announce_progress = {
                    "announced": int(_announce["announced"]),
                    "complete": int(_announce["complete"]),
                    "failed": int(_announce["failed"]),
                    "missing": int(_announce["missing"]),
                }
        # Rank 19 (``docs/plans/latency-audit-2026-07-15``): a PARTIAL
        # announcement already answers the mid-flight lifecycle question — the
        # announce census (complete/failed/missing) plus the cheap alive probe
        # suffice to say "still in flight, N complete". So when ANY announcement
        # is present we SKIP the per-task status-reporter WALK (``_ssh_status_
        # report`` — the 20-25 min run-12 findings 20/24 cost) and derive
        # ``summary`` from the census. The walk still runs on the SETTLE path
        # (nothing alive → a terminal verdict needs per-task failure evidence) and
        # BYTE-IDENTICALLY on every announce-absent tick. A FULL announcement never
        # reaches here (it returned terminal above). Pinned by
        # tests/ops/monitor/test_reconcile_announce.py::
        # test_partial_mid_flight_skips_reporter_walk.
        skip_walk = announce_progress is not None
        with ThreadPoolExecutor(max_workers=3) as pool:
            _resolved_ssh_target = resolve_ssh_target(record)
            fut_status = (
                None
                if skip_walk
                else pool.submit(
                    _ssh_status_report,
                    ssh_target=_resolved_ssh_target,
                    remote_path=record.remote_path,
                    run_id=run_id,
                    job_ids=record.job_ids,
                    job_name=record.job_name,
                    file_glob=file_glob,
                    remote_activation=remote_activation_for_sidecar(
                        _sidecar, fallback_cluster=getattr(record, "cluster", None)
                    ),
                )
            )
            fut_waves = pool.submit(
                _ssh_list_combined_waves,
                ssh_target=_resolved_ssh_target,
                remote_path=record.remote_path,
            )
            fut_alive = pool.submit(
                _ssh_alive_job_ids,
                ssh_target=_resolved_ssh_target,
                job_ids=record.job_ids,
                scheduler=scheduler,
            )

            # Resolve the CHEAP probes first: the ``alive`` verdict decides whether
            # the walk-skip holds (mid-flight) or the settle path must still walk.
            # Each future has its own try/except: an SSH blip on any of them must
            # not abort the journal update. In particular, falling back to the
            # *current* job_ids on the alive-check path is essential — defaulting to
            # empty would mark a healthy run ``abandoned`` whenever the SSH check
            # itself failed.
            alive_check_failed = False
            try:
                combined = fut_waves.result()
            except Exception as exc:
                combined = list(record.combined_waves)
                warnings.append(f"wave list: {exc}")

            try:
                alive = fut_alive.result()
            except Exception as exc:
                alive = list(record.job_ids)  # treat as still alive on error
                warnings.append(f"alive check: {exc}")
                alive_check_failed = True

            if fut_status is not None:
                # Announce-absent tick: the walk ran in PARALLEL — read it.
                try:
                    report = fut_status.result()
                    summary = dict(report.get("summary", {}))
                    reporter_failed = False
                except Exception as exc:
                    summary = {"error": str(exc)}
                    warnings.append(f"status reporter: {exc}")
                    reporter_failed = True
            elif not alive:
                # SETTLE path: the census is PARTIAL but nothing is alive — a
                # terminal verdict needs the per-task failure evidence only the
                # reporter walk carries (``_failed_evidence_task_ids`` /
                # ``_gather_failure_features`` below). Run it now, synchronously —
                # the pool's other probes have already returned. This is the ONE
                # announce-present tick that still walks, and only to settle.
                try:
                    report = _ssh_status_report(
                        ssh_target=_resolved_ssh_target,
                        remote_path=record.remote_path,
                        run_id=run_id,
                        job_ids=record.job_ids,
                        job_name=record.job_name,
                        file_glob=file_glob,
                        remote_activation=remote_activation_for_sidecar(
                            _sidecar, fallback_cluster=getattr(record, "cluster", None)
                        ),
                    )
                    summary = dict(report.get("summary", {}))
                    reporter_failed = False
                except Exception as exc:
                    summary = {"error": str(exc)}
                    warnings.append(f"status reporter: {exc}")
                    reporter_failed = True
            else:
                # Mid-flight with a PARTIAL announcement: the census counts + the
                # live ``alive`` probe ARE the lifecycle evidence — no walk. The
                # census-derived summary records ``status_source ==
                # "task_announcements"`` so the brief / downstream readers know the
                # census stood in for the walk (fallback-disclosure preserved).
                # ``fut_status is None`` only when ``skip_walk`` — which is exactly
                # ``announce_progress is not None`` — so the narrowing holds.
                assert announce_progress is not None
                summary = _census_progress_summary(announce_progress)
                reporter_failed = False
            summary["checked_at"] = utcnow_iso()
            if isinstance(report.get("waves"), dict) and report["waves"]:
                summary["waves"] = report["waves"]

    # Crash-only Phase-1: a PARTIAL announcement is progress evidence. Threaded
    # in here (after the probe built ``summary``) so it rides the SAME persisted
    # ``last_status`` — the ``{**summary, ...}`` spreads in the settle/kill arms
    # and the main ``update_run_status`` below all carry it out to the envelope.
    if announce_progress is not None:
        summary["task_announcements"] = announce_progress

    if warnings:
        summary["warnings"] = warnings

    # Kill-confirmed terminal short-circuit (proving run #5, finding 14).
    #
    # A deliberate kill the scheduler CONFIRMED gone (``is_kill_confirmed``:
    # ``kill_confirmed_at`` stamped AND every requested job id covered by
    # ``kill_confirmed_job_ids``) is terminal from the KILL evidence alone — the
    # status reporter's per-task counts are irrelevant to a deliberate kill. Key
    # this off the fresh alive probe too (``not alive``): when the alive-check
    # itself couldn't run it falls back to the live job_ids, so ``not alive`` is
    # False and we conservatively route through ``unable_to_verify`` below rather
    # than settle on a probe that didn't run.
    #
    # Without this pre-branch, a broken cluster env crashes the per-task reporter
    # (``reporter_failed=True``) — the reporter-dependent settle branch further
    # down is guarded on ``not reporter_failed`` and is skipped — so a
    # KILL-CONFIRMED run wrongly stayed ``in_flight`` (surfaced
    # ``unable_to_verify``), blocked the next submit, and forced the driver to
    # hand-choreograph reconcile→supersede.
    #
    # The verdict is ``abandoned``: exactly what ``classify.settle`` already
    # yields for a killed run when the reporter DOES work (killed mid-flight = not
    # complete, no positive ``failed`` count = abandoned). This preserves ONE
    # verdict for a killed run across both reporter-healthy and reporter-broken
    # paths rather than inventing a new state, and routes through the SAME
    # ``mark_run`` + transition-gated ``harvest_on_terminal`` the settle arm uses.
    # Placed BEFORE the ``unable_to_verify`` marking so the run settles TERMINAL
    # (not unverifiable — otherwise the envelope's ``verify_state`` override would
    # mask the terminal state); keyed STRICTLY on kill-confirmation so a non-kill
    # reporter failure still routes through ``unable_to_verify``.
    if is_kill_confirmed(record) and not alive:
        recorded = {**summary, "verdict_reason": "killed_confirmed_reporter_independent"}
        update_run_status(
            experiment_dir,
            run_id,
            last_status=recorded,
            combined_waves=combined,
            failed_waves=[w for w in record.failed_waves if w not in set(combined)],
        )
        updated = mark_run(experiment_dir, run_id, status=str(LifecycleState.ABANDONED))
        # Guaranteed harvest (§5) — identical to the settle arm below: reconcile
        # is invoked directly (kill / driver), so its terminal transitions never
        # pass through the poll loop's finally. Fire on a verdict TRANSITION, OR
        # as a journal-evidence backstop when the run is terminal with no harvest
        # receipt (a death in the mark_run→harvest window); an idempotent
        # re-reconcile of an already-harvested kill does NOT re-fire.
        _harvest_if_owed(
            experiment_dir,
            run_id,
            terminal_cause=str(LifecycleState.ABANDONED),
            record=updated,
            pre_reconcile_status=pre_reconcile_status,
        )
        return updated, alive_check_failed

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
            from hpc_agent.state.runs import read_job_task_spans

            features = _gather_failure_features(
                ssh_target=resolve_ssh_target(record),
                remote_path=record.remote_path,
                job_name=record.job_name,
                job_ids=list(record.job_ids),
                scheduler=scheduler,
                task_ids=_failed_evidence_task_ids(report),
                # Waved runs: probe the covering job with the job-LOCAL log
                # index; None (old sidecar / single array) keeps the global
                # probe — read_job_task_spans never raises.
                job_task_spans=read_job_task_spans(experiment_dir, run_id),
            )
            update_run_status(
                experiment_dir, run_id, last_status={**recorded, "failure_features": features}
            )
            updated = mark_run(experiment_dir, run_id, status="failed")
        else:
            update_run_status(experiment_dir, run_id, last_status=recorded)
            updated = mark_run(experiment_dir, run_id, status=str(decision.verdict))
        # Guaranteed harvest (§5). reconcile's settle arms are terminal
        # transitions the poll loop's own finally never sees (reconcile is
        # invoked directly by drivers / the skill), so the "no path ends in
        # silence" sweep must fire HERE too. It fires on a verdict TRANSITION
        # from the pre-reconcile status, OR — the rank-2/U8 backstop — when the
        # run is terminal but carries NO harvest receipt (a session-death in the
        # mark_run→harvest window), derived from the durable harvest ledger. An
        # idempotent re-reconcile of an already-harvested run does NOT re-fire
        # (each fire pays an rsync pull + reduce + a ledger append). Not a
        # "terminal is sticky" guard — the verdict stays revisable
        # (engineering-principles): a legit complete→failed downgrade IS a
        # transition and still harvests. Best-effort and loud by contract —
        # never raises, never masks the verdict just recorded.
        _harvest_if_owed(
            experiment_dir,
            run_id,
            terminal_cause=str(decision.verdict),
            record=updated,
            pre_reconcile_status=pre_reconcile_status,
        )
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
