"""Reconcile + mark-terminal runner primitives."""

from __future__ import annotations

import shlex
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra import remote
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.monitor.status import _ssh_status_report
from hpc_agent.state.journal import load_run, mark_run, update_run_status

if TYPE_CHECKING:
    from hpc_agent.state.run_record import RunRecord


def _ssh_list_combined_waves(*, ssh_target: str, remote_path: str) -> list[int]:
    """Derive ``combined_waves`` from cluster artifacts.

    The combiner writes ``_combiner/wave_<N>.json`` per successful run
    (see ``hpc_agent/models/mapreduce/combiner.py``). We use the
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


def _reconcile_envelope(record: RunRecord) -> dict[str, Any]:
    """Project ``RunRecord`` into the ``reconcile.output.json`` envelope shape.

    The envelope ``lifecycle_state`` is the journal status, EXCEPT when the
    cluster alive-check could not run (SSH/auth/network failure): the journal
    status is left untouched (we couldn't verify it), but the envelope surfaces
    ``unable_to_verify`` (#258) so callers can distinguish "cluster says it's
    still running" from "we couldn't ask" — different remediations. The marker
    lives in ``last_status.verify_state`` (set by :func:`_reconcile_one`).
    """
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
                choices=("sge", "slurm", "pbspro", "torque"),
                help="Scheduler family — needed to query alive job IDs.",
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
) -> RunRecord:
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
    alive-check could not run (#258).
    """
    from hpc_agent.state.run_record import TERMINAL_STATUSES

    primary, _primary_alive_failed = _reconcile_one(
        experiment_dir, run_id, scheduler=scheduler, file_glob=file_glob
    )

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
        sibling_outcomes.append(
            {"run_id": sib_id, "lifecycle_state": sib_rec.status, "reconciled": True}
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
) -> tuple[RunRecord, bool]:
    """Reconcile a single run against the cluster; return ``(record, alive_check_failed)``.

    Re-derives ground truth from the cluster:
      A. Fresh status report -> ``last_status``.
      B. List ``_combiner/wave_*.json`` -> canonical ``combined_waves``
         (cluster wins; journal overwritten on drift).
      C. Cross-check ``job_ids`` against the scheduler; if zero are alive,
         flip ``status`` to ``"abandoned"``.

    All three SSH calls run concurrently. Writes the reconciled record
    back atomically. When the alive-check itself failed (SSH/auth/network),
    the run is NOT marked abandoned and ``last_status.verify_state`` is set to
    ``unable_to_verify`` (#258) so the envelope can surface that distinctly;
    the bool return mirrors it.
    """
    record = load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no run record for {run_id!r}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_status = pool.submit(
            _ssh_status_report,
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            run_id=run_id,
            job_ids=record.job_ids,
            job_name=record.job_name,
            file_glob=file_glob,
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

        warnings: list[str] = []
        report: dict[str, Any] = {}
        try:
            report = fut_status.result()
            summary = dict(report.get("summary", {}))
        except Exception as exc:
            summary = {"error": str(exc)}
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
            alive: list[str] | set[str] = fut_alive.result()
        except Exception as exc:
            alive = list(record.job_ids)  # treat as still alive on error
            warnings.append(f"alive check: {exc}")
            alive_check_failed = True

    if warnings:
        summary["warnings"] = warnings

    # #258: when the alive-check couldn't run, the run's true state is unknown.
    # Mark the snapshot so the envelope can surface ``unable_to_verify`` instead
    # of masquerading the stale journal status as a confirmed reading.
    if alive_check_failed:
        summary["verify_state"] = "unable_to_verify"

    fields: dict[str, Any] = {
        "last_status": summary,
        "combined_waves": combined,
        # Drop any failed_waves entries that are now combined.
        "failed_waves": [w for w in record.failed_waves if w not in set(combined)],
    }
    updated = update_run_status(experiment_dir, run_id, **fields)

    # Only mark abandoned when the alive check actually ran and found
    # nothing — never on SSH failure of the alive check itself.
    if record.job_ids and not alive and not alive_check_failed:
        updated = mark_run(experiment_dir, run_id, status="abandoned")
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
