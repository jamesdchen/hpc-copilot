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
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from tests.ops._block_fixtures import greenlight

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260703-120000-agg"


# ── fixtures ──────────────────────────────────────────────────────────────────


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


# ── aggregate-check: reducibility readiness (run #12 finding 28) ──────────────


def _write_sidecar(experiment_dir: Path, **overrides: Any) -> None:
    """Write a per-run sidecar for the reducibility readiness gate.

    ``summary_artifact`` / ``aggregate_defaults`` are the two records the gate
    reads (mirrors the run path's ``_combiner_only_reduce`` decision)."""
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


def test_check_surfaces_non_reducible_csv_artifact_before_greenlight(
    journal_home, experiment
) -> None:
    """Finding 28: a terminal run with NO aggregate_cmd and a non-JSON summary
    artifact can NEVER reduce through the built-in per-task fallback — surfaced at
    CHECK time as a never-auto-masked blocking issue, before the 40+ min pull."""
    upsert_run(experiment, _record(status="complete"))
    _write_sidecar(experiment, summary_artifact="causal_tune_linear/metrics_table.csv")
    spec = AggregateCheckSpec(run_id=_RUN_ID, run_preflight=False)

    # Integrity unavailable (no combiner pulled yet) — the common pre-run shape.
    with mock.patch.object(
        blocks,
        "verify_aggregation_complete",
        side_effect=errors.SpecInvalid("combiner_dir_local is not a directory"),
    ):
        result = blocks.aggregate_check(experiment, spec=spec)

    assert result.stage_reached == "integrity_review"
    assert result.needs_decision is True
    issues = {i["issue"]: i for i in result.brief["integrity_issues"]}
    assert "non_reducible_summary_artifact" in issues
    issue = issues["non_reducible_summary_artifact"]
    assert issue["auto_masked"] is False
    assert issue["detail"]["summary_artifact"] == "causal_tune_linear/metrics_table.csv"
    assert "aggregate_cmd" in issue["recommendation"]


def test_check_no_reducibility_issue_when_aggregate_cmd_present(journal_home, experiment) -> None:
    """An aggregate_cmd routes to the custom reducer, never the per-task fallback —
    so a non-JSON artifact is NOT a reducibility problem. No issue, stays ready."""
    upsert_run(experiment, _record(status="complete"))
    _write_sidecar(
        experiment,
        summary_artifact="causal_tune_linear/metrics_table.csv",
        aggregate_defaults={"aggregate_cmd": "python reduce.py"},
    )
    spec = AggregateCheckSpec(run_id=_RUN_ID, run_preflight=False)

    with mock.patch.object(
        blocks,
        "verify_aggregation_complete",
        side_effect=errors.SpecInvalid("combiner_dir_local is not a directory"),
    ):
        result = blocks.aggregate_check(experiment, spec=spec)

    assert result.stage_reached == "ready"
    assert result.needs_decision is False
    surfaced = {i["issue"] for i in result.brief["integrity_issues"]}
    assert "non_reducible_summary_artifact" not in surfaced


def test_check_no_reducibility_issue_for_json_artifact(journal_home, experiment) -> None:
    """A JSON summary artifact reduces cleanly through the per-task fallback — no
    reducibility issue even with no aggregate_cmd."""
    upsert_run(experiment, _record(status="complete"))
    _write_sidecar(experiment, summary_artifact="metrics.json")
    spec = AggregateCheckSpec(run_id=_RUN_ID, run_preflight=False)

    with mock.patch.object(
        blocks,
        "verify_aggregation_complete",
        side_effect=errors.SpecInvalid("combiner_dir_local is not a directory"),
    ):
        result = blocks.aggregate_check(experiment, spec=spec)

    assert result.stage_reached == "ready"
    assert result.needs_decision is False
    surfaced = {i["issue"] for i in result.brief["integrity_issues"]}
    assert "non_reducible_summary_artifact" not in surfaced


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
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)
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
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)
    _greenlight(experiment, "aggregate-run")
    looks = {"held_out": {"prior_looks": 2, "distinct_lineages": 1}}

    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result(scope_looks=looks)):
        result = blocks.aggregate_run(experiment, spec=spec)

    assert result.brief["scope_looks"] == looks


def test_run_brief_omits_scope_looks_key_for_scope_less_run(experiment) -> None:
    """A scope-less reduction (``scope_looks is None``) leaves the key ABSENT —
    not ``None`` — so a scope-less brief is byte-identical to a pre-T3 one."""
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)
    _greenlight(experiment, "aggregate-run")

    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result(scope_looks=None)):
        result = blocks.aggregate_run(experiment, spec=spec)

    assert "scope_looks" not in result.brief


def test_run_partial_harvest_when_waves_escalate(experiment) -> None:
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)
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
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)
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
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)
    _greenlight(experiment, "aggregate-run")

    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result()):
        result = blocks.aggregate_run(experiment, spec=spec)

    assert result.brief["harvest_ledger"] == {
        "terminal_cause": "complete",
        "harvest_ok": True,
    }


def test_harvest_ledger_tail_falls_back_over_torn_final_line(journal_home, experiment) -> None:
    """A crash mid-append can leave a torn final line; the reader scans BACKWARD
    for the newest PARSEABLE marker so a finished run's evidence is not stranded.

    B2: the whole-line-atomic append seam keeps every prior line intact, so a
    valid marker followed by a torn half-line still surfaces the valid marker
    (not None) — the ledger corroboration reaches the brief.
    """
    from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path

    ledger = harvest_marker_path(experiment, _RUN_ID)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    # A whole valid marker, then a torn final line (a crash mid-write).
    ledger.write_text(
        '{"terminal_cause": "complete", "harvest_ok": true}\n'
        '{"terminal_cause": "timeout", "harvest_ok": fal',
        encoding="utf-8",
    )
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)
    _greenlight(experiment, "aggregate-run")

    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result()):
        result = blocks.aggregate_run(experiment, spec=spec)

    assert result.brief["harvest_ledger"] == {
        "terminal_cause": "complete",
        "harvest_ok": True,
    }


def test_harvest_ledger_tail_none_only_when_entirely_unparseable(journal_home, experiment) -> None:
    """Only a ledger with no parseable line at all yields None (not a torn tail)."""
    from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path

    ledger = harvest_marker_path(experiment, _RUN_ID)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("not json\n{also bad\n", encoding="utf-8")
    spec = AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False)
    _greenlight(experiment, "aggregate-run")

    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result()):
        result = blocks.aggregate_run(experiment, spec=spec)

    assert result.brief["harvest_ledger"] is None


# ── aggregate-run detach-by-contract (design §3; run-#10 F-K) ──────────────────

_LAUNCH_PATH = "hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached"


class _FakeLaunch:
    run_id = _RUN_ID
    pid = 4242
    log_path = "/x/detached.log"


def test_run_detaches_by_default_after_gate(experiment) -> None:
    """detach ON (default): the harvest never runs in-process — a durable worker
    is spawned and the handle envelope returned. The child spec carries detach OFF."""
    _greenlight(experiment, "aggregate-run")
    with (
        mock.patch(_LAUNCH_PATH, return_value=_FakeLaunch()) as m_launch,
        mock.patch.object(blocks, "aggregate_flow") as m_agg,
    ):
        result = blocks.aggregate_run(
            experiment, spec=AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
        )

    m_agg.assert_not_called()
    m_launch.assert_called_once()
    assert m_launch.call_args.kwargs["verb"] == "aggregate-run"
    assert m_launch.call_args.kwargs["spec"]["detach"] is False
    assert result.stage_reached == "detached"
    assert result.started is True
    assert result.watch == "journal"
    assert result.detached_pid == 4242
    assert result.needs_decision is False
    assert result.next_block is None


def test_run_gate_fires_before_detach(experiment) -> None:
    """Ordering proof: no greenlight → the gate raises SYNCHRONOUSLY and the
    detached launcher is NEVER reached (a gate failure can never hide in a child)."""
    with (
        mock.patch(_LAUNCH_PATH) as m_launch,
        pytest.raises(errors.SpecInvalid, match="no journaled greenlight"),
    ):
        blocks.aggregate_run(
            experiment, spec=AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
        )
    m_launch.assert_not_called()


def test_run_locked_scope_refuses_before_detach(experiment) -> None:
    """Ordering proof (rigor-primitives T3): a LOCKED evidence-scope refuses
    SYNCHRONOUSLY in the parent — the detached launcher is NEVER reached."""
    from hpc_agent.state.runs import write_run_sidecar
    from hpc_agent.state.scopes import record_lock

    _greenlight(experiment, "aggregate-run")
    write_run_sidecar(
        experiment,
        run_id=_RUN_ID,
        cmd_sha="deadbeef",
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{task_id}",
        task_count=4,
        tasks_py_sha="",
        scopes=["holdout"],
    )
    record_lock(experiment, "holdout", reason="reserve this look")

    with (
        mock.patch(_LAUNCH_PATH) as m_launch,
        pytest.raises(errors.ScopeLocked, match="holdout"),
    ):
        blocks.aggregate_run(
            experiment, spec=AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
        )
    m_launch.assert_not_called()


def test_run_replays_recorded_terminal_without_respawn(experiment) -> None:
    """Run #7 idempotent re-invoke: after the detached worker (a synchronous run)
    records its terminal for the current tree, a re-invoke with detach ON REPLAYS
    that brief — no second spawn, no re-combine."""
    from tests.ops._block_fixtures import sidecar

    _greenlight(experiment, "aggregate-run")
    sidecar(experiment, cmd_sha="deadbeef", run_id=_RUN_ID)

    # 1. The detached CHILD runs synchronously and records its terminal.
    with mock.patch.object(blocks, "aggregate_flow", return_value=_agg_result()):
        sync = blocks.aggregate_run(
            experiment,
            spec=AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID), detach=False),
        )
    assert sync.stage_reached == "harvested"

    # 2. The parent's re-invoke (detach ON) replays it — launcher never called.
    with (
        mock.patch(_LAUNCH_PATH) as m_launch,
        mock.patch.object(blocks, "aggregate_flow") as m_agg,
    ):
        replay = blocks.aggregate_run(
            experiment, spec=AggregateRunSpec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
        )

    m_launch.assert_not_called()
    m_agg.assert_not_called()
    assert replay.stage_reached == "harvested"
    assert replay.brief["results_table"] == sync.brief["results_table"]


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
