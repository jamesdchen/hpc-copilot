"""``write-run-sidecar`` primitive — agent-facing CLI for the sidecar write.

Wraps :func:`hpc_agent.state.runs.write_run_sidecar` so an agent can
satisfy the write-first guard (``_write_first_error`` in
``hpc_agent.ops.submit_flow``) by shelling out instead of importing the
function via Python introspection (#200).

Auto-stamps the two fields the underlying function requires but that
no caller has any business synthesising — ``submitted_at`` (current
UTC) and ``hpc_agent_version`` (the running framework version) — so
the agent only has to resolve the per-run identity + cluster contract
fields the :class:`WriteRunSidecarInput` model carries.

The primitive also pre-rejects sidecars whose ``executor`` is the
job-script dispatcher (#162): the sidecar's ``executor`` field MUST be
the real per-task command (e.g. ``python train.py --seed $SEED``); the
dispatcher path lives in the submit-flow spec's
``job_env["EXECUTOR"]``. Letting a dispatcher-shaped value through here
would let the array self-recurse at the new CLI surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent import __version__ as _hpc_agent_version
from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.submit_flow import _is_runnable_executor
from hpc_agent.state.runs import write_run_sidecar as _write_run_sidecar


@primitive(
    name="write-run-sidecar",
    verb="mutate",
    side_effects=[SideEffect("file_write", "<experiment>/.hpc/runs/<run_id>.json")],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Write the per-run sidecar JSON at "
            "<experiment>/.hpc/runs/<run_id>.json. Use this to satisfy "
            "the submit-flow write-first guard via the CLI instead of "
            "introspecting the Python helper. Auto-stamps submitted_at "
            "and hpc_agent_version."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=WriteRunSidecarInput,
        schema_ref=SchemaRef(input="write_run_sidecar"),
    ),
    agent_facing=True,
)
def write_run_sidecar(*, experiment_dir: Path, spec: WriteRunSidecarInput) -> dict[str, Any]:
    """Write the sidecar described by *spec* under *experiment_dir*.

    Returns ``{"path": str(target)}`` where *target* is the absolute
    path of the written sidecar file (``<experiment>/.hpc/runs/<run_id>.json``).

    Raises
    ------
    :class:`errors.SpecInvalid`
        ``spec.executor`` is the job-script dispatcher (``dispatch.py``
        in the command), which would let the cluster array self-recurse
        (#162). The fix is to pass the REAL per-task command — e.g.
        ``python train.py --seed $SEED``.
    """
    if not _is_runnable_executor(spec.executor):
        raise errors.SpecInvalid(
            f"write-run-sidecar refused: executor {spec.executor!r} is the "
            "job-script dispatcher, not a per-task command. The sidecar's "
            "executor field must be the REAL per-task command (e.g. "
            "`python train.py --seed $SEED`); the dispatcher path belongs "
            "in the submit-flow spec's job_env['EXECUTOR'], not here (#162)."
        )

    target = _write_run_sidecar(
        Path(experiment_dir),
        run_id=spec.run_id,
        cmd_sha=spec.cmd_sha,
        hpc_agent_version=_hpc_agent_version,
        submitted_at=utcnow_iso(),
        executor=spec.executor,
        result_dir_template=spec.result_dir_template,
        task_count=spec.task_count,
        tasks_py_sha=spec.tasks_py_sha,
        wave_map=spec.wave_map,
        extra=spec.extra,
        cluster=spec.cluster,
        profile=spec.profile,
        campaign_id=spec.campaign_id,
        project=spec.project,
        remote_path=spec.remote_path,
        resources=spec.resources,
        env=spec.env,
        env_group=spec.env_group,
        constraints=spec.constraints,
        gpu_fallback=spec.gpu_fallback,
        max_retries=spec.max_retries,
        runtime=spec.runtime,
        auto_retry=spec.auto_retry,
        aggregate_defaults=spec.aggregate_defaults,
        results=spec.results,
        trial_tokens=spec.trial_tokens,
        job_ids=spec.job_ids,
    )
    return {"path": str(target)}
