"""Tests for the ``write-run-sidecar`` primitive.

Covers the agent-facing CLI wrapper around
:func:`hpc_agent.state.runs.write_run_sidecar`: happy path, the #162
dispatcher-executor refusal that prevents self-recursive sidecars at
the new CLI surface, idempotent overwrite, and v2 config-snapshot
round-trip.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
from hpc_agent.ops.write_run_sidecar import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


def _spec(**overrides: object) -> WriteRunSidecarInput:
    """Build a minimal valid ``WriteRunSidecarInput`` with overrides."""
    base: dict[str, object] = {
        "run_id": "20260101-000000-deadbee",
        "cmd_sha": "0" * 64,
        "executor": "python3 src/run.py --seed $SEED",
        "result_dir_template": "results/{seed}",
        "task_count": 4,
        "tasks_py_sha": "1" * 64,
    }
    base.update(overrides)
    return WriteRunSidecarInput.model_validate(base)


def test_happy_path_writes_sidecar_and_returns_path(tmp_path: Path) -> None:
    spec = _spec()
    out = write_run_sidecar(experiment_dir=tmp_path, spec=spec)

    target = tmp_path / ".hpc" / "runs" / f"{spec.run_id}.json"
    assert out == {"path": str(target)}
    assert target.is_file()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["run_id"] == spec.run_id
    assert data["executor"] == spec.executor
    assert data["task_count"] == 4
    # Auto-stamped fields are present and non-empty.
    assert data["submitted_at"]
    assert data["hpc_agent_version"]


class TestResultDirTemplateIsolation:
    """Per-task isolation guard on ``result_dir_template`` (multi-task runs).

    Empirical 2026-06-06 demo: orchestrator hand-built a sidecar with
    ``result_dir_template = "results/{run_id}"`` and ``task_count = 100``.
    Every task wrote to the same dir; 99 outputs got clobbered. The
    validator refuses at sidecar-write time so the bad config never lands.
    """

    def test_constant_only_template_refused_for_multi_task(self) -> None:
        with pytest.raises(ValueError, match="no per-task placeholder"):
            _spec(result_dir_template="results/{run_id}", task_count=100)

    def test_literal_template_refused_for_multi_task(self) -> None:
        with pytest.raises(ValueError, match="no per-task placeholder"):
            _spec(result_dir_template="results", task_count=2)

    def test_constant_template_allowed_for_single_task(self) -> None:
        # task_count=1 cannot clobber itself; the guard is a no-op.
        spec = _spec(result_dir_template="results/{run_id}", task_count=1)
        assert spec.result_dir_template == "results/{run_id}"

    def test_task_id_placeholder_accepted(self) -> None:
        spec = _spec(result_dir_template="results/{run_id}/task_{task_id}", task_count=100)
        assert "{task_id}" in spec.result_dir_template

    def test_swept_kwarg_placeholder_accepted(self) -> None:
        # ``{seed}`` is a kwarg from tasks.py FLAGS; if seed is swept, each
        # task renders to a unique dir. The validator doesn't know what's
        # swept (that's in tasks.py) but accepts ANY non-constant placeholder.
        spec = _spec(result_dir_template="results/{run_id}/seed_{seed}", task_count=100)
        assert "{seed}" in spec.result_dir_template

    def test_error_message_names_the_template_and_offers_two_fixes(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            _spec(result_dir_template="results/{run_id}", task_count=100)
        msg = str(exc_info.value)
        # Names the offending template and the task_count
        assert "results/{run_id}" in msg
        assert "task_count=100" in msg
        # Offers both the {task_id} and the swept-kwarg fix paths
        assert "{task_id}" in msg
        assert "swept kwarg" in msg


def test_dispatcher_executor_refused(tmp_path: Path) -> None:
    spec = _spec(executor="python3 .hpc/_hpc_dispatch.py")
    with pytest.raises(errors.SpecInvalid, match="dispatcher"):
        write_run_sidecar(experiment_dir=tmp_path, spec=spec)
    # Nothing should be written when the guard fires.
    target = tmp_path / ".hpc" / "runs" / f"{spec.run_id}.json"
    assert not target.exists()


def test_dispatcher_plain_dispatch_py_refused(tmp_path: Path) -> None:
    # The ``_is_runnable_executor`` predicate also matches bare ``dispatch.py``
    # (not just ``_hpc_dispatch.py``) — confirm the broader guard fires too.
    spec = _spec(executor="python3 dispatch.py")
    with pytest.raises(errors.SpecInvalid):
        write_run_sidecar(experiment_dir=tmp_path, spec=spec)


def test_bare_script_name_executor_refused(tmp_path: Path) -> None:
    """proving-run-5 finding 17: a bare `train.py` (no interpreter, no path) is
    refused BEFORE write, with the interpreter-prefix remedy — the sidecar the
    finding hit shipped `train.py` and every task died exit-127."""
    spec = _spec(executor="train.py")
    with pytest.raises(errors.SpecInvalid) as exc_info:
        write_run_sidecar(experiment_dir=tmp_path, spec=spec)
    msg = str(exc_info.value)
    assert "bare script name" in msg
    assert "python train.py" in msg  # the precise remedy, not the generic dispatcher refusal
    # Nothing written when the guard fires.
    assert not (tmp_path / ".hpc" / "runs" / f"{spec.run_id}.json").exists()


def _write_tasks_py(exp: Path, resolve_body: str = "{'seed': i}") -> None:
    (exp / ".hpc").mkdir(parents=True, exist_ok=True)
    (exp / ".hpc" / "tasks.py").write_text(
        f"def total():\n    return 3\n\n\ndef resolve(i):\n    return {resolve_body}\n",
        encoding="utf-8",
    )


def _true_identity(exp: Path, run_name: str = "demo") -> dict[str, object]:
    """The ground-truth identity for *exp*'s tasks.py, via compute-run-id —
    what a truthful caller threads into write-run-sidecar (run #6 F1)."""
    from hpc_agent.incorporation.build.compute_run_id import compute_run_id

    truth = compute_run_id(exp, run_name=run_name)
    return {
        "run_id": truth["run_id"],
        "cmd_sha": truth["cmd_sha"],
        "task_count": truth["total"],
    }


class TestPerTaskExecutorGuard:
    """The dispatcher reads ``sidecar.executor`` and runs it per task verbatim,
    so a broken command fails silently cluster-side. Both shapes below are from
    the live 2026-06-06 demo (canary `--seed $SEED` correct; resubmit regressed
    to `--seed $seed` + a `{run_id}/seed_{seed}` --output-file)."""

    def test_format_placeholders_in_executor_refused(self, tmp_path: Path) -> None:
        spec = _spec(
            executor=(
                "python executors/monte_carlo_pi.py --seed $SEED "
                "--output-file results/{run_id}/seed_{seed}/metrics.json"
            )
        )
        with pytest.raises(errors.SpecInvalid, match="placeholder"):
            write_run_sidecar(experiment_dir=tmp_path, spec=spec)
        # Nothing written when the guard fires.
        target = tmp_path / ".hpc" / "runs" / f"{spec.run_id}.json"
        assert not target.exists()

    def test_wrong_case_kwarg_ref_refused(self, tmp_path: Path) -> None:
        _write_tasks_py(tmp_path)  # proves `seed` is a swept kwarg
        spec = _spec(executor="python executors/monte_carlo_pi.py --seed $seed")
        with pytest.raises(errors.SpecInvalid, match="SEED"):
            write_run_sidecar(experiment_dir=tmp_path, spec=spec)

    def test_correct_per_task_executor_accepted(self, tmp_path: Path) -> None:
        # No false positive on the canonical command: uppercase $SEED + $RESULT_DIR.
        # With tasks.py present the identity cross-check runs too, so the spec
        # must be TRUTHFUL (run #6 F1: placeholder identity is now refused).
        _write_tasks_py(tmp_path)
        spec = _spec(
            executor=(
                "python executors/monte_carlo_pi.py --seed $SEED "
                '--output-file "$RESULT_DIR/metrics.json"'
            ),
            **_true_identity(tmp_path),
        )
        out = write_run_sidecar(experiment_dir=tmp_path, spec=spec)
        assert Path(out["path"]).is_file()


def test_idempotent_overwrite_produces_same_content(tmp_path: Path) -> None:
    spec = _spec()
    out1 = write_run_sidecar(experiment_dir=tmp_path, spec=spec)
    first = json.loads(Path(out1["path"]).read_text(encoding="utf-8"))

    # Drop the auto-stamped ``submitted_at`` — it's clock-dependent — and
    # confirm every other key matches byte-for-byte on a second write.
    out2 = write_run_sidecar(experiment_dir=tmp_path, spec=spec)
    second = json.loads(Path(out2["path"]).read_text(encoding="utf-8"))

    assert out1 == out2
    first.pop("submitted_at", None)
    second.pop("submitted_at", None)
    assert first == second


def test_v2_fields_round_trip_to_disk(tmp_path: Path) -> None:
    spec = _spec(
        cluster="hoffman2",
        profile="ml_ridge",
        resources={"cpus": 4, "mem": "16G", "walltime": "02:00:00"},
    )
    out = write_run_sidecar(experiment_dir=tmp_path, spec=spec)
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))

    assert data["cluster"] == "hoffman2"
    assert data["profile"] == "ml_ridge"
    assert data["resources"] == {"cpus": 4, "mem": "16G", "walltime": "02:00:00"}


def test_trial_tokens_persist_to_disk(tmp_path: Path) -> None:
    """trial_tokens threaded through the primitive (from compute-run-id) land
    on the sidecar verbatim, completing the CLI round-trip to prior_records."""
    spec = _spec(campaign_id="tune_q1", trial_tokens=[10, 11, 12])
    out = write_run_sidecar(experiment_dir=tmp_path, spec=spec)
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert data["trial_tokens"] == [10, 11, 12]


def test_trial_tokens_omitted_when_absent(tmp_path: Path) -> None:
    """Ordinary submit (no tokens) leaves the key off the on-disk JSON."""
    out = write_run_sidecar(experiment_dir=tmp_path, spec=_spec())
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert "trial_tokens" not in data


def test_trial_params_persist_to_disk(tmp_path: Path) -> None:
    """trial_params (the opaque cmd_sha pre-image from compute-run-id) land on
    the sidecar verbatim — provenance the framework records but never reads.

    Synthetic, meaningless keys/values: no optimizer is installed and core
    never interprets them."""
    params = [{"alpha": 0.1, "nonsense": [1, 2]}, {"alpha": 0.2, "nonsense": []}]
    spec = _spec(trial_params=params)
    out = write_run_sidecar(experiment_dir=tmp_path, spec=spec)
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert data["trial_params"] == params


def test_trial_params_omitted_when_absent(tmp_path: Path) -> None:
    """Ordinary submit (no params threaded) leaves the key off the JSON."""
    out = write_run_sidecar(experiment_dir=tmp_path, spec=_spec())
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert "trial_params" not in data


# Import Path lazily for the runtime branches above (TYPE_CHECKING gate).
from pathlib import Path  # noqa: E402


class TestIdentityCrossCheck:
    """Finding 21 at the CLI surface (run #6 F1 family): cmd_sha / task_count /
    run_id must agree with the materialized task list when .hpc/tasks.py
    exists. Refuse-on-provable-miss only: no tasks.py -> unchecked (the
    happy-path tests above); -canary run_ids exempt (the mirror owns them)."""

    def test_wrong_task_count_refused(self, tmp_path: Path) -> None:
        _write_tasks_py(tmp_path)  # total() == 3
        ident = _true_identity(tmp_path)
        ident["task_count"] = 4
        with pytest.raises(errors.SpecInvalid, match=r"tasks\.total"):
            write_run_sidecar(experiment_dir=tmp_path, spec=_spec(**ident))
        assert not (tmp_path / ".hpc" / "runs").exists()

    def test_wrong_cmd_sha_refused(self, tmp_path: Path) -> None:
        _write_tasks_py(tmp_path)
        ident = _true_identity(tmp_path)
        ident["cmd_sha"] = "0" * 64
        ident["run_id"] = "demo-" + "0" * 8  # consistent with the (wrong) sha
        with pytest.raises(errors.SpecInvalid, match="dedup"):
            write_run_sidecar(experiment_dir=tmp_path, spec=_spec(**ident))

    def test_conventional_run_id_with_wrong_suffix_refused(self, tmp_path: Path) -> None:
        _write_tasks_py(tmp_path)
        ident = _true_identity(tmp_path)
        ident["run_id"] = "demo-" + "a" * 8  # claims the convention, wrong hex
        with pytest.raises(errors.SpecInvalid, match="convention"):
            write_run_sidecar(experiment_dir=tmp_path, spec=_spec(**ident))

    def test_truthful_identity_accepted(self, tmp_path: Path) -> None:
        _write_tasks_py(tmp_path)
        out = write_run_sidecar(experiment_dir=tmp_path, spec=_spec(**_true_identity(tmp_path)))
        assert Path(out["path"]).is_file()

    def test_cmd_sha_prefix_form_accepted(self, tmp_path: Path) -> None:
        # The wire model admits the 8-char prefix; a TRUE prefix passes.
        _write_tasks_py(tmp_path)
        ident = _true_identity(tmp_path)
        ident["cmd_sha"] = str(ident["cmd_sha"])[:8]
        out = write_run_sidecar(experiment_dir=tmp_path, spec=_spec(**ident))
        assert Path(out["path"]).is_file()

    def test_nonconventional_run_id_makes_no_identity_claim(self, tmp_path: Path) -> None:
        # No trailing 8-hex token -> only cmd_sha/task_count are checked.
        _write_tasks_py(tmp_path)
        ident = _true_identity(tmp_path)
        ident["run_id"] = "freeform_name"
        out = write_run_sidecar(experiment_dir=tmp_path, spec=_spec(**ident))
        assert Path(out["path"]).is_file()

    def test_canary_run_id_exempt(self, tmp_path: Path) -> None:
        # The canary sidecar mirrors the MAIN run (task_count=1, main cmd_sha);
        # its identity is not independently derivable from tasks.py.
        _write_tasks_py(tmp_path)
        spec = _spec(run_id="demo-deadbeef-canary", cmd_sha="0" * 64, task_count=1)
        out = write_run_sidecar(experiment_dir=tmp_path, spec=spec)
        assert Path(out["path"]).is_file()
