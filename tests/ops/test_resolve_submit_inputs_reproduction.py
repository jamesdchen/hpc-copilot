"""Reproduction-receipt lever threaded through ``resolve-submit-inputs`` (S1).

A deliberate reproduction of identical params names the ORIGINAL run via
``reproduction_of``. At S1 that must:

* make ``find-prior-run`` skip the original (a ``complete`` original no longer
  terminates resolve at ``prior_run_found``) — but ANY OTHER live prior with the
  same params still fires the guard (the fork keeps its fire path);
* stamp ``reproduces`` onto the derived run's sidecar spec (so a later
  reproduction of the same original skips this one too);
* thread ``reproduction_of`` onto the built submit-flow spec (so the detached
  submit-flow worker's layer-2 dedup pierces the same original).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest import mock

from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec

if TYPE_CHECKING:
    from pathlib import Path

_SEAM = "hpc_agent.ops.resolve_submit_inputs"
_ORIGINAL = "ridge-orig1234"


def _submit_input() -> BuildSubmitSpecInput:
    return BuildSubmitSpecInput(
        profile="ridge",
        cluster="h2",
        ssh_target="me@login.h2",
        remote_path="/scratch/me/exp",
        run_id="ridge-abcd1234",
        cmd_sha="a" * 64,
        total_tasks=2,
        backend="sge",
    )


def _sidecar_input() -> WriteRunSidecarInput:
    return WriteRunSidecarInput(
        run_id="ridge-placeholder",
        cmd_sha="0" * 8,
        executor="python -m src.ridge --alpha $alpha",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=2,
    )


def _spec(reproduction_of: str | None) -> ResolveSubmitInputsSpec:
    return ResolveSubmitInputsSpec(
        run_name="ridge",
        submit=_submit_input(),
        sidecar=_sidecar_input(),
        reproduction_of=reproduction_of,
    )


def _cr() -> dict[str, Any]:
    return {
        "run_id": "ridge-abcd1234",
        "cmd_sha": "a" * 64,
        "trial_tokens": None,
        "trial_params": [{"alpha": 0.1}, {"alpha": 1.0}],
        "total": 2,
    }


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


def test_complete_original_with_reproduction_of_proceeds(tmp_path: Path) -> None:
    """A ``complete`` original that ``reproduction_of`` names is SKIPPED by
    find-prior-run (returns found=False), so resolve reaches the clean
    ``resolved`` terminal — and the lever is threaded onto BOTH the sidecar spec
    (as ``reproduces``) and the built submit-flow spec (as ``reproduction_of``)."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    built: dict[str, Any] = {"profile": "ridge", "run_id": "ridge-abcd1234", "total_tasks": 2}
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        # The real find_prior_run would skip the original for this cmd_sha; the
        # mock reflects that outcome (found=False) so the composite proceeds.
        mock.patch(f"{_SEAM}.find_prior_run", return_value=_fp(found=False)) as fp,
        mock.patch(f"{_SEAM}.build_submit_spec", return_value=built),
        mock.patch(f"{_SEAM}.write_run_sidecar", return_value={"path": "/x.json"}) as ws,
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec(reproduction_of=_ORIGINAL))

    assert res.stage_reached == "resolved"
    assert res.needs_decision is False
    # The lever reached find-prior-run so the original could be skipped.
    assert fp.call_args.kwargs["reproduction_of"] == _ORIGINAL
    # Sidecar records which original this derived run reproduces.
    assert ws.call_args.kwargs["spec"].reproduces == _ORIGINAL
    # And the built submit-flow spec carries the lever for the submit-time dedup.
    assert res.submit_spec is not None
    assert res.submit_spec["reproduction_of"] == _ORIGINAL


def test_complete_unrelated_prior_still_escalates(tmp_path: Path) -> None:
    """The guard keeps its fire path: a ``complete`` prior that find-prior-run
    still surfaces (an UNRELATED same-params run, not the named original) is a
    live prior — resolve escalates ``prior_run_found`` even with the lever set."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(
            f"{_SEAM}.find_prior_run",
            return_value=_fp(found=True, status="complete", prior_run_id="ridge-other999"),
        ),
        mock.patch(f"{_SEAM}.build_submit_spec") as bs,
        mock.patch(f"{_SEAM}.write_run_sidecar") as ws,
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec(reproduction_of=_ORIGINAL))

    assert res.stage_reached == "prior_run_found"
    assert res.needs_decision is True
    assert res.prior_run_id == "ridge-other999"
    bs.assert_not_called()
    ws.assert_not_called()


def test_lever_unset_does_not_thread_reproduction(tmp_path: Path) -> None:
    """No ``reproduction_of``: find-prior-run is called with None, the sidecar's
    ``reproduces`` stays None, and the built spec gains no ``reproduction_of``
    key (byte-identical to the pre-feature resolved path)."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    built: dict[str, Any] = {"profile": "ridge", "run_id": "ridge-abcd1234", "total_tasks": 2}
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_SEAM}.find_prior_run", return_value=_fp(found=False)) as fp,
        mock.patch(f"{_SEAM}.build_submit_spec", return_value=built),
        mock.patch(f"{_SEAM}.write_run_sidecar", return_value={"path": "/x.json"}) as ws,
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec(reproduction_of=None))

    assert res.stage_reached == "resolved"
    assert fp.call_args.kwargs["reproduction_of"] is None
    assert ws.call_args.kwargs["spec"].reproduces is None
    assert res.submit_spec is not None
    assert "reproduction_of" not in res.submit_spec


def test_find_prior_run_wrapper_skips_the_original_end_to_end(tmp_path: Path) -> None:
    """The find-prior-run wrapper (setup_actions) genuinely skips the original
    when the lever names it: with only the original on disk, a reproduction
    lookup reports found=False, while an unset lookup finds it."""
    from hpc_agent.cli.setup_actions import find_prior_run

    runs = tmp_path / ".hpc" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{_ORIGINAL}.json").write_text(
        json.dumps(
            {
                "sidecar_schema_version": 2,
                "run_id": _ORIGINAL,
                "cmd_sha": "a" * 64,
                "hpc_agent_version": "0.2.0",
                "submitted_at": "2026-01-01T00:00:00Z",
                "executor": "python3 src/run.py",
                "result_dir_template": "results/{task_id}",
                "task_count": 2,
                "tasks_py_sha": "1" * 64,
                "job_ids": ["12345"],
            }
        ),
        encoding="utf-8",
    )

    # Unset → the original is a prior.
    assert find_prior_run(tmp_path, cmd_sha="a" * 64)["found"] is True
    # Reproduction of that original → the original is skipped, no prior found.
    assert find_prior_run(tmp_path, cmd_sha="a" * 64, reproduction_of=_ORIGINAL)["found"] is False
