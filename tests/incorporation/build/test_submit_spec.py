"""Tests for ``hpc_agent.incorporation.build.submit_spec`` — slash-command Step 6d
collapses to one primitive call."""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
from hpc_agent.incorporation.build.submit_spec import build_submit_spec


def _required() -> dict:
    return dict(
        profile="ml_ridge",
        cluster="hoffman2",
        ssh_target="alice@h2.idre.ucla.edu",
        remote_path="/u/scratch/alice/exp",
        run_id="20260101-000000-deadbee",
        cmd_sha="a" * 64,
        total_tasks=42,
        backend="sge",
    )


def test_returns_minimal_valid_spec_with_synthesized_job_env() -> None:
    spec = build_submit_spec(spec=BuildSubmitSpecInput(**_required()))
    assert spec["profile"] == "ml_ridge"
    assert spec["job_name"] == "ml_ridge"  # defaults to profile
    assert spec["script"] == ".hpc/templates/cpu_array.sh"
    assert spec["job_env"]["EXECUTOR"] == "python3 .hpc/_hpc_dispatch.py"
    assert spec["job_env"]["HPC_RUN_ID"] == "20260101-000000-deadbee"
    assert spec["job_env"]["HPC_CMD_SHA"] == "a" * 64
    assert spec["job_env"]["HPC_TASK_COUNT"] == "42"
    assert spec["job_env"]["REPO_DIR"] == "/u/scratch/alice/exp"
    assert spec["canary"] is True
    assert spec["skip_preflight"] is True


def test_service_env_stamps_hpc_service_env_json() -> None:
    """Seam closure (#231): a spec service_env ships as the JSON HPC_SERVICE_ENV
    job_env var the cluster-side dispatcher reads to inject HPC_SERVICE_* vars."""
    import json

    from hpc_agent.ops.recover.service import inject_service_env

    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(**_required(), service_env={"addr": "http://node7:8000"})
    )
    raw = spec["job_env"]["HPC_SERVICE_ENV"]
    assert json.loads(raw) == {"addr": "http://node7:8000"}
    # ...and the dispatcher side turns it into a namespaced task-env var.
    task_env = inject_service_env({}, json.loads(raw))
    assert task_env["HPC_SERVICE_ADDR"] == "http://node7:8000"


def test_no_service_env_omits_the_var() -> None:
    spec = build_submit_spec(spec=BuildSubmitSpecInput(**_required()))
    assert "HPC_SERVICE_ENV" not in spec["job_env"]


def test_gpu_picks_gpu_template() -> None:
    spec = build_submit_spec(spec=BuildSubmitSpecInput(**_required(), is_gpu=True))
    assert spec["script"] == ".hpc/templates/gpu_array.sh"


def test_slurm_backend_picks_slurm_template() -> None:
    spec = build_submit_spec(spec=BuildSubmitSpecInput(**{**_required(), "backend": "slurm"}))
    assert spec["script"] == ".hpc/templates/cpu_array.slurm"
    spec_gpu = build_submit_spec(
        spec=BuildSubmitSpecInput(**{**_required(), "backend": "slurm"}, is_gpu=True)
    )
    assert spec_gpu["script"] == ".hpc/templates/gpu_array.slurm"


def test_uv_runtime_sets_hpc_runtime_env() -> None:
    spec = build_submit_spec(spec=BuildSubmitSpecInput(**_required(), runtime="uv"))
    assert spec["job_env"]["HPC_RUNTIME"] == "uv"
    assert spec["runtime"] == "uv"


def test_campaign_id_threaded_to_env_and_top_level() -> None:
    spec = build_submit_spec(spec=BuildSubmitSpecInput(**_required(), campaign_id="ml_ridge_q1"))
    assert spec["campaign_id"] == "ml_ridge_q1"
    assert spec["job_env"]["HPC_CAMPAIGN_ID"] == "ml_ridge_q1"


def test_extra_env_wins_over_framework_default_on_collision() -> None:
    """Caller-supplied extra_env keys override the synthesized defaults."""
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(
            **_required(),
            extra_env={"EXECUTOR": "python3 -m my.custom_dispatch", "EXTRA_FLAG": "1"},
        )
    )
    assert spec["job_env"]["EXECUTOR"] == "python3 -m my.custom_dispatch"
    assert spec["job_env"]["EXTRA_FLAG"] == "1"


def test_modules_and_conda_threaded_through() -> None:
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(
            **_required(),
            modules="cuda/12.3 anaconda3/2024.02",
            conda_source="/u/local/apps/conda/etc/profile.d/conda.sh",
            conda_env="ml-py311",
        )
    )
    assert spec["job_env"]["MODULES"] == "cuda/12.3 anaconda3/2024.02"
    assert spec["job_env"]["CONDA_SOURCE"] == "/u/local/apps/conda/etc/profile.d/conda.sh"
    assert spec["job_env"]["CONDA_ENV"] == "ml-py311"


def test_optional_passthroughs() -> None:
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(
            **_required(),
            pass_env_keys=["EXECUTOR", "HPC_RUN_ID"],
            rsync_excludes=["data/", "*.pkl"],
            slurm_account="my_account",
            slurm_cluster="hoffman2",
        )
    )
    assert spec["pass_env_keys"] == ["EXECUTOR", "HPC_RUN_ID"]
    assert spec["rsync_excludes"] == ["data/", "*.pkl"]
    assert spec["slurm_account"] == "my_account"
    assert spec["slurm_cluster"] == "hoffman2"


def test_resources_and_result_dir_template_threaded() -> None:
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(
            **_required(),
            result_dir_template="results/{run_id}/task_{task_id}",
            walltime_sec=7200,
            mem_mb=8192,
            cpus=4,
        )
    )
    assert spec["result_dir_template"] == "results/{run_id}/task_{task_id}"
    assert spec["resources"] == {"walltime_sec": 7200, "mem_mb": 8192, "cpus": 4}


def test_partial_resources_only_emit_set_fields() -> None:
    spec = build_submit_spec(spec=BuildSubmitSpecInput(**_required(), walltime_sec=3600))
    assert spec["resources"] == {"walltime_sec": 3600}


def test_omitted_optional_fields_not_in_output() -> None:
    spec = build_submit_spec(spec=BuildSubmitSpecInput(**_required()))
    omitted = (
        "pass_env_keys",
        "rsync_excludes",
        "slurm_account",
        "slurm_cluster",
        "campaign_id",
        "runtime",
        "resources",
        "result_dir_template",
    )
    for k in omitted:
        assert k not in spec, f"{k!r} should be omitted when not supplied"


def test_invalid_ssh_target_raises_spec_invalid() -> None:
    # ``BuildSubmitSpecInput.ssh_target`` is now typed ``SshTarget``
    # (pattern-validated). A shell-injection-shaped value fails at the
    # Pydantic boundary BEFORE reaching the atom, which is the stronger
    # contract; the atom-side ``validate_ssh_target`` raise remains for
    # callers that construct the spec dict directly.
    from pydantic import ValidationError

    with pytest.raises((errors.SpecInvalid, ValidationError)):
        build_submit_spec(
            spec=BuildSubmitSpecInput(**{**_required(), "ssh_target": "alice; rm -rf /"})
        )


def test_assembled_spec_passes_submit_flow_input_schema() -> None:
    """Belt-and-suspenders: the schema validator inside the primitive must
    accept its own output. A regression here means the framework-default
    job_env dict drifted from the schema."""
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(
            **_required(),
            is_gpu=True,
            modules="cuda/12.3",
            conda_source="/path/conda.sh",
            conda_env="ml",
            runtime="uv",
            campaign_id="c1",
            canary=False,
            partial_ok=True,
        )
    )
    for k in (
        "profile",
        "cluster",
        "ssh_target",
        "remote_path",
        "run_id",
        "total_tasks",
        "backend",
        "job_name",
        "script",
        "job_env",
    ):
        assert k in spec
