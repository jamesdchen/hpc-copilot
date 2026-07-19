"""Scheduler-in-a-container smoke test: the REAL submit spine, end to end.

This is the one test that converts the "found live in proving run #N" bug class
into "found in CI". It drives the framework's three workflow atoms against a
REAL single-node scheduler running inside a container (over SSH) — Slurm or
SGE, selected by ``HPC_SCHEDULER_IT_FAMILY`` (default ``slurm``):

    submit_flow  →  monitor_flow  →  aggregate_flow

exercising, with NO mocks on the transport or scheduler seam:

  * pre-flight SSH probe + rsync/tar push (``infra.transport.rsync_push``)
  * runtime deploy (``deploy_runtime`` scp's the job templates + framework
    stubs into ``<remote>/.hpc/``)
  * the tiny-batch canary auto-skip (``total_tasks <= 4`` → no canary; #263)
  * array submit over SSH via the family's remote backend
    (``sbatch --array=1-2`` on slurm, ``qsub -t 1-2`` on sge)
  * the cluster-side dispatcher (``.hpc/_hpc_dispatch.py``) resolving each
    task's kwargs from ``.hpc/tasks.py`` and running the per-task executor,
    which writes ``metrics.json`` into the promoted ``RESULT_DIR``
  * the real status reporter polled to terminal over SSH
    (``sacct``/``squeue`` on slurm; ``qstat``/``qacct`` on sge — where, by
    container design (ci/sge), those binaries resolve ONLY via the login
    profile chain, so the reporter's non-login subprocess degrades to
    file-based completion detection exactly as on a real SGE login node)
  * the cluster-side combiner + local reduce (``aggregate_flow``)

It is INERT anywhere the container env vars are absent (local dev, the main CI
matrix): the module-level ``skipif`` collects it but skips cleanly. The
containers + workflow that make it live are in ``ci/slurm/`` / ``ci/sge/`` and
``.github/workflows/scheduler-integration.yml``; the design + local-repro
recipe are in ``docs/internals/scheduler-integration-ci.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec, SubmitResources
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.ops.submit_flow import submit_flow
from hpc_agent.state.runs import write_run_sidecar

# ``slow`` opts out of the hermetic cluster-binary shim (tests/conftest.py) so
# the real ssh/rsync are reachable; ``scheduler_integration`` is this tier's
# selector (registered in the local conftest). ``skipif`` keeps the suite inert
# without the container — evaluated at collection, skips cleanly, never errors.
pytestmark = [
    pytest.mark.slow,
    pytest.mark.scheduler_integration,
    pytest.mark.skipif(
        os.environ.get("HPC_SCHEDULER_IT") != "1",
        reason=(
            "scheduler-in-a-container integration test: set HPC_SCHEDULER_IT=1 "
            "and provide the container env (HPC_CLUSTERS_CONFIG + the SSH-able "
            "scheduler container). See docs/internals/scheduler-integration-ci.md."
        ),
    ),
]


# The per-task executor the tasks land on the cluster. Reads its swept kwarg
# from the HPC_KW_* env the dispatcher exports, and writes a metrics.json the
# combiner reduces. Stdlib + the deployed hpc_agent stub only (both present in
# the container's python), so no experiment conda env is needed.
_TRAIN_PY = '''\
"""Tiny per-task executor for the scheduler-integration smoke test."""
from hpc_agent.execution.mapreduce.metrics_io import read_kw_env, write_metrics

kw = read_kw_env()
x = float(kw.get("x", "0"))
# A trivial, deterministic metric the combiner can weighted-mean across tasks.
write_metrics({"value": x * 2.0, "n_samples": 1})
'''

# Two independent tasks — under the canary-skip threshold (4), so the main
# array's own first tasks are the smoke test and no separate canary fires.
_TASKS_PY = """\
_TASKS = [{"x": 1}, {"x": 2}]


def total():
    return len(_TASKS)


def resolve(i):
    return _TASKS[i]
"""


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val else default


# Scheduler families the smoke can run against, selected by
# ``HPC_SCHEDULER_IT_FAMILY`` (default ``slurm`` so the original lane and the
# local-repro docs keep working unchanged). Each entry: the ``backend`` wire
# value for :class:`SubmitFlowSpec` + the array-script path ``deploy_runtime``
# lands for that family (the ``("sge", False)`` / ``("slurm", False)`` rows of
# build/submit_spec.py's template map). Env selection rather than
# ``pytest.mark.parametrize``: the workflow runs ONE container per job (the
# slurm-smoke and sge-smoke jobs are separate runner VMs), so parametrizing
# over families would demand both containers on one runner for zero gain.
_FAMILIES: dict[str, tuple[str, str]] = {
    "slurm": ("slurm", ".hpc/templates/cpu_array.slurm"),
    "sge": ("sge", ".hpc/templates/cpu_array.sh"),
}


def _fabricate_experiment(experiment_dir: Path, *, run_id: str, remote_path: str, cluster: str):
    """Lay down a minimal, complete 2-task experiment and its run sidecar.

    Mirrors the on-disk shape ``/wrap-entry-point`` + ``build-submit-spec``
    produce: ``.hpc/tasks.py`` (the ``total()``/``resolve(i)`` contract the
    dispatcher imports) + a per-task executor script + the per-run sidecar the
    dispatcher and combiner read. We pre-write the sidecar because submit-flow
    only SYNTHESISES one from a real per-task command, and here the job-script
    EXECUTOR is the dispatcher (the two-layer executor contract).
    """
    hpc = experiment_dir / ".hpc"
    hpc.mkdir(parents=True, exist_ok=True)
    (hpc / "tasks.py").write_text(_TASKS_PY, encoding="utf-8")
    (experiment_dir / "train.py").write_text(_TRAIN_PY, encoding="utf-8")

    cmd_sha = hashlib.sha256(run_id.encode("utf-8")).hexdigest()  # 64 hex
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha=cmd_sha,
        hpc_agent_version="0.0.0+scheduler-it",
        submitted_at="2026-01-01T00:00:00Z",
        # The REAL per-task command (distinct from the job-script EXECUTOR,
        # which is the dispatcher). Has an interpreter + a path token, so it
        # passes the runnable-executor + bare-name guards.
        executor="python3 train.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=2,
        tasks_py_sha="",
        wave_map={"0": [0, 1]},
        cluster=cluster,
        remote_path=remote_path,
    )
    return cmd_sha


def _submit_spec(
    *, run_id, cmd_sha, ssh_target, remote_path, cluster, backend, script
) -> SubmitFlowSpec:
    return SubmitFlowSpec(
        profile="cpu",
        cluster=cluster,
        ssh_target=ssh_target,
        remote_path=remote_path,
        job_name="scheduler_it",
        run_id=run_id,
        total_tasks=2,
        backend=backend,
        # Deployed by deploy_runtime into <remote_path>/.hpc/templates/.
        script=script,
        job_env={
            # The job-script command = the dispatcher (NOT the per-task cmd).
            "EXECUTOR": "python3 .hpc/_hpc_dispatch.py",
            "HPC_RUN_ID": run_id,
            "HPC_CMD_SHA": cmd_sha,
            "HPC_TASK_COUNT": "2",
            "REPO_DIR": remote_path,
            # No conda/module activation in the container — the system python3
            # carries hpc_agent. Empty activation makes the preamble skip
            # module/conda setup and use the login shell's python3.
            "MODULES": "",
            "CONDA_SOURCE": "",
            "CONDA_ENV": "",
        },
        # Tiny resource asks OVERRIDE the template's 16G/4-cpu/6h #SBATCH
        # directives (a CLI flag beats a directive) so the job fits the
        # deliberately under-provisioned container node.
        resources=SubmitResources(mem_mb=256, cpus=1, walltime_sec=300),
    )


def test_submit_monitor_aggregate_against_real_scheduler(tmp_path: Path) -> None:
    """The full spine: submit → poll-to-terminal → reduce, on a real scheduler."""
    family = _env("HPC_SCHEDULER_IT_FAMILY", "slurm")
    if family not in _FAMILIES:
        raise ValueError(f"unknown HPC_SCHEDULER_IT_FAMILY {family!r}; known: {sorted(_FAMILIES)}")
    backend, script = _FAMILIES[family]
    ssh_target = _env("HPC_SCHEDULER_IT_SSH_TARGET", "hpcuser@slurmci")
    remote_base = _env("HPC_SCHEDULER_IT_REMOTE_BASE", "/home/hpcuser/scratch")
    cluster = _env("HPC_SCHEDULER_IT_CLUSTER", "slurmci")

    run_id = f"scheduler_it_{uuid.uuid4().hex[:8]}"
    experiment_dir = tmp_path / "experiment"
    experiment_dir.mkdir()
    remote_path = f"{remote_base}/{run_id}"

    cmd_sha = _fabricate_experiment(
        experiment_dir, run_id=run_id, remote_path=remote_path, cluster=cluster
    )

    # 1) SUBMIT — real pre-flight + rsync push + deploy + array submit over SSH.
    submit = submit_flow(
        experiment_dir,
        spec=_submit_spec(
            run_id=run_id,
            cmd_sha=cmd_sha,
            ssh_target=ssh_target,
            remote_path=remote_path,
            cluster=cluster,
            backend=backend,
            script=script,
        ),
    )
    assert not submit.deduped, "fresh run_id must not dedup"
    assert submit.main_launched, "main array should have launched"
    assert not submit.canary_done, "2-task batch (<=4) must auto-skip the canary (#263)"
    assert submit.job_ids, f"expected a scheduler job id, got {submit.job_ids!r}"
    assert submit.total_tasks == 2

    # 2) MONITOR — the real reporter polled to terminal over SSH. On sge the
    # reporter's qstat/qacct subprocess runs on the NON-login channel (where
    # the container deliberately does NOT resolve the scheduler binaries —
    # the F7 dialect), so completion is detected from the result files, the
    # same degradation path a real SGE login node exercises.
    monitored = monitor_flow(
        experiment_dir,
        spec=MonitorFlowSpec(
            run_id=run_id,
            poll_interval_seconds=5,
            wall_clock_budget_seconds=600,
            file_glob="metrics.json",
        ),
    )
    assert monitored.lifecycle_state == "complete", (
        f"run did not complete: lifecycle_state={monitored.lifecycle_state!r} "
        f"escalation_reason={monitored.escalation_reason!r} "
        f"last_status={monitored.last_status!r}"
    )

    # 3) AGGREGATE — cluster-side combine + local reduce; results must land.
    aggregated = aggregate_flow(
        experiment_dir,
        spec=AggregateFlowSpec(run_id=run_id),
    )
    assert aggregated.escalation_reason is None, (
        f"aggregate escalated: {aggregated.escalation_reason!r} "
        f"failed_waves={aggregated.failed_waves!r}"
    )
    assert aggregated.aggregated_metrics, (
        "aggregated_metrics is empty — no per-task results reduced "
        f"(combined_waves={aggregated.combined_waves!r})"
    )

    # The reduced aggregate must have persisted locally.
    agg_json = experiment_dir / "_aggregated" / run_id / "metrics_aggregate.json"
    assert agg_json.is_file(), f"expected persisted aggregate at {agg_json}"
    doc = json.loads(agg_json.read_text(encoding="utf-8"))
    assert doc, "persisted aggregate json is empty"
