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

from tests._paths import rendered_templates_dir


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
    # ``pass_env_keys`` is declared on the SGE subclass, not the HPCBackend base.
    assert tuple(forward_empty.pass_env_keys) == job_env_keys  # type: ignore[attr-defined]
    assert tuple(forward_all.pass_env_keys) == tuple(  # type: ignore[attr-defined]
        forward_empty.pass_env_keys  # type: ignore[attr-defined]
    )


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
    assert tuple(backend.pass_env_keys) == ("EXECUTOR",)  # type: ignore[attr-defined]


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
    guard checks emptiness + the bare-name shape, NOT runnability (unlike the
    sidecar's per-task executor, which must NOT be the dispatcher)."""
    from hpc_agent.ops.submit_flow import _ensure_job_script_executor

    # Must not raise — this is the canonical, correct value.
    _ensure_job_script_executor("rX", {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"})


# ─── #191 shape extension (proving run #3): a bare NAME is not a command ────


@pytest.mark.parametrize("bad_executor", ["run", "train", "x", "  run  "])
def test_ensure_job_script_executor_refuses_bare_name(bad_executor: str) -> None:
    """Proving run #3 layer (a): an agent-hand-authored spec carried the
    executor NAME 'run' (the interview run_name) instead of a command; the
    empty-only guard passed it and the job script ran `time run` → exit 127,
    discovered only by a cluster round-trip. A single token with no whitespace
    and no path separator cannot be the job-script command — refuse at the
    desk, pointing at build-submit-spec (which defaults the dispatcher)."""
    from hpc_agent import errors
    from hpc_agent.ops.submit_flow import _ensure_job_script_executor

    with pytest.raises(errors.SpecInvalid, match="build-submit-spec"):
        _ensure_job_script_executor("rX", {"EXECUTOR": bad_executor})


@pytest.mark.parametrize(
    "good_executor",
    [
        # The canonical default the build layer stamps.
        "python3 .hpc/_hpc_dispatch.py",
        # A command with flags (whitespace ⇒ not a bare name).
        "python3 .hpc/_hpc_dispatch.py --verbose",
        # A single token that carries a path separator is a runnable path.
        "./run.sh",
        ".hpc/dispatch_wrapper.sh",
    ],
)
def test_ensure_job_script_executor_accepts_real_commands(good_executor: str) -> None:
    """Real commands — the dispatcher default, flagged variants, and pathed
    single tokens — all pass the shape check."""
    from hpc_agent.ops.submit_flow import _ensure_job_script_executor

    _ensure_job_script_executor("rX", {"EXECUTOR": good_executor})


def test_ensure_job_script_executor_names_the_interview_run_name(tmp_path) -> None:
    """When interview.json resolves a registered run_name and the EXECUTOR
    equals it, the refusal names the run_name specifically — the exact garbage
    the proving-run-3 spec shipped."""
    import json

    from hpc_agent import errors
    from hpc_agent.ops.submit_flow import _ensure_job_script_executor

    (tmp_path / "interview.json").write_text(
        json.dumps(
            {
                "goal": "g",
                "_materialized": {"entry_point": {"kind": "register_run", "run_name": "run"}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(errors.SpecInvalid, match="run_name"):
        _ensure_job_script_executor("rX", {"EXECUTOR": "run"}, experiment_dir=tmp_path)


def test_ensure_job_script_executor_shape_check_needs_no_interview(tmp_path) -> None:
    """The bare-name refusal fires WITHOUT an interview.json (fail-closed on
    shape alone); the run_name arm is only the more specific message."""
    from hpc_agent import errors
    from hpc_agent.ops.submit_flow import _ensure_job_script_executor

    with pytest.raises(errors.SpecInvalid, match="bare name"):
        _ensure_job_script_executor("rX", {"EXECUTOR": "train"}, experiment_dir=tmp_path)


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
    body = (rendered_templates_dir() / template).read_text(encoding="utf-8")
    assert '"${EXECUTOR:?' in body, (
        f'{template} is missing the `: "${{EXECUTOR:?...}}"` guard — without it '
        "an unset EXECUTOR runs `time` with no command and 'succeeds' silently "
        "(#191/#192)."
    )


def test_executor_guard_follows_the_task_id_guard() -> None:
    """Structural: the EXECUTOR guard sits with the other critical-var guards
    near the top (after the scheduler task-id guard), not buried mid-script."""
    body = (rendered_templates_dir() / "runtime/sge/cpu_array.sh").read_text(encoding="utf-8")
    task_id_at = body.index("SGE_TASK_ID:?")
    executor_at = body.index("EXECUTOR:?")
    assert task_id_at < executor_at < task_id_at + 400


# ─── proving-run-5 finding 17: a bare script name is not a runnable executor ──


@pytest.mark.parametrize("bare", ["train.py", "run.sh", "analyze.R", "sim.jl", "  train.py  "])
def test_is_runnable_executor_refuses_bare_script_name(bare: str) -> None:
    """The SIDECAR per-task executor predicate must reject a bare script token
    (no interpreter, no path separator): run verbatim by the dispatcher it exits
    127. Before finding 17 this returned True (non-empty AND not-dispatcher), so
    a hand-onboarded `train.py` sidecar sailed to the cluster and died."""
    from hpc_agent.ops.submit_flow import _is_runnable_executor

    assert _is_runnable_executor(bare) is False


@pytest.mark.parametrize(
    "runnable",
    [
        "python train.py --seed $SEED",  # interpreter prefix
        "python3 analyze.py",  # interpreter prefix, no args
        "./run.sh",  # executable path
        "scripts/train.py",  # pathed (relative)
        "/abs/train.py",  # pathed (absolute)
        "mybinary",  # a bare NON-script token is not this check's concern
    ],
)
def test_is_runnable_executor_accepts_real_commands(runnable: str) -> None:
    from hpc_agent.ops.submit_flow import _is_runnable_executor

    assert _is_runnable_executor(runnable) is True


def test_is_bare_script_name_predicate_unit() -> None:
    from hpc_agent.ops.submit_flow import _is_bare_script_name

    assert _is_bare_script_name("train.py") is True
    assert _is_bare_script_name("run.sh") is True
    assert _is_bare_script_name("analyze.R") is True  # case-insensitive extension
    assert _is_bare_script_name("sim.jl") is True
    # Not bare-script: interpreter prefix, path separators, non-script tokens.
    assert _is_bare_script_name("python train.py") is False
    assert _is_bare_script_name("./train.py") is False
    assert _is_bare_script_name("a/b/train.py") is False
    assert _is_bare_script_name("mybinary") is False
    assert _is_bare_script_name("") is False
    assert _is_bare_script_name(None) is False


def test_ensure_run_sidecar_refuses_prewritten_bare_script_executor(tmp_path) -> None:
    """The submit-time reach: a per-run sidecar pre-written (Step 6d) with a bare
    `train.py` executor is refused in _ensure_run_sidecar BEFORE any rsync/qsub —
    the exit-127 the finding-17 audit hit only after a full cluster round-trip."""
    import json

    from hpc_agent import errors
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
    from hpc_agent.ops.submit_flow import _ensure_run_sidecar

    runs = tmp_path / ".hpc" / "runs"
    runs.mkdir(parents=True)
    (runs / "r1.json").write_text(
        json.dumps(
            {"executor": "train.py", "result_dir_template": "results/{run_id}/task_{task_id}"}
        ),
        encoding="utf-8",
    )
    spec = SubmitFlowSpec(
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/r",
        job_name="j",
        run_id="r1",
        total_tasks=4,
        backend="sge",
        script="run.sh",
        job_env={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
        result_dir_template="results/{run_id}/task_{task_id}",
    )
    with pytest.raises(errors.SpecInvalid, match="bare script name"):
        _ensure_run_sidecar(tmp_path, spec)
