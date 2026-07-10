"""``verify-submitted`` query — per-job scheduler state for a fresh array (#157).

``qsub``/``sbatch`` returning a job id is necessary but not sufficient: an SGE
array can land in ``Eqw`` (error) and a SLURM job can be held — both of which a
plain alive-check (``qstat -u`` / ``squeue``) reports as merely "present." This
verb runs the backend's scheduler-state command over the ``ssh_argv`` seam,
maps each submitted job id to its raw state, and classifies error/held — so the
submit worker's Step 8b post-submit check is a *verb call*, not raw
``ssh … qstat`` (the verbs-over-raw-ssh principle, #151 / #157).

Read-only: it queries the scheduler and the journal; it never mutates either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="verify-submitted",
    verb="query",
    side_effects=[SideEffect("ssh", "<cluster> (scheduler state query)")],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Post-submit health check: map each of a run's submitted job ids to "
            "its scheduler state and flag error (SGE Eqw) / held jobs. Returns "
            "{ok, states, healthy, error, held, missing, details}. Use it instead "
            "of raw `ssh qstat` to confirm an array landed cleanly."
        ),
        experiment_dir_arg=True,
        requires_ssh=True,
        args=(
            CliArg(
                "--run-id",
                type=str,
                required=True,
                help="Run ID whose submitted job_ids to check (read from the journal).",
            ),
        ),
    ),
    agent_facing=True,
)
def verify_submitted(experiment_dir: Path, *, run_id: str) -> dict[str, Any]:
    """Query per-job scheduler state for *run_id*'s submitted jobs.

    Loads the run's ``job_ids`` + ``ssh_target`` + ``cluster`` from the
    journal, resolves the scheduler from ``clusters.yaml``, runs the backend's
    :meth:`build_scheduler_state_cmd` over SSH, then maps each job id to its
    state and buckets error/held. ``ok`` is True iff no job is error/held.

    Raises :class:`errors.SpecInvalid` on a missing journal record / no
    job_ids / unresolvable scheduler; :class:`errors.SshUnreachable` on an SSH
    transport failure.
    """
    if not run_id:
        raise errors.SpecInvalid("run_id is required")

    from hpc_agent.infra import remote
    from hpc_agent.infra.backends import get_backend_class
    from hpc_agent.infra.clusters import load_clusters_config
    from hpc_agent.state.journal import load_run

    record = load_run(experiment_dir, run_id)
    if record is None:
        raise errors.SpecInvalid(f"no journal record for run_id={run_id!r}")
    job_ids = [str(j) for j in (record.job_ids or [])]
    if not job_ids:
        raise errors.SpecInvalid(f"run_id={run_id!r} has no recorded job_ids to check")

    try:
        clusters_cfg = load_clusters_config()
    except Exception:  # noqa: BLE001
        clusters_cfg = {}
    scheduler = (clusters_cfg.get(record.cluster) or {}).get("scheduler")
    if not scheduler:
        raise errors.SpecInvalid(
            f"cannot resolve scheduler for cluster {record.cluster!r}: absent from "
            "clusters.yaml or missing a 'scheduler' key — refusing to guess."
        )

    backend_cls = get_backend_class(scheduler)
    cmd = backend_cls.build_scheduler_state_cmd(job_ids)
    proc = remote.ssh_run(cmd, ssh_target=record.ssh_target)
    if proc.returncode != 0:
        # A non-zero rc is an SSH transport failure, not "no jobs found" —
        # surface it as such rather than reporting all gone.
        raise errors.SshUnreachable(
            f"scheduler-state query for {run_id!r} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:200]}"
        )

    # Positive-evidence transport verdict (docs/design/connection-broker.md):
    # the query proves it RAN by echoing an affirmative ack token. An empty read
    # without it is a silently truncated / never-run channel (or the scheduler
    # binary failed) — UNKNOWN, not "every submitted job already left the queue".
    # Reading absence as terminal here would report a freshly-landed array as
    # entirely `missing` (never landed) on one silent blip.
    clean, ran_ok = backend_cls.scheduler_query_ran(proc.stdout)
    if not ran_ok:
        raise errors.SshUnreachable(
            f"scheduler-state query for {run_id!r} returned no positive-evidence "
            "ack (silent/empty read — the query did not run to completion, or the "
            "scheduler binary itself failed); refusing to read absence as "
            "'all submitted jobs already terminal'."
        )

    states = backend_cls.parse_scheduler_states(clean, job_ids)
    healthy: list[str] = []
    error: list[str] = []
    held: list[str] = []
    for jid in job_ids:
        state = states.get(jid)
        if state is None:
            continue  # not in the queue → reported under `missing`
        kind = backend_cls.classify_scheduler_state(state)
        if kind == "error":
            error.append(jid)
        elif kind == "held":
            held.append(jid)
        else:
            healthy.append(jid)
    missing = [jid for jid in job_ids if jid not in states]

    ok = not error and not held
    if error or held:
        details = (
            f"run {run_id!r}: {len(error)} job(s) in error state, {len(held)} held "
            f"(states={states}). Do NOT treat the array as launched cleanly."
        )
    elif missing:
        details = (
            f"run {run_id!r}: {len(healthy)} job(s) alive; {len(missing)} not in the "
            f"queue ({missing}) — already terminal, or never landed. Verify intent."
        )
    else:
        details = f"run {run_id!r}: all {len(healthy)} job(s) queued/running, none in error/held."

    return {
        "ok": ok,
        "states": states,
        "healthy": healthy,
        "error": error,
        "held": held,
        "missing": missing,
        "details": details,
    }
