"""The greenlight-names-target gate + the speculative canary (design §2, §3).

Covers:

* ``ops.block_gate.assert_greenlit_target`` — no record / nudge / wrong-verb /
  matching-greenlight paths.
* The sequenced block verbs refuse to act without a matching journaled
  greenlight (``submit-s2`` / ``aggregate-run`` as representatives).
* ``next_block`` is DETERMINISTICALLY computed at the right terminators.
* ``submit-speculate`` runs the canary, and no-ops on the budget dedup when the
  ``(cmd_sha, version)`` canary cache is validated-fresh.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

import hpc_agent.ops.submit_blocks as submit_blocks
from hpc_agent import errors
from hpc_agent.ops.block_gate import assert_greenlit_target
from hpc_agent.state.decision_journal import append_decision
from tests.ops._block_fixtures import greenlight

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "ml_run_gate01"


def _greenlight(experiment_dir: Path, verb: str, *, response: str = "y") -> None:
    greenlight(experiment_dir, verb, run_id=_RUN_ID, response=response)


# ── the gate itself ────────────────────────────────────────────────────────────


def test_gate_no_record_names_expected_flow(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid) as ei:
        assert_greenlit_target(tmp_path, run_id=_RUN_ID, verb="submit-s2", predecessor="S1")
    msg = str(ei.value)
    assert "no journaled greenlight for submit-s2" in msg
    assert "S1 brief" in msg


def test_gate_latest_is_nudge_refuses(tmp_path: Path) -> None:
    _greenlight(tmp_path, "submit-s2", response="no — halve the grid")
    with pytest.raises(errors.SpecInvalid) as ei:
        assert_greenlit_target(tmp_path, run_id=_RUN_ID, verb="submit-s2", predecessor="S1")
    assert "nudge, not a greenlight" in str(ei.value)


def test_gate_wrong_verb_names_both(tmp_path: Path) -> None:
    _greenlight(tmp_path, "submit-s3")  # greenlit a DIFFERENT block
    with pytest.raises(errors.SpecInvalid) as ei:
        assert_greenlit_target(tmp_path, run_id=_RUN_ID, verb="submit-s2", predecessor="S1")
    msg = str(ei.value)
    assert "submit-s3" in msg and "submit-s2" in msg


def test_gate_matching_greenlight_passes(tmp_path: Path) -> None:
    _greenlight(tmp_path, "submit-s2")
    assert_greenlit_target(tmp_path, run_id=_RUN_ID, verb="submit-s2", predecessor="S1")


def test_gate_greenlight_survives_later_unrelated_touchpoints(tmp_path: Path) -> None:
    """The wedge fix: a greenlight for the verb is NOT retracted by an unrelated
    later touchpoint in the SHARED run journal — the gate scans for the latest
    greenlight naming *this* verb, so a trailing nudge and a `y` for a different
    verb both fall through to the still-standing s2 greenlight.

    (Consumption is NOT enforced here — a later same-verb nudge does not
    retract; replay is backstopped by run-dedup. See gate TODO(wave4).)"""
    _greenlight(tmp_path, "submit-s2")
    _greenlight(tmp_path, "submit-s2", response="actually hold on")  # nudge, not a retraction
    _greenlight(tmp_path, "submit-s3")  # unrelated greenlight for a DIFFERENT verb
    assert_greenlit_target(tmp_path, run_id=_RUN_ID, verb="submit-s2", predecessor="S1")


def test_gate_no_greenlight_for_verb_among_other_records_refuses(tmp_path: Path) -> None:
    """Regression: records exist but NONE greenlights this verb → still raises.
    Here the only greenlight names a different verb, preceded by a nudge — the
    scan finds no `y` for submit-s2 and fails closed."""
    _greenlight(tmp_path, "submit-s2", response="no — hold on")  # nudge, not a `y`
    _greenlight(tmp_path, "submit-s3")  # greenlight for a DIFFERENT verb
    with pytest.raises(errors.SpecInvalid) as ei:
        assert_greenlit_target(tmp_path, run_id=_RUN_ID, verb="submit-s2", predecessor="S1")
    msg = str(ei.value)
    assert "submit-s3" in msg and "submit-s2" in msg


def test_gate_accepts_whole_hint_dict(tmp_path: Path) -> None:
    """A greenlight that journaled the whole ``{verb, ...}`` hint (not the bare
    string) is still honored — the verb is extracted."""
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=_RUN_ID,
        block="s1",
        response="y",
        resolved={"next_block": {"verb": "submit-s2", "why": "go"}},
    )
    assert_greenlit_target(tmp_path, run_id=_RUN_ID, verb="submit-s2", predecessor="S1")


# ── the block verbs enforce the gate ───────────────────────────────────────────


def _sv_spec():
    from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec, SubmitResources

    submit = SubmitFlowSpec(
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@h",
        remote_path="/x",
        job_name="ml",
        run_id=_RUN_ID,
        total_tasks=10,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"K": "v"},
        canary=True,
        resources=SubmitResources(walltime_sec=3600, cpus=4),
    )
    return SubmitAndVerifySpec(submit=submit, poll_interval_sec=1, wait_budget_sec=5)


def test_submit_s2_refuses_without_greenlight(tmp_path: Path) -> None:
    from hpc_agent._wire.workflows.submit_blocks import SubmitS2Spec

    with pytest.raises(errors.SpecInvalid) as ei:
        submit_blocks.submit_s2(tmp_path, spec=SubmitS2Spec(submit=_sv_spec(), detach=False))
    assert "no journaled greenlight for submit-s2" in str(ei.value)


def test_submit_s2_next_block_points_to_s3(tmp_path: Path) -> None:
    from hpc_agent._wire.workflows.submit_blocks import SubmitS2Spec

    _greenlight(tmp_path, "submit-s2")

    def _sv_result(*_a: Any, **_k: Any):
        from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifyResult

        return SubmitAndVerifyResult(
            run_id=_RUN_ID,
            job_ids=[],
            total_tasks=10,
            deduped=False,
            canary_run_id=f"{_RUN_ID}_canary",
            canary_job_ids=["12344"],
            verified=True,
            failure_kind=None,
            verify_result=None,
        )

    with mock.patch.object(submit_blocks, "submit_and_verify", side_effect=_sv_result):
        result = submit_blocks.submit_s2(
            tmp_path, spec=SubmitS2Spec(submit=_sv_spec(), detach=False)
        )

    assert result.stage_reached == "canary_verified"
    assert result.next_block is not None
    assert result.next_block["verb"] == "submit-s3"
    assert result.next_block["spec_hint"]["run_id"] == _RUN_ID
    assert result.next_block["spec_hint"]["canary_run_id"] == f"{_RUN_ID}_canary"


def test_s1_clean_resolved_next_block_points_to_s2(tmp_path: Path) -> None:
    from hpc_agent._wire.queries.walk_submit_ambiguities import WalkSubmitAmbiguitiesInput
    from hpc_agent._wire.workflows.submit_blocks import SubmitS1Spec

    walk = WalkSubmitAmbiguitiesInput.model_validate(
        {
            "cluster": "hoffman2",
            "goal": "g",
            "tasks_py_present": True,
            "entry_point_resolved": True,
            "data_axis_resolved": True,
            "homogeneous_axes_resolved": True,
        }
    )
    result = submit_blocks.submit_s1(tmp_path, spec=SubmitS1Spec(walk=walk, run_preflight=False))
    assert result.stage_reached == "resolved"
    assert result.next_block is not None
    assert result.next_block["verb"] == "submit-s2"


def test_s1_needs_resolution_has_no_next_block(tmp_path: Path) -> None:
    from hpc_agent._wire.queries.walk_submit_ambiguities import WalkSubmitAmbiguitiesInput
    from hpc_agent._wire.workflows.submit_blocks import SubmitS1Spec

    walk = WalkSubmitAmbiguitiesInput.model_validate(
        {
            "cluster": None,
            "configured_clusters": ["carc", "hoffman2"],
            "goal": "g",
            "tasks_py_present": True,
            "entry_point_resolved": True,
            "data_axis_resolved": True,
            "homogeneous_axes_resolved": True,
        }
    )
    result = submit_blocks.submit_s1(tmp_path, spec=SubmitS1Spec(walk=walk, run_preflight=False))
    assert result.stage_reached == "needs_resolution"
    assert result.next_block is None


# ── submit-speculate ───────────────────────────────────────────────────────────


def _speculate_spec():
    from hpc_agent._wire.workflows.submit_speculate import SubmitSpeculateSpec

    return SubmitSpeculateSpec(submit=_sv_spec(), detach=False)


def test_speculate_runs_canary_when_not_cached(tmp_path: Path) -> None:
    import hpc_agent.ops.submit_speculate as spec_mod

    def _sv(*_a: Any, **_k: Any):
        from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifyResult

        return SubmitAndVerifyResult(
            run_id=_RUN_ID,
            job_ids=[],
            total_tasks=10,
            deduped=False,
            canary_run_id=f"{_RUN_ID}_canary",
            canary_job_ids=["12344"],
            verified=True,
            failure_kind=None,
            verify_result=None,
        )

    # job_env has no HPC_CMD_SHA → cache key is None → speculation proceeds.
    with mock.patch.object(spec_mod, "submit_and_verify", side_effect=_sv) as m:
        result = spec_mod.submit_speculate(tmp_path, spec=_speculate_spec())

    assert m.call_args.kwargs["stop_after_canary"] is True
    assert result.speculated is True
    assert result.verified is True
    assert result.canary_run_id == f"{_RUN_ID}_canary"


def test_speculate_noops_when_canary_validated_fresh(tmp_path: Path, monkeypatch) -> None:
    """Budget = 1 per brief: a validated-fresh (cmd_sha, version) refuses a fresh
    canary — the TTL cache IS the dedup (no extra machinery)."""
    import hpc_agent.ops.submit_speculate as spec_mod
    from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec, SubmitResources
    from hpc_agent._wire.workflows.submit_speculate import SubmitSpeculateSpec

    submit = SubmitFlowSpec(
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@h",
        remote_path="/x",
        job_name="ml",
        run_id=_RUN_ID,
        total_tasks=10,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"HPC_CMD_SHA": "deadbeef"},
        canary=True,
        resources=SubmitResources(walltime_sec=3600, cpus=4),
    )
    spec = SubmitSpeculateSpec(
        submit=SubmitAndVerifySpec(submit=submit, poll_interval_sec=1, wait_budget_sec=5),
        detach=False,
    )

    monkeypatch.setattr(
        "hpc_agent.state.canary_cache.is_canary_validated_fresh", lambda *_a, **_k: True
    )
    monkeypatch.setattr("hpc_agent.state.canary_cache.cache_disabled", lambda: False)
    with mock.patch.object(spec_mod, "submit_and_verify") as m:
        result = spec_mod.submit_speculate(tmp_path, spec=spec)

    m.assert_not_called()  # never fires a redundant canary
    assert result.speculated is False
    assert result.verified is True
    assert "already validated-fresh" in result.reason


# ── L3: code-fired speculative canary at S1 resolved-park (R2 APPROVED) ──────────


def _resolved_s1(tmp_path: Path, *, cmd_sha: str = "deadbeef") -> Any:
    """Drive submit_s1 to a CLEAN resolved-park (run_id minted, submit_spec built),
    with resolve_submit_inputs patched so the test stays cluster-free."""
    from hpc_agent._wire.queries.walk_submit_ambiguities import WalkSubmitAmbiguitiesInput
    from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsResult
    from hpc_agent._wire.workflows.submit_blocks import SubmitS1Spec

    walk = WalkSubmitAmbiguitiesInput.model_validate(
        {
            "cluster": "hoffman2",
            "configured_clusters": ["carc", "hoffman2"],
            "goal": "g",
            "tasks_py_present": True,
            "entry_point_resolved": True,
            "data_axis_resolved": True,
            "homogeneous_axes_resolved": True,
        }
    )
    spec = SubmitS1Spec.model_construct(walk=walk, run_preflight=False, resolve=object())
    submit_flow = {
        "profile": "ml",
        "cluster": "hoffman2",
        "run_id": _RUN_ID,
        "job_env": {"HPC_CMD_SHA": cmd_sha},
        "canary": True,
    }
    fake_rr = ResolveSubmitInputsResult(
        stage_reached="resolved",
        needs_decision=True,
        reason="plan resolved; stage & canary.",
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        submit_spec=submit_flow,
        sidecar_path=str(tmp_path / ".hpc" / "runs" / f"{_RUN_ID}.json"),
    )
    return spec, fake_rr, submit_flow


def test_s1_resolved_park_fires_speculative_canary_in_code(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HPC_S1_SPECULATE", "1")
    spec, fake_rr, submit_flow = _resolved_s1(tmp_path)
    with (
        mock.patch.object(submit_blocks, "resolve_submit_inputs", return_value=fake_rr),
        mock.patch("hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached") as launch,
    ):
        result = submit_blocks.submit_s1(tmp_path, spec=spec)

    assert result.stage_reached == "resolved"
    launch.assert_called_once()
    kwargs = launch.call_args.kwargs
    assert kwargs["verb"] == "submit-speculate"
    # stop_after_canary is unstrippable: submit-speculate is the ONLY verb fired,
    # and it composes submit-and-verify(stop_after_canary=True) — the main array is
    # unreachable from it. The composed spec threads the SAME submit_flow S2 runs.
    fired = kwargs["spec"]
    assert fired["submit"]["submit"] == submit_flow
    # cmd_sha identity flows through, so a nudge that moves it ORPHANS the canary.
    assert fired["submit"]["submit"]["job_env"]["HPC_CMD_SHA"] == "deadbeef"
    assert fired["detach"] is False


def test_s1_speculation_off_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HPC_S1_SPECULATE", raising=False)
    spec, fake_rr, _ = _resolved_s1(tmp_path)
    with (
        mock.patch.object(submit_blocks, "resolve_submit_inputs", return_value=fake_rr),
        mock.patch("hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached") as launch,
    ):
        submit_blocks.submit_s1(tmp_path, spec=spec)
    launch.assert_not_called()


def test_s1_speculation_skips_ambiguous_walk(tmp_path: Path, monkeypatch) -> None:
    """Required ambiguities → skip (the same rule the hpc-submit skill applies)."""
    from hpc_agent._wire.queries.walk_submit_ambiguities import WalkSubmitAmbiguitiesInput
    from hpc_agent._wire.workflows.submit_blocks import SubmitS1Spec

    monkeypatch.setenv("HPC_S1_SPECULATE", "1")
    walk = WalkSubmitAmbiguitiesInput.model_validate(
        {
            "cluster": None,
            "configured_clusters": ["carc", "hoffman2"],
            "goal": "g",
            "tasks_py_present": True,
            "entry_point_resolved": True,
            "data_axis_resolved": True,
            "homogeneous_axes_resolved": True,
        }
    )
    spec = SubmitS1Spec(walk=walk, run_preflight=False)
    with mock.patch("hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached") as launch:
        result = submit_blocks.submit_s1(tmp_path, spec=spec)
    assert result.stage_reached == "needs_resolution"
    launch.assert_not_called()


def test_s1_speculative_fire_leaves_the_journal_untouched(tmp_path: Path, monkeypatch) -> None:
    """The speculative fire writes the _detached/ handle only — no decision/brief."""
    from hpc_agent.state.decision_journal import read_decisions

    monkeypatch.setenv("HPC_S1_SPECULATE", "1")
    spec, fake_rr, _ = _resolved_s1(tmp_path)
    with (
        mock.patch.object(submit_blocks, "resolve_submit_inputs", return_value=fake_rr),
        mock.patch("hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached"),
    ):
        submit_blocks.submit_s1(tmp_path, spec=spec)
    assert read_decisions(tmp_path, "run", _RUN_ID) == []
