"""Tests for ``hpc_agent.incorporation.build.submit_spec`` — slash-command Step 6d
collapses to one primitive call."""

from __future__ import annotations

from pathlib import Path

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
        # A COHERENT env-activation (#281): conda_env paired with the
        # conda_source the preamble sources before `conda activate`. conda_env
        # without a source is the incoherent partial state Activation now
        # refuses, so the minimal-valid fixture carries both. The
        # build-dir conftest isolates clusters.yaml to an empty config, so
        # there is nothing to back-fill from — the fixture must be coherent
        # on its own.
        conda_env="ml-py311",
        conda_source="/u/local/apps/anaconda3/2024.06/etc/profile.d/conda.sh",
    )


class TestResultDirTemplateIsolation:
    """Per-task isolation guard on ``result_dir_template`` at the
    build-submit-spec boundary (mirror of the same guard on
    WriteRunSidecarInput; fires one step earlier in the pipeline)."""

    def test_constant_only_template_refused_for_multi_task(self) -> None:
        with pytest.raises(ValueError, match="no per-task placeholder"):
            BuildSubmitSpecInput.model_validate(
                _required() | {"result_dir_template": "results/{run_id}"}
            )

    def test_literal_template_refused_for_multi_task(self) -> None:
        with pytest.raises(ValueError, match="no per-task placeholder"):
            BuildSubmitSpecInput.model_validate(_required() | {"result_dir_template": "results"})

    def test_constant_template_allowed_when_total_tasks_is_one(self) -> None:
        spec = BuildSubmitSpecInput.model_validate(
            _required() | {"total_tasks": 1, "result_dir_template": "results/{run_id}"}
        )
        assert spec.result_dir_template == "results/{run_id}"

    def test_task_id_placeholder_accepted(self) -> None:
        spec = BuildSubmitSpecInput.model_validate(
            _required() | {"result_dir_template": "results/{run_id}/task_{task_id}"}
        )
        assert "{task_id}" in (spec.result_dir_template or "")

    def test_swept_kwarg_placeholder_accepted(self) -> None:
        spec = BuildSubmitSpecInput.model_validate(
            _required() | {"result_dir_template": "results/seed_{seed}"}
        )
        assert "{seed}" in (spec.result_dir_template or "")

    def test_none_template_passes_through(self) -> None:
        # build_submit_spec fills in a framework default when None — the
        # validator must not refuse here.
        spec = BuildSubmitSpecInput.model_validate(_required() | {"result_dir_template": None})
        assert spec.result_dir_template is None


def test_rejects_relative_remote_path() -> None:
    """A relative remote_path becomes REPO_DIR in the qsub env and the
    preamble's `cd "$REPO_DIR"` then runs from an unpredictable SSH login
    dir. Reject at the boundary so a half-resolved cluster config can't
    fire a broken canary and poison submit dedup."""
    with pytest.raises(errors.SpecInvalid) as excinfo:
        build_submit_spec(
            spec=BuildSubmitSpecInput(**{**_required(), "remote_path": "monte_carlo_pi-bc3eb1b5"})
        )
    msg = str(excinfo.value)
    assert "absolute" in msg
    assert "monte_carlo_pi-bc3eb1b5" in msg


def test_rejects_all_empty_env_activation() -> None:
    """If modules / conda_source / conda_env are all empty, the cluster-side
    preamble skips every env-setup step and runs whatever python the SSH
    login inherits — frequently fatal. Reject at the boundary."""
    intent = _required()
    intent.pop("conda_env")
    intent.pop("conda_source", None)  # leave modules/conda_source/conda_env all empty
    with pytest.raises(errors.SpecInvalid) as excinfo:
        build_submit_spec(spec=BuildSubmitSpecInput(**intent))
    msg = str(excinfo.value)
    assert "env-activation" in msg
    assert "modules" in msg and "conda_source" in msg and "conda_env" in msg


def test_accepts_modules_alone_as_env_activation() -> None:
    """`modules` alone is a valid env-activation (pure module-based clusters)."""
    intent = _required()
    intent.pop("conda_env")
    intent.pop("conda_source", None)
    intent["modules"] = "anaconda3/2024.06"
    spec = build_submit_spec(spec=BuildSubmitSpecInput(**intent))
    assert spec["job_env"]["MODULES"] == "anaconda3/2024.06"


def test_conda_env_with_source_is_coherent() -> None:
    """conda_env + conda_source is the coherent activation the preamble needs (#281)."""
    spec = build_submit_spec(spec=BuildSubmitSpecInput(**_required()))
    assert spec["job_env"]["CONDA_ENV"] == "ml-py311"
    assert spec["job_env"]["CONDA_SOURCE"].endswith("conda.sh")


def test_conda_env_without_source_rejected_when_no_backfill() -> None:
    """#281: conda_env set, conda_source empty, and (under the conftest's empty
    isolated clusters.yaml) nothing to back-fill from → refused at the build
    boundary instead of crashing every task at `conda: command not found`."""
    intent = _required()
    intent.pop("conda_source")  # conda_env stays; no source, no modules
    with pytest.raises(errors.SpecInvalid) as excinfo:
        build_submit_spec(spec=BuildSubmitSpecInput(**intent))
    msg = str(excinfo.value)
    assert "conda_env" in msg and "conda: command not found" in msg


def test_conda_source_backfilled_from_clusters_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#281 code-forward fix: when conda_env is set but the agent dropped
    conda_source, build-submit-spec resolves activation as ONE unit and
    back-fills conda_source from clusters.yaml — so the incoherent state can't
    reach qsub (the 2026-06-05 Hoffman2 incident: clusters.yaml had the source,
    the agent lost it between `clusters describe` and spec construction)."""
    cfg = tmp_path / "clusters.yaml"
    cfg.write_text(
        "hoffman2:\n"
        "  scheduler: sge\n"
        "  host: h2.idre.ucla.edu\n"
        "  conda_source: /u/local/apps/anaconda3/2024.06/etc/profile.d/conda.sh\n"
        "  conda_envs: [ml-py311]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
    intent = _required()
    intent.pop("conda_source")  # agent dropped it; the cluster config carries it
    spec = build_submit_spec(spec=BuildSubmitSpecInput(**intent))
    assert (
        spec["job_env"]["CONDA_SOURCE"] == "/u/local/apps/anaconda3/2024.06/etc/profile.d/conda.sh"
    )
    assert spec["job_env"]["CONDA_ENV"] == "ml-py311"


def test_activation_value_object_makes_illegal_states_unrepresentable() -> None:
    """The Activation value object (#281) enforces the coherence invariant at
    construction — you cannot build one the cluster preamble would crash on."""
    from hpc_agent.infra.clusters import Activation, resolve_activation

    with pytest.raises(errors.SpecInvalid):
        Activation()  # all empty
    with pytest.raises(errors.SpecInvalid):
        Activation(conda_env="ml-py311")  # env with no source/modules
    assert Activation(modules="anaconda3").as_job_env()["MODULES"] == "anaconda3"
    # resolve_activation back-fills conda_source from the cluster block...
    assert (
        resolve_activation(cluster_cfg={"conda_source": "/x/conda.sh"}, conda_env="e").conda_source
        == "/x/conda.sh"
    )
    # ...but a caller-supplied source wins over the cluster default.
    assert (
        resolve_activation(
            cluster_cfg={"conda_source": "/cluster/conda.sh"},
            conda_source="/caller/conda.sh",
            conda_env="e",
        ).conda_source
        == "/caller/conda.sh"
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
    # #275: build-submit-spec no longer emits skip_preflight — it was an
    # agent-settable bypass that silenced the uv runtime probe. The preflight
    # skip is operator-only now (HPC_AGENT_SKIP_PREFLIGHT), never a built field.
    assert "skip_preflight" not in spec


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
            **{
                **_required(),
                "modules": "cuda/12.3 anaconda3/2024.02",
                "conda_source": "/u/local/apps/conda/etc/profile.d/conda.sh",
                "conda_env": "ml-py311",
            }
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


def test_rejects_bare_script_executor_for_register_run_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ``python3 <file>.py`` EXECUTOR against a @register_run-decorated
    file is the empirical 0.10.2-demo failure: argparse-__main__ fires, exits
    2 silently, no metrics.json. Catch at the submission boundary."""
    monkeypatch.chdir(tmp_path)
    exec_dir = tmp_path / "executors"
    exec_dir.mkdir()
    (exec_dir / "monte_carlo_pi.py").write_text(
        "from hpc_agent import register_run\n"
        "\n"
        "@register_run\n"
        "def monte_carlo_pi(n_samples: int = 1000) -> dict:\n"
        "    return {'pi': 3.14}\n",
        encoding="utf-8",
    )
    with pytest.raises(errors.SpecInvalid) as excinfo:
        build_submit_spec(
            spec=BuildSubmitSpecInput(
                **_required(),
                extra_env={"EXECUTOR": "python3 executors/monte_carlo_pi.py"},
            )
        )
    msg = str(excinfo.value)
    assert "register_run" in msg
    assert "HPC_KW_" in msg
    assert "python3 -c" in msg


def test_rejects_bare_script_executor_with_trailing_args_for_register_run_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empirical 2026-06-05 demo failure: an agent built the spec by hand and
    emitted ``python executors/monte_carlo_pi.py --samples 100000 --seed $SEED``
    against a @register_run-decorated file. The pre-0.10.11 guard required
    ``len(parts) == 2`` and let the with-args form slip through; the dispatcher
    runs it literally, argparse exits 2 on the missing ``--output-file``, the
    canary fails, the user is stuck. Trailing args are not a safe path — they
    are the *exact* signal that the agent forgot the ``-c`` one-liner.
    """
    monkeypatch.chdir(tmp_path)
    exec_dir = tmp_path / "executors"
    exec_dir.mkdir()
    (exec_dir / "monte_carlo_pi.py").write_text(
        "from hpc_agent import register_run\n"
        "\n"
        "@register_run\n"
        "def monte_carlo_pi(seed: int = 0, samples: int = 1000) -> dict:\n"
        "    return {'pi': 3.14}\n",
        encoding="utf-8",
    )
    with pytest.raises(errors.SpecInvalid) as excinfo:
        build_submit_spec(
            spec=BuildSubmitSpecInput(
                **_required(),
                extra_env={
                    "EXECUTOR": "python executors/monte_carlo_pi.py --samples 100000 --seed $SEED"
                },
            )
        )
    msg = str(excinfo.value)
    assert "register_run" in msg
    assert "HPC_KW_" in msg


def test_accepts_one_liner_executor_for_register_run_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``python3 -c "..."`` form is the correct invocation — the guard
    must NOT fire even when the targeted file is @register_run-decorated."""
    monkeypatch.chdir(tmp_path)
    exec_dir = tmp_path / "executors"
    exec_dir.mkdir()
    (exec_dir / "monte_carlo_pi.py").write_text(
        "from hpc_agent import register_run\n"
        "\n"
        "@register_run\n"
        "def monte_carlo_pi(n_samples: int = 1000) -> dict:\n"
        "    return {'pi': 3.14}\n",
        encoding="utf-8",
    )
    one_liner = (
        'python3 -c "import runpy as _r; '
        "_m = _r.run_path('executors/monte_carlo_pi.py'); "
        '_n = next(v for v in _m.values() if getattr(v, \\"_hpc_run\\", False)); '
        '_m.compute(_n)"'
    )
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(**_required(), extra_env={"EXECUTOR": one_liner})
    )
    assert spec["job_env"]["EXECUTOR"] == one_liner


def test_accepts_bare_script_executor_for_non_register_run_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ``python3 <file>.py`` against a normal script (not @register_run)
    is a legitimate use — the user is intentionally running a plain script.
    The guard must not fire."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plain.py").write_text(
        "import sys\n"
        "\n"
        "def main() -> None:\n"
        "    print('hello')\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(**_required(), extra_env={"EXECUTOR": "python3 plain.py"})
    )
    assert spec["job_env"]["EXECUTOR"] == "python3 plain.py"


def test_register_run_guard_resolves_script_against_experiment_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#292 Bug A: the bare-script-vs-register_run guard must fire even when the
    process CWD is NOT the experiment dir — the empirical worker case where the
    pre-#292 ``Path(script).is_file()`` was CWD-relative and silently no-op'd.
    Passing ``experiment_dir`` resolves the script against the real tree."""
    exp = tmp_path / "exp"
    (exp / "executors").mkdir(parents=True)
    (exp / "executors" / "monte_carlo_pi.py").write_text(
        "from hpc_agent import register_run\n"
        "\n"
        "@register_run\n"
        "def monte_carlo_pi(seed: int = 0) -> dict:\n"
        "    return {'pi': 3.14}\n",
        encoding="utf-8",
    )
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)  # CWD != experiment dir — the empirical case
    with pytest.raises(errors.SpecInvalid) as excinfo:
        build_submit_spec(
            exp,
            spec=BuildSubmitSpecInput(
                **_required(),
                extra_env={"EXECUTOR": "python executors/monte_carlo_pi.py --seed $SEED"},
            ),
        )
    assert "register_run" in str(excinfo.value)


def test_register_run_guard_is_cwd_relative_without_experiment_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion to the above: with no ``experiment_dir`` AND a CWD that isn't
    the experiment dir, the guard can't see the script, so it can't fire — this
    is exactly the #292 hole, pinned so the experiment_dir thread-through stays
    the thing that closes it (and so the CWD-relative fallback is intentional,
    not accidental, for invocations run from inside the experiment dir)."""
    exp = tmp_path / "exp"
    (exp / "executors").mkdir(parents=True)
    (exp / "executors" / "monte_carlo_pi.py").write_text(
        "from hpc_agent import register_run\n\n"
        "@register_run\ndef f(seed: int = 0):\n    return {}\n",
        encoding="utf-8",
    )
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    # No experiment_dir → CWD-relative probe misses → guard silently passes.
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(
            **_required(),
            extra_env={"EXECUTOR": "python executors/monte_carlo_pi.py --seed 1"},
        )
    )
    assert spec["job_env"]["EXECUTOR"] == "python executors/monte_carlo_pi.py --seed 1"


def _write_tasks_py(exp: Path, resolve_body: str) -> None:
    (exp / ".hpc").mkdir(parents=True, exist_ok=True)
    (exp / ".hpc" / "tasks.py").write_text(
        f"def total():\n    return 3\n\n\ndef resolve(i):\n    return {resolve_body}\n",
        encoding="utf-8",
    )


def test_rejects_executor_referencing_unexported_var(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#292 Bug B: a hand-built EXECUTOR referencing ``$SAMPLES`` when ``samples``
    is not a swept axis (tasks.resolve() returns only ``seed``) is the empirical
    silent failure — ``$SAMPLES`` expands to empty cluster-side and argparse
    dies. Refuse at build time, naming the var and the two resolutions."""
    exp = tmp_path / "exp"
    exp.mkdir()
    (exp / "analyze.py").write_text("print('plain, not register_run')\n", encoding="utf-8")
    _write_tasks_py(exp, "{'seed': i}")
    monkeypatch.chdir(tmp_path)  # CWD != exp — guard still resolves via experiment_dir
    with pytest.raises(errors.SpecInvalid) as excinfo:
        build_submit_spec(
            exp,
            spec=BuildSubmitSpecInput(
                **_required(),
                extra_env={
                    "EXECUTOR": (
                        "python3 analyze.py --samples $SAMPLES --seed $SEED "
                        "--output-file $RESULT_DIR/metrics.json"
                    )
                },
            ),
        )
    msg = str(excinfo.value)
    assert "SAMPLES" in msg and "samples" in msg
    assert "homogeneous_axes" in msg  # the remediation


def test_accepts_executor_with_only_covered_vars(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#292 Bug B must NOT false-positive: a swept-axis kwarg ($SEED, $SAMPLES),
    a framework var ($RESULT_DIR), an inherited cluster var ($SCRATCH) and a
    ``:-``-defaulted ref (${OUTDIR:-/tmp}) are all covered."""
    exp = tmp_path / "exp"
    exp.mkdir()
    (exp / "analyze.py").write_text("print('plain')\n", encoding="utf-8")
    _write_tasks_py(exp, "{'seed': i, 'samples': 100}")
    monkeypatch.chdir(tmp_path)
    spec = build_submit_spec(
        exp,
        spec=BuildSubmitSpecInput(
            **_required(),
            extra_env={
                "EXECUTOR": (
                    "python3 analyze.py --samples $SAMPLES --seed $SEED "
                    "--data $SCRATCH/in --out ${OUTDIR:-/tmp} "
                    "--output-file $RESULT_DIR/metrics.json"
                )
            },
        ),
    )
    assert "analyze.py" in spec["job_env"]["EXECUTOR"]


def test_var_reference_check_noops_when_kwargs_unknowable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no experiment_dir the kwarg set can't be established, so the check
    must degrade to a no-op rather than false-refuse a possibly-fine EXECUTOR."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "analyze.py").write_text("print('plain')\n", encoding="utf-8")
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(
            **_required(),
            extra_env={"EXECUTOR": "python3 analyze.py --samples $SAMPLES"},
        )
    )
    assert "$SAMPLES" in spec["job_env"]["EXECUTOR"]


def test_default_executor_does_not_import_tasks_py(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The $VAR cross-check must NOT import .hpc/tasks.py when the EXECUTOR has no
    $VAR refs (the default dispatcher command) — otherwise resolve-submit-inputs
    would re-import user tasks.py a second time on every submit."""
    import hpc_agent

    calls: list = []
    real = hpc_agent.load_tasks_module
    monkeypatch.setattr(hpc_agent, "load_tasks_module", lambda p: (calls.append(p), real(p))[1])
    exp = tmp_path / "exp"
    exp.mkdir()
    _write_tasks_py(exp, "{'seed': i}")
    # Default executor (no extra_env) → job_env["EXECUTOR"] has no '$'.
    build_submit_spec(exp, spec=BuildSubmitSpecInput(**_required()))
    assert calls == [], "default executor must not trigger a tasks.py import"


def test_check_executor_var_references_unit() -> None:
    """Direct coverage of the #292 Bug B predicate."""
    from hpc_agent.incorporation.build.submit_spec import _check_executor_var_references

    job_env_keys = {"EXECUTOR", "HPC_RUN_ID", "REPO_DIR", "HPC_CAMPAIGN_ID"}
    # Covered: seed kwarg, RESULT_DIR framework, SCRATCH shell, SLURM_ prefix,
    # HPC_CAMPAIGN_ID job_env key, ${Q:-1} defaulted, $HPC_KW_SEED namespaced.
    _check_executor_var_references(
        "p --seed $SEED --kw $HPC_KW_SEED --o $RESULT_DIR --d $SCRATCH/x "
        "--n $SLURM_JOB_ID --c $HPC_CAMPAIGN_ID --q ${Q:-1}",
        job_env_keys=job_env_keys,
        kwargs_keys={"seed"},
    )
    # Uncovered bare var → refuse.
    with pytest.raises(errors.SpecInvalid):
        _check_executor_var_references(
            "p --samples $SAMPLES", job_env_keys=job_env_keys, kwargs_keys={"seed"}
        )
    # Uncovered HPC_KW_ namespaced var → refuse.
    with pytest.raises(errors.SpecInvalid):
        _check_executor_var_references(
            "p --samples $HPC_KW_SAMPLES", job_env_keys=job_env_keys, kwargs_keys={"seed"}
        )
    # Unknowable kwarg set → never refuse.
    _check_executor_var_references(
        "p --samples $SAMPLES", job_env_keys=job_env_keys, kwargs_keys=None
    )


def test_walltime_sec_stamped_into_job_env_for_checkpoint_deadline() -> None:
    """#294: a submit with a walltime stamps HPC_WALLTIME_SEC so the cluster
    preamble can derive HPC_WALLTIME_END_EPOCH for walltime-margin checkpointing.
    Absent a walltime, the key is omitted (no deadline → checkpoint no-op)."""
    spec = build_submit_spec(spec=BuildSubmitSpecInput(**_required(), walltime_sec=7200))
    assert spec["job_env"]["HPC_WALLTIME_SEC"] == "7200"
    assert (
        "HPC_WALLTIME_SEC"
        not in build_submit_spec(spec=BuildSubmitSpecInput(**_required()))["job_env"]
    )


def test_assembled_spec_passes_submit_flow_input_schema() -> None:
    """Belt-and-suspenders: the schema validator inside the primitive must
    accept its own output. A regression here means the framework-default
    job_env dict drifted from the schema."""
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(
            **{
                **_required(),
                "is_gpu": True,
                "modules": "cuda/12.3",
                "conda_source": "/path/conda.sh",
                "conda_env": "ml",
                "runtime": "uv",
                "campaign_id": "c1",
                "canary": False,
                "partial_ok": True,
            }
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
