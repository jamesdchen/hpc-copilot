"""RFC #362 §5 regression: trial-token/param round-trip through the sidecar.

The closed-loop out-of-order ``tell`` contract (RFC #362 §5) requires that the
opaque reconciliation tokens a strategy round-trips through ``resolve()`` — and
the resolved per-task params that are the ``cmd_sha`` pre-image — survive from
``compute-run-id`` (the ONE place the task list is materialized) all the way to
``prior_records`` (what a strategy reads at ``tasks.py`` load to reconcile a
finished iteration back to the proposal that produced it).

``compute_run_id`` surfaces ``trial_tokens`` + ``trial_params``
(``incorporation/build/compute_run_id.py::compute_run_id``);
``resolve_submit_inputs`` injects BOTH into the ``sidecar_spec``
``model_copy(update=…)`` — NOT just ``run_id``/``cmd_sha``
(``ops/resolve_submit_inputs.py`` §5 write step); ``write_run_sidecar`` persists
them (``state/runs.py``); ``prior_records`` re-surfaces them verbatim
(``execution/mapreduce/reduce/history.py::prior_records``).

Unlike ``tests/ops/test_resolve_submit_inputs.py`` (which asserts the *mocked*
``write_run_sidecar`` call args), this drives the REAL sidecar write to disk and
reads it back through ``prior_records`` — the end-to-end round-trip that is the
actual §5 contract. ``compute_run_id`` is mocked only at the resolve seam to
inject a tokened result; the real per-run write path and history reader run
unmocked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec
from hpc_agent.execution.mapreduce.reduce.history import prior_records

if TYPE_CHECKING:
    from pathlib import Path

_SEAM = "hpc_agent.ops.resolve_submit_inputs"

_CAMPAIGN_ID = "sweep-alpha"
_RUN_ID = "ridge-abcd1234"
_CMD_SHA = "a" * 64
_TOKENS = [7, 11]
_PARAMS = [{"seed": 1}, {"seed": 2}]


def _submit_input() -> BuildSubmitSpecInput:
    return BuildSubmitSpecInput(
        profile="ridge",
        cluster="h2",
        ssh_target="me@login.h2",
        remote_path="/scratch/me/exp",
        run_id="ridge-placeholder",  # overridden by compute-run-id
        cmd_sha="0" * 8,  # placeholder; overridden
        total_tasks=2,
        backend="sge",
    )


def _sidecar_input() -> WriteRunSidecarInput:
    # campaign_id is load-bearing: prior_records filters by it. A per-task
    # {task_id} placeholder satisfies the result-dir isolation validator.
    return WriteRunSidecarInput(
        run_id="ridge-placeholder",  # overridden by compute-run-id
        cmd_sha="0" * 8,  # placeholder; overridden
        executor="python train.py --output-file $RESULT_DIR/metrics.json",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=2,
        campaign_id=_CAMPAIGN_ID,
    )


def _spec() -> ResolveSubmitInputsSpec:
    return ResolveSubmitInputsSpec(
        run_name="ridge",
        submit=_submit_input(),
        sidecar=_sidecar_input(),
    )


def _tokened_cr() -> dict[str, Any]:
    """compute-run-id result carrying non-null trial_tokens (the campaign case)."""
    return {
        "run_id": _RUN_ID,
        "cmd_sha": _CMD_SHA,
        "trial_tokens": _TOKENS,
        "trial_params": _PARAMS,
        "total": 2,
    }


def _touch_stub_tasks_py(experiment_dir: Path) -> None:
    # A stub tasks.py makes the write path's _assert_identity_matches_tasks
    # cross-check treat the module as malformed (compute_run_id raises
    # SpecInvalid → the check returns without asserting), so the injected
    # identity stands. The identity itself is already cross-checked at the
    # resolve seam against the mocked compute-run-id total.
    hpc = experiment_dir / ".hpc"
    hpc.mkdir(parents=True, exist_ok=True)
    (hpc / "tasks.py").write_text("# stub\n", encoding="utf-8")


def test_trial_tokens_and_params_round_trip_to_prior_records(tmp_path: Path) -> None:
    """RFC #362 §5: resolve → REAL sidecar write → prior_records surfaces both.

    compute-run-id is authoritative for the per-task round-trip; its
    trial_tokens AND trial_params must land on the sidecar (not the caller's
    placeholders) and re-surface verbatim through prior_records for the
    out-of-order tell reconciliation to work.
    """
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_stub_tasks_py(tmp_path)
    built = {"profile": "ridge", "run_id": _RUN_ID, "total_tasks": 2}

    # Mock ONLY the resolve-seam atoms; write_run_sidecar runs for real so the
    # sidecar is actually persisted to <exp>/.hpc/runs/<run_id>.json.
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_tokened_cr()),
        mock.patch(f"{_SEAM}.find_prior_run", return_value={"found": False}),
        mock.patch(f"{_SEAM}.build_submit_spec", return_value=built),
        mock.patch(f"{_SEAM}.build_tasks_py"),
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    # The resolved path wrote the real sidecar.
    assert res.stage_reached == "resolved"
    assert res.run_id == _RUN_ID
    assert res.sidecar_path is not None
    sidecar_file = tmp_path / ".hpc" / "runs" / f"{_RUN_ID}.json"
    assert sidecar_file.is_file()

    # The end-to-end §5 contract: prior_records re-surfaces the tokens/params a
    # strategy round-tripped, keyed by campaign_id, read straight from disk.
    records = prior_records(tmp_path, _CAMPAIGN_ID)
    assert len(records) == 1
    rec = records[0]
    assert rec["run_id"] == _RUN_ID
    assert rec["campaign_id"] == _CAMPAIGN_ID
    assert rec["trial_tokens"] == _TOKENS
    assert rec["trial_params"] == _PARAMS


def test_trial_params_persist_without_tokens(tmp_path: Path) -> None:
    """An ordinary submit (no reconciliation token) still persists trial_params.

    trial_tokens is omitted (None) when no task carries one, but trial_params —
    the cmd_sha pre-image — is always surfaced so a run's params are recoverable
    from its sidecar. Guards the asymmetry in the §5 injection.
    """
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_stub_tasks_py(tmp_path)
    cr = _tokened_cr() | {"trial_tokens": None}

    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=cr),
        mock.patch(f"{_SEAM}.find_prior_run", return_value={"found": False}),
        mock.patch(f"{_SEAM}.build_submit_spec", return_value={"run_id": _RUN_ID}),
        mock.patch(f"{_SEAM}.build_tasks_py"),
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "resolved"
    records = prior_records(tmp_path, _CAMPAIGN_ID)
    assert len(records) == 1
    assert records[0]["trial_tokens"] is None
    assert records[0]["trial_params"] == _PARAMS
