"""Tests for the aggregate-check / aggregate-run human-amplification block verbs
(docs/design/human-amplification-blocks.md §3 — the finer grain of submit's S4).

Cluster-free: the composed rings (aggregate-preflight / verify-aggregation-
complete / aggregate-flow) are mocked at the ``blocks`` module boundary, so these
assert the block orchestration + brief digestion, never SSH or a scheduler. The
journal seams reuse the fixtures the aggregate precondition tests use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

import hpc_agent.ops.aggregate_blocks as blocks
from hpc_agent import errors
from hpc_agent._wire.workflows.aggregate_blocks import AggregateCheckSpec, AggregateRunSpec
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.state import run_record
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from tests.ops._block_fixtures import greenlight

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260703-120000-agg"


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _record(**overrides) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "p",
        "job_ids": ["9001"],
        "total_tasks": 4,
        "submitted_at": "2026-07-03T12:00:00+00:00",
        "experiment_dir": "/tmp/exp",
    }
    base.update(overrides)
    return RunRecord(**base)


def _greenlight(experiment_dir: Path, verb: str, *, run_id: str = _RUN_ID) -> None:
    greenlight(experiment_dir, verb, run_id=run_id)


def _clean_vac() -> dict:
    """A verify-aggregation-complete report with every invariant passing."""
    return {
        "ok": True,
        "all_waves_combined": True,
        "missing_waves": [],
        "all_tasks_present": True,
        "missing_tasks": [],
        "unexpected_tasks": [],
        "unexpected_aggregated_keys": [],
        "provenance_present": True,
        "columns_checked": False,
        "column_violations": [],
    }


# ── aggregate-check: readiness gate ───────────────────────────────────────────


def test_check_not_ready_when_non_terminal(journal_home, experiment) -> None:
    upsert_run(experiment, _record(status="in_flight"))
    spec = AggregateCheckSpec(run_id=_RUN_ID, run_preflight=False)

    result = blocks.aggregate_check(experiment, spec=spec)

    assert result.block == "check"
    assert result.stage_reached == "not_ready"
    assert result.needs_decision is True
    assert result.brief["terminal"] is False
    assert result.brief["status"] == "in_flight"


def test_check_not_ready_when_no_record(journal_home, experiment) -> None:
    spec = AggregateCheckSpec(run_id=_RUN_ID, run_preflight=False)

    result = blocks.aggregate_check(experiment, spec=spec)

    assert result.stage_reached == "not_ready"
    assert result.needs_decision is True
    assert result.brief["record_found"] is False


def test_check_not_ready_when_preflight_fails(journal_home, experiment) -> None:
    upsert_run(experiment, _record(status="complete"))
    spec = AggregateCheckSpec(run_id=_RUN_ID, run_preflight=True)

    with (
        mock.patch.object(blocks, "aggregate_preflight", return_value={"overall": "fail"}),
        mock.patch.object(blocks, "verify_aggregation_complete", return_value=_clean_vac()),
    ):
        result = blocks.aggregate_check(experiment, spec=spec)

    assert result.stage_reached == "not_ready"
    assert result.needs_decision is True
    assert result.brief["preflight"] == {"overall": "fail"}


# ── aggregate-check: integrity gate (never auto-masked) ───────────────────────


def test_check_surfaces_integrity_issues_never_masked(journal_home, experiment) -> None:
    """The load-bearing aggregate-check invariant (§2): a verify-aggregation-
    complete violation is surfaced as a decision point with a recommendation and
    is NEVER auto-masked."""
    upsert_run(experiment, _record(status="complete"))
    vac = _clean_vac()
    vac.update({"ok": False, "missing_tasks": [3], "unexpected_tasks": [99]})
    spec = AggregateCheckSpec(run_id=_RUN_ID, run_preflight=False)

    with mock.patch.object(blocks, "verify_aggregation_complete", return_value=vac):
        result = blocks.aggregate_check(experiment, spec=spec)

    assert result.stage_reached == "integrity_review"
    assert result.needs_decision is True
    assert result.brief["integrity_checked"] is True
    issues = {i["issue"]: i for i in result.brief["integrity_issues"]}
    assert "missing_tasks" in issues and "unexpected_tasks" in issues
    # Every issue carries a recommendation and is NEVER auto-masked.
    for issue in issues.values():
        assert issue["auto_masked"] is False
        assert issue["recommendation"]


def test_check_integrity_unavailable_before_pull_is_not_a_failure(journal_home, experiment) -> None:
    """A pre-run check where nothing is pulled yet: verify-aggregation-complete
    raises SpecInvalid (no local combiner). That is NOT an integrity failure —
    integrity_checked=False and the run is otherwise ready."""
    upsert_run(experiment, _record(status="complete"))
    spec = AggregateCheckSpec(run_id=_RUN_ID, run_preflight=False)

    with mock.patch.object(
        blocks,
        "verify_aggregation_complete",
        side_effect=errors.SpecInvalid("combiner_dir_local is not a directory"),
    ):
        result = blocks.aggregate_check(experiment, spec=spec)

    assert result.stage_reached == "ready"
    assert result.needs_decision is False
    assert result.brief["integrity_checked"] is False
    assert result.brief["integrity_issues"] == []


def test_check_ready_when_terminal_and_clean(journal_home, experiment) -> None:
    upsert_run(experiment, _record(status="complete"))
    spec = AggregateCheckSpec(run_id=_RUN_ID, run_preflight=False)

    with mock.patch.object(blocks, "verify_aggregation_complete", return_value=_clean_vac()):
        result = blocks.aggregate_check(experiment, spec=spec)

    assert result.stage_reached == "ready"
    assert result.needs_decision is False


def test_check_missing_waves_blocks_by_default_but_not_under_allow_partial(
    journal_home, experiment
) -> None:
    """missing_waves is the one integrity issue whose blocking-ness bends to the
    operator's allow_partial stance — but it is surfaced (never auto-masked)
    either way."""
    upsert_run(experiment, _record(status="complete"))
    vac = _clean_vac()
    vac.update({"ok": False, "all_waves_combined": False, "missing_waves": [2, 5]})

    # Default (allow_partial=False): missing_waves blocks → integrity_review.
    with mock.patch.object(blocks, "verify_aggregation_complete", return_value=vac):
        blocked = blocks.aggregate_check(
            experiment, spec=AggregateCheckSpec(run_id=_RUN_ID, run_preflight=False)
        )
    assert blocked.stage_reached == "integrity_review"
    assert blocked.needs_decision is True

    # allow_partial=True: missing_waves is still surfaced but no longer blocks.
    with mock.patch.object(blocks, "verify_aggregation_complete", return_value=vac):
        ok = blocks.aggregate_check(
            experiment,
            spec=AggregateCheckSpec(run_id=_RUN_ID, run_preflight=False, allow_partial=True),
        )
    assert ok.stage_reached == "ready"
    assert ok.needs_decision is False
    # Surfaced regardless of the stance.
    surfaced = {i["issue"] for i in ok.brief["integrity_issues"]}
    assert "missing_waves" in surfaced


# ── aggregate-run: results table + empty interpretation slot ──────────────────


def _agg_result(*, escalation_reason: str | None = None, failed_waves=None, scope_looks=None):
    from hpc_agent.ops.aggregate_flow import AggregateFlowResult

    return AggregateFlowResult(
        run_id=_RUN_ID,
        combined_waves=[0, 1],
        failed_waves=failed_waves or [],
        waves_combined_this_call=[0, 1],
        combiner_dir_local="/tmp/agg/_combiner",
        aggregated_metrics={"ridge_h5": {"rmse": 0.12}, "ridge_h1": {"rmse": 0.20}},
        escalation_reason=escalation_reason,
        scope_looks=scope_looks,
    )


def test_run_returns_results_table_with_empty_interpretation_slot(experiment) -> None:
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
    _greenlight(experiment, "aggregate-run")

    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result()) as m_agg:
        result = blocks.aggregate_run(experiment, spec=spec)

    m_agg.assert_called_once()
    assert result.block == "run"
    assert result.stage_reached == "harvested"
    assert result.needs_decision is True
    # Code-extracted results table (row per grid key, sorted).
    table = result.brief["results_table"]
    assert [r["key"] for r in table] == ["ridge_h1", "ridge_h5"]
    assert table[1]["metrics"] == {"rmse": 0.12}
    # The interpretation slot is handed over EMPTY — the human concludes (§2).
    assert result.brief["proposed_interpretations"] == []
    # Deterministic error-sweep summary is present.
    assert result.brief["error_sweep"]["escalation_reason"] is None


def test_run_brief_carries_scope_looks_verbatim(experiment) -> None:
    """A reduction over a scoped run copies the aggregate-flow result's
    ``scope_looks`` onto the brief VERBATIM — two plain integers per tag, the
    block interprets nothing (rigor-primitives T3)."""
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
    _greenlight(experiment, "aggregate-run")
    looks = {"held_out": {"prior_looks": 2, "distinct_lineages": 1}}

    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result(scope_looks=looks)):
        result = blocks.aggregate_run(experiment, spec=spec)

    assert result.brief["scope_looks"] == looks


def test_run_brief_omits_scope_looks_key_for_scope_less_run(experiment) -> None:
    """A scope-less reduction (``scope_looks is None``) leaves the key ABSENT —
    not ``None`` — so a scope-less brief is byte-identical to a pre-T3 one."""
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
    _greenlight(experiment, "aggregate-run")

    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result(scope_looks=None)):
        result = blocks.aggregate_run(experiment, spec=spec)

    assert "scope_looks" not in result.brief


def test_run_partial_harvest_when_waves_escalate(experiment) -> None:
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
    _greenlight(experiment, "aggregate-run")

    with mock.patch.object(
        blocks,
        "aggregate_flow",
        return_value=_agg_result(escalation_reason="combiner_failed_max_retries:waves=3"),
    ):
        result = blocks.aggregate_run(experiment, spec=spec)

    assert result.stage_reached == "harvest_partial"
    assert result.needs_decision is True
    assert result.brief["error_sweep"]["escalation_reason"].startswith("combiner_failed")


def test_run_partial_harvest_when_failed_waves(experiment) -> None:
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
    _greenlight(experiment, "aggregate-run")

    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result(failed_waves=[3])):
        result = blocks.aggregate_run(experiment, spec=spec)

    assert result.stage_reached == "harvest_partial"
    assert result.needs_decision is True


def test_run_surfaces_harvest_ledger_tail(journal_home, experiment) -> None:
    """aggregate-run references (never writes) the guaranteed-harvest ledger and
    surfaces its last marker as corroborating evidence in the brief."""
    from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path

    ledger = harvest_marker_path(experiment, _RUN_ID)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text('{"terminal_cause": "complete", "harvest_ok": true}\n', encoding="utf-8")
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
    _greenlight(experiment, "aggregate-run")

    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result()):
        result = blocks.aggregate_run(experiment, spec=spec)

    assert result.brief["harvest_ledger"] == {
        "terminal_cause": "complete",
        "harvest_ok": True,
    }


# ── registry metadata ─────────────────────────────────────────────────────────


def test_blocks_are_agent_facing_workflows() -> None:
    from hpc_agent._kernel.registry.primitive import get_meta, register_primitives

    register_primitives()
    for name in ("aggregate-check", "aggregate-run"):
        meta = get_meta(name)
        assert meta.verb == "workflow"
        assert meta.agent_facing is True
        assert meta.cli is not None
        assert meta.cli.spec_arg is True
