"""Intake + cluster-side guards for two silent-canary failure modes (#191, #192).

Both surfaced live on the 0.8.0 inline-subagent path, where a worker-constructed
fields-file handed ``submit-flow`` a structurally-broken spec the cluster
"succeeded" on instantly:

* **#191** — ``job_env["EXECUTOR"]`` empty/missing → the job script runs
  ``time`` with no command, prints ``0.000``, exits 0. Canary "passes", main
  array fires the same no-op qsub.
* **#192** — ``pass_env_keys=[]`` (the natural-feeling JSON "no override")
  forwards ZERO vars to ``qsub -v``, so a *correctly-set* ``$EXECUTOR`` is
  stripped on the way to the scheduler — same broken job, different cause.

Defense is layered, and these tests pin each layer:
  - wire layer: ``SubmitFlowSpec`` refuses ``pass_env_keys=[]`` at construction.
  - factory layer: ``build_remote_backend`` treats ``[]`` and ``None`` alike
    ("forward all"), for any caller that bypasses the spec validator.
  - submit-flow layer: an empty job-script ``EXECUTOR`` is refused before qsub.
  - template layer: every array template fences ``$EXECUTOR`` with ``:?``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests._paths import TEMPLATES_DIR


def _spec(**overrides):
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

    base = dict(
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/r",
        job_name="j",
        run_id="rX",
        total_tasks=4,
        backend="sge",
        script="run.sh",
        job_env={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
        result_dir_template="results/{run_id}/task_{task_id}",
    )
    base.update(overrides)
    return SubmitFlowSpec(**base)


# ─── #192 B: wire layer refuses pass_env_keys=[] ───────────────────────────


def test_empty_pass_env_keys_is_rejected_at_construction() -> None:
    """``pass_env_keys=[]`` is the worst interpretation (forward nothing); the
    spec must refuse it with an actionable message rather than ship it."""
    with pytest.raises(ValidationError, match="pass_env_keys"):
        _spec(pass_env_keys=[])


def test_none_and_nonempty_pass_env_keys_are_accepted() -> None:
    """``null`` (forward all) and a non-empty list (restrict) both validate —
    only the empty list is refused."""
    assert _spec(pass_env_keys=None).pass_env_keys is None
    assert _spec(pass_env_keys=["EXECUTOR", "HPC_RUN_ID"]).pass_env_keys == [
        "EXECUTOR",
        "HPC_RUN_ID",
    ]


# ─── #192 A: factory treats [] and None alike (defense-in-depth) ───────────


def test_build_remote_backend_treats_empty_pass_env_keys_as_forward_all() -> None:
    """A caller that bypasses the spec validator and passes ``()``/``[]`` must
    still get "forward all" — not a backend that strips every key from qsub -v."""
    from hpc_agent.infra.backends.remote_factory import build_remote_backend

    job_env_keys = ("EXECUTOR", "CONDA_ENV", "REPO_DIR", "HPC_RUN_ID")

    forward_all = build_remote_backend(
        backend_name="sge",
        script="run.sh",
        ssh_target="u@h",
        remote_path="/r",
        pass_env_keys=None,
        job_env_keys=job_env_keys,
    )
    forward_empty = build_remote_backend(
        backend_name="sge",
        script="run.sh",
        ssh_target="u@h",
        remote_path="/r",
        pass_env_keys=(),
        job_env_keys=job_env_keys,
    )
    # [] / () must normalize to the SAME key set as None — every job_env key.
    assert tuple(forward_empty.pass_env_keys) == job_env_keys
    assert tuple(forward_all.pass_env_keys) == tuple(forward_empty.pass_env_keys)


def test_build_remote_backend_nonempty_pass_env_keys_restricts() -> None:
    """A non-empty list still restricts forwarding to exactly those keys."""
    from hpc_agent.infra.backends.remote_factory import build_remote_backend

    backend = build_remote_backend(
        backend_name="sge",
        script="run.sh",
        ssh_target="u@h",
        remote_path="/r",
        pass_env_keys=("EXECUTOR",),
        job_env_keys=("EXECUTOR", "CONDA_ENV", "REPO_DIR"),
    )
    assert tuple(backend.pass_env_keys) == ("EXECUTOR",)


# ─── #191 A: submit-flow refuses an empty job-script EXECUTOR ──────────────


@pytest.mark.parametrize("bad_executor", ["", "   ", None])
def test_ensure_job_script_executor_refuses_empty(bad_executor) -> None:
    """An empty / missing job-script EXECUTOR is refused before any qsub."""
    from hpc_agent import errors
    from hpc_agent.ops.submit_flow import _ensure_job_script_executor

    job_env = {"HPC_RUN_ID": "rX"}
    if bad_executor is not None:
        job_env["EXECUTOR"] = bad_executor
    with pytest.raises(errors.SpecInvalid, match="EXECUTOR"):
        _ensure_job_script_executor("rX", job_env)


def test_ensure_job_script_executor_accepts_the_dispatcher_command() -> None:
    """The job-script EXECUTOR is *supposed* to be the dispatcher command — the
    guard checks non-emptiness only, NOT runnability (unlike the sidecar's
    per-task executor, which must NOT be the dispatcher)."""
    from hpc_agent.ops.submit_flow import _ensure_job_script_executor

    # Must not raise — this is the canonical, correct value.
    _ensure_job_script_executor("rX", {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"})


# ─── #191 B: every array template fences $EXECUTOR with :? ──────────────────


@pytest.mark.parametrize(
    "template",
    [
        "runtime/sge/cpu_array.sh",
        "runtime/sge/gpu_array.sh",
        "runtime/slurm/cpu_array.slurm",
        "runtime/slurm/gpu_array.slurm",
    ],
)
def test_array_template_fences_executor(template: str) -> None:
    """The cluster-side shell guard is the last line of defense: a job that
    reaches the node with $EXECUTOR unset must fail loudly (``EXECUTOR is not
    set``) instead of running ``time`` with no command and exiting 0."""
    body = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
    assert '"${EXECUTOR:?' in body, (
        f'{template} is missing the `: "${{EXECUTOR:?...}}"` guard — without it '
        "an unset EXECUTOR runs `time` with no command and 'succeeds' silently "
        "(#191/#192)."
    )


def test_executor_guard_follows_the_task_id_guard() -> None:
    """Structural: the EXECUTOR guard sits with the other critical-var guards
    near the top (after the scheduler task-id guard), not buried mid-script."""
    body = (TEMPLATES_DIR / "runtime/sge/cpu_array.sh").read_text(encoding="utf-8")
    task_id_at = body.index("SGE_TASK_ID:?")
    executor_at = body.index("EXECUTOR:?")
    assert task_id_at < executor_at < task_id_at + 400
