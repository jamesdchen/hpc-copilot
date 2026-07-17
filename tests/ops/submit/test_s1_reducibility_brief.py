"""S1 reducibility shift-left (run #12 finding 28, RULED *disclose* 2026-07-17):
S1's resolved brief — the human boundary the greenlight crosses BEFORE submit-s2
detaches and spends the whole compute — carries a CODE-computed reducibility
DISCLOSURE. A run with NO ``aggregate_cmd`` and a non-JSON summary artifact can
NEVER reduce through the built-in per-task fallback (a JSON weighted-mean), so the
aggregate would refuse only AFTER a 40+ min results/ pull. Surfacing it at S1 is a
disclosure, never a gate: the bare ``y`` flow is unchanged; the park brief just
gains lines. The predicate is the SAME one aggregate-CHECK keys on — a THIRD seat
of the one ``per_task_fallback_reducible`` definition, reused verbatim (no new
prose, no second JSON-name test).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import hpc_agent.ops.aggregate_blocks as agg_blocks
import hpc_agent.ops.submit_blocks as blocks
from hpc_agent._wire.queries.walk_submit_ambiguities import WalkSubmitAmbiguitiesInput
from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsResult
from hpc_agent._wire.workflows.submit_blocks import SubmitS1Spec

_RUN_ID = "ridge-abcd1234"


def _write_sidecar(experiment_dir: Path, **overrides: Any) -> None:
    """Write a per-run sidecar for the reducibility predicate (mirrors the
    aggregate-CHECK test's helper — ``summary_artifact`` / ``aggregate_defaults``
    are the two records the shared predicate reads)."""
    from hpc_agent.state.runs import write_run_sidecar

    base: dict[str, Any] = {
        "cmd_sha": "deadbeef",
        "run_id": _RUN_ID,
        "hpc_agent_version": "0.0.0-test",
        "submitted_at": "2026-01-01T00:00:00+00:00",
        "executor": "python run.py",
        "result_dir_template": "results/causal_tune_linear/{estimator}/task-{task_id}",
        "task_count": 4,
        "tasks_py_sha": "",
    }
    base.update(overrides)
    write_run_sidecar(experiment_dir, **base)


def _clean_walk() -> WalkSubmitAmbiguitiesInput:
    return WalkSubmitAmbiguitiesInput.model_validate(
        {
            "cluster": "carc",
            "configured_clusters": ["carc", "hoffman2"],
            "goal": "sweep ridge",
            "tasks_py_present": True,
            "entry_point_resolved": True,
            "data_axis_resolved": True,
            "homogeneous_axes_resolved": True,
        }
    )


def _fake_rr(sidecar_path: Path, *, run_id: str | None = _RUN_ID) -> ResolveSubmitInputsResult:
    return ResolveSubmitInputsResult(
        stage_reached="resolved",
        needs_decision=True,
        reason="plan resolved; stage & canary.",
        run_id=run_id,
        cmd_sha="0" * 64,
        submit_spec={"rsync_excludes": None},
        sidecar_path=str(sidecar_path),
    )


# ── the pure helper ───────────────────────────────────────────────────────────


def test_reducibility_brief_irreducible_carries_the_issue_verbatim(tmp_path: Path) -> None:
    """A no-``aggregate_cmd`` + non-JSON-artifact sidecar → the never-auto-masked
    issue dict (the aggregate-CHECK rendering, reused) plus the S1 consequence."""
    _write_sidecar(tmp_path, summary_artifact="causal_tune_linear/metrics_table.csv")

    brief = blocks._reducibility_brief(tmp_path, _RUN_ID)

    assert brief is not None
    assert brief["checked"] is True
    assert brief["issue"] == "non_reducible_summary_artifact"
    assert brief["auto_masked"] is False
    assert brief["detail"]["summary_artifact"] == "causal_tune_linear/metrics_table.csv"
    assert "aggregate_cmd" in brief["recommendation"]  # the reused predicate prose
    assert "consequence" in brief and brief["consequence"]  # the S1-boundary line


def test_reducibility_brief_reducible_returns_none(tmp_path: Path) -> None:
    """A JSON artifact reduces cleanly → None, so the brief stays byte-identical."""
    _write_sidecar(tmp_path, summary_artifact="metrics.json")
    assert blocks._reducibility_brief(tmp_path, _RUN_ID) is None


def test_reducibility_brief_aggregate_cmd_returns_none(tmp_path: Path) -> None:
    """A custom reducer (``aggregate_cmd``) routes away from the fallback → None,
    even for a non-JSON artifact (mirrors the CHECK seat)."""
    _write_sidecar(
        tmp_path,
        summary_artifact="causal_tune_linear/metrics_table.csv",
        aggregate_defaults={"aggregate_cmd": "python reduce.py"},
    )
    assert blocks._reducibility_brief(tmp_path, _RUN_ID) is None


def test_reducibility_brief_no_run_id_says_could_not_run(tmp_path: Path) -> None:
    """No minted run_id → the honest could-not-run line, never a silent skip."""
    brief = blocks._reducibility_brief(tmp_path, None)
    assert brief is not None
    assert brief["checked"] is False
    assert "could not be checked" in brief["reason"]


def test_reducibility_brief_unreadable_sidecar_says_could_not_run(tmp_path: Path) -> None:
    """A run_id whose sidecar is absent/unreadable → could-not-run, not a clean
    pass (no-silent-caps: the brief SAYS the check could not run)."""
    brief = blocks._reducibility_brief(tmp_path, _RUN_ID)  # no sidecar written
    assert brief is not None
    assert brief["checked"] is False
    assert "could not be checked" in brief["reason"]


def test_s1_seat_routes_through_the_one_aggregate_predicate(tmp_path: Path) -> None:
    """The S1 seat and the aggregate-CHECK seat are the SAME call: the S1 helper
    routes through ``aggregate_blocks._reducibility_issue`` (the one
    ``per_task_fallback_reducible`` home), never a re-inlined JSON-name test."""
    _write_sidecar(tmp_path, summary_artifact="causal_tune_linear/metrics_table.csv")

    with mock.patch.object(
        agg_blocks, "_reducibility_issue", wraps=agg_blocks._reducibility_issue
    ) as spy:
        blocks._reducibility_brief(tmp_path, _RUN_ID)

    spy.assert_called_once_with(tmp_path, _RUN_ID)


# ── S1 wiring ─────────────────────────────────────────────────────────────────


def test_s1_resolved_brief_carries_reducibility_disclosure(tmp_path: Path) -> None:
    """Wiring: submit_s1's CLEAN-RESOLVE brief carries the reducibility disclosure
    for an irreducible plan, beside the deploy_payload block."""
    (tmp_path / "tasks.py").write_text("x")
    _write_sidecar(tmp_path, summary_artifact="causal_tune_linear/metrics_table.csv")
    spec = SubmitS1Spec.model_construct(walk=_clean_walk(), run_preflight=False, resolve=object())
    sidecar_path = tmp_path / ".hpc" / "runs" / f"{_RUN_ID}.json"

    with mock.patch.object(blocks, "resolve_submit_inputs", return_value=_fake_rr(sidecar_path)):
        result = blocks.submit_s1(tmp_path, spec=spec)

    assert result.stage_reached == "resolved"
    red = result.brief["reducibility"]
    assert red["checked"] is True
    assert red["issue"] == "non_reducible_summary_artifact"
    assert red["auto_masked"] is False


def test_s1_reducible_plan_brief_omits_the_key(tmp_path: Path) -> None:
    """Regression pin: a reducible plan (JSON artifact) leaves the S1 brief
    byte-unchanged — no ``reducibility`` key at all."""
    (tmp_path / "tasks.py").write_text("x")
    _write_sidecar(tmp_path, summary_artifact="metrics.json")
    spec = SubmitS1Spec.model_construct(walk=_clean_walk(), run_preflight=False, resolve=object())
    sidecar_path = tmp_path / ".hpc" / "runs" / f"{_RUN_ID}.json"

    with mock.patch.object(blocks, "resolve_submit_inputs", return_value=_fake_rr(sidecar_path)):
        result = blocks.submit_s1(tmp_path, spec=spec)

    assert result.stage_reached == "resolved"
    assert "reducibility" not in result.brief


def test_s1_could_not_run_line_fires_when_sidecar_absent(tmp_path: Path) -> None:
    """Inputs absent (resolve minted a run_id but no readable sidecar) → the brief
    carries the honest could-not-run line, never a silent skip."""
    (tmp_path / "tasks.py").write_text("x")
    spec = SubmitS1Spec.model_construct(walk=_clean_walk(), run_preflight=False, resolve=object())
    sidecar_path = tmp_path / ".hpc" / "runs" / f"{_RUN_ID}.json"

    with mock.patch.object(blocks, "resolve_submit_inputs", return_value=_fake_rr(sidecar_path)):
        result = blocks.submit_s1(tmp_path, spec=spec)

    red = result.brief["reducibility"]
    assert red["checked"] is False
    assert "could not be checked" in red["reason"]
