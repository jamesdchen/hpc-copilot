"""Tests for the ``resolve-submit-inputs`` composite.

The composite chains four laptop-side atoms (compute-run-id, find-prior-run,
build-tasks-py, build-submit-spec) and branches on tasks.py presence + the
find-prior-run resume contract. These tests mock each atom at the
``resolve_submit_inputs`` module seam and exercise every ``stage_reached``
path — no cluster, no journal, ``tmp_path`` for the experiment dir.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
from hpc_agent._wire.actions.build_tasks_py import BuildTasksPyInput
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec

if TYPE_CHECKING:
    from pathlib import Path

_SEAM = "hpc_agent.ops.resolve_submit_inputs"


def _submit_input() -> BuildSubmitSpecInput:
    return BuildSubmitSpecInput(
        profile="ridge",
        cluster="h2",
        ssh_target="me@login.h2",
        remote_path="/scratch/me/exp",
        run_id="ridge-abcd1234",
        cmd_sha="a" * 64,
        total_tasks=4,
        backend="sge",
    )


def _sidecar_input() -> WriteRunSidecarInput:
    return WriteRunSidecarInput(
        run_id="ridge-placeholder",  # overridden by compute-run-id inside the composite
        cmd_sha="0" * 8,  # placeholder; overridden too
        executor="python -m src.ridge --alpha $alpha",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=4,
    )


def _sidecar_ret() -> dict[str, Any]:
    return {"path": "/tmp/exp/.hpc/runs/ridge-abcd1234.json"}


def _build_tasks_input() -> BuildTasksPyInput:
    return BuildTasksPyInput(
        axes=[{"name": "exp_alpha", "values": [0.1, 1.0]}],
        flags_by_executor={"src.ridge": [{"name": "alpha", "type": "float"}]},
    )


def _spec(build_tasks: BuildTasksPyInput | None = None) -> ResolveSubmitInputsSpec:
    return ResolveSubmitInputsSpec(
        run_name="ridge",
        submit=_submit_input(),
        sidecar=_sidecar_input(),
        build_tasks=build_tasks,
    )


def _cr() -> dict[str, Any]:
    return {"run_id": "ridge-abcd1234", "cmd_sha": "a" * 64, "trial_tokens": None}


def _fp(
    *,
    found: bool = False,
    is_orphan: bool = False,
    status: str | None = None,
    prior_run_id: str | None = None,
) -> dict[str, Any]:
    return {
        "found": found,
        "prior_run_id": prior_run_id,
        "is_orphan": is_orphan,
        "status": status,
        "age_sec": None,
        "profile": None,
        "cluster": None,
        "job_ids": [],
        "campaign_id": None,
        "submitted_at": None,
    }


def _touch_tasks_py(experiment_dir: Path) -> None:
    hpc = experiment_dir / ".hpc"
    hpc.mkdir(parents=True, exist_ok=True)
    (hpc / "tasks.py").write_text("# stub\n", encoding="utf-8")


def test_resolved_builds_submit_spec(tmp_path: Path) -> None:
    """tasks.py present, no prior → resolved, carries the built submit spec."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    built = {"profile": "ridge", "run_id": "ridge-abcd1234", "total_tasks": 4}
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_SEAM}.find_prior_run", return_value=_fp(found=False)) as fp,
        mock.patch(f"{_SEAM}.build_submit_spec", return_value=built) as bs,
        mock.patch(f"{_SEAM}.write_run_sidecar", return_value=_sidecar_ret()) as ws,
        mock.patch(f"{_SEAM}.build_tasks_py") as bt,
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "resolved"
    assert res.needs_decision is False
    assert res.run_id == "ridge-abcd1234"
    assert res.cmd_sha == "a" * 64
    assert res.submit_spec == built
    assert res.sidecar_path == "/tmp/exp/.hpc/runs/ridge-abcd1234.json"
    assert res.prior_run_id is None
    fp.assert_called_once()
    bs.assert_called_once()
    ws.assert_called_once()  # per-run sidecar written on the resolved path (#171)
    # compute-run-id values are injected into BOTH downstream inputs, overriding
    # the placeholders the caller passed — so the built spec + sidecar match the
    # reported run_id.
    assert bs.call_args.kwargs["spec"].run_id == "ridge-abcd1234"
    assert ws.call_args.kwargs["spec"].run_id == "ridge-abcd1234"
    assert ws.call_args.kwargs["spec"].cmd_sha == "a" * 64
    bt.assert_not_called()  # tasks.py present → no scaffold


def test_terminal_failed_prior_is_not_live_proceeds_fresh(tmp_path: Path) -> None:
    """A failed/abandoned record (#276) is forensic, not live → resolved."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    built = {"profile": "ridge"}
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(
            f"{_SEAM}.find_prior_run",
            return_value=_fp(found=True, status="failed", prior_run_id="ridge-dead0000"),
        ),
        mock.patch(f"{_SEAM}.build_submit_spec", return_value=built),
        mock.patch(f"{_SEAM}.write_run_sidecar", return_value=_sidecar_ret()),
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "resolved"
    assert res.needs_decision is False
    assert res.submit_spec == built


def test_live_prior_complete_escalates(tmp_path: Path) -> None:
    """A live complete prior → prior_run_found, resume-vs-fresh is the user's call."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(
            f"{_SEAM}.find_prior_run",
            return_value=_fp(found=True, status="complete", prior_run_id="ridge-abcd1234"),
        ),
        mock.patch(f"{_SEAM}.build_submit_spec") as bs,
        mock.patch(f"{_SEAM}.write_run_sidecar") as ws,
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "prior_run_found"
    assert res.needs_decision is True
    assert res.prior_run_id == "ridge-abcd1234"
    assert res.prior_status == "complete"
    assert res.submit_spec is None
    assert res.sidecar_path is None
    bs.assert_not_called()  # stopped before building the spec
    ws.assert_not_called()  # and before writing the sidecar


def test_live_prior_in_flight_escalates(tmp_path: Path) -> None:
    """An in_flight prior also blocks (a timed-out run stays in_flight)."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(
            f"{_SEAM}.find_prior_run",
            return_value=_fp(found=True, status="in_flight", prior_run_id="ridge-abcd1234"),
        ),
        mock.patch(f"{_SEAM}.build_submit_spec"),
        mock.patch(f"{_SEAM}.write_run_sidecar"),
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "prior_run_found"
    assert res.needs_decision is True
    assert res.prior_status == "in_flight"


def test_orphan_prior_is_not_live_proceeds_fresh(tmp_path: Path) -> None:
    """A half-baked orphan sidecar is not a real prior → resolved."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(
            f"{_SEAM}.find_prior_run",
            return_value=_fp(found=True, is_orphan=True, status="complete"),
        ),
        mock.patch(f"{_SEAM}.build_submit_spec", return_value={"ok": 1}),
        mock.patch(f"{_SEAM}.write_run_sidecar", return_value=_sidecar_ret()),
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "resolved"
    assert res.needs_decision is False


def test_absent_tasks_no_scaffold_spec_escalates(tmp_path: Path) -> None:
    """tasks.py absent + no build_tasks spec → needs_scaffold_interview."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    # No .hpc/tasks.py on disk; build_tasks omitted.
    with (
        mock.patch(f"{_SEAM}.compute_run_id") as cr,
        mock.patch(f"{_SEAM}.find_prior_run") as fp,
        mock.patch(f"{_SEAM}.build_submit_spec") as bs,
        mock.patch(f"{_SEAM}.write_run_sidecar") as ws,
        mock.patch(f"{_SEAM}.build_tasks_py") as bt,
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec(build_tasks=None))

    assert res.stage_reached == "needs_scaffold_interview"
    assert res.needs_decision is True
    assert res.run_id is None
    assert res.submit_spec is None
    assert res.sidecar_path is None
    # Stopped at the escalation — none of the downstream atoms ran.
    cr.assert_not_called()
    fp.assert_not_called()
    bs.assert_not_called()
    ws.assert_not_called()
    bt.assert_not_called()


def test_absent_tasks_with_scaffold_spec_builds_then_resolves(tmp_path: Path) -> None:
    """tasks.py absent + build_tasks supplied → scaffold deterministically, then resolved."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    # build_tasks_py is mocked, so it won't actually write tasks.py; that is
    # fine — the seam is what we test, and compute_run_id is also mocked.
    built = {"profile": "ridge"}
    with (
        mock.patch(f"{_SEAM}.build_tasks_py", return_value={"wrote": True}) as bt,
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_SEAM}.find_prior_run", return_value=_fp(found=False)),
        mock.patch(f"{_SEAM}.build_submit_spec", return_value=built),
        mock.patch(f"{_SEAM}.write_run_sidecar", return_value=_sidecar_ret()),
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec(build_tasks=_build_tasks_input()))

    assert res.stage_reached == "resolved"
    assert res.needs_decision is False
    assert res.submit_spec == built
    bt.assert_called_once()  # the deterministic scaffold fired
