"""Tests for the campaign human-amplification blocks (design §4).

The campaign flow decomposed into its three §4 touchpoints as blocks:

* ``campaign-greenlight`` (start) — digest the greenlit-once spec; a confirm
  re-invocation stamps ``mark_greenlit`` + journals the decision; an
  already-greenlit manifest is an idempotent re-read; no manifest fails loudly.
* ``campaign-watch`` (async execution surface) — read-only; surfaces a healthy
  / anomaly / hand-off terminator (the anomaly brief rides through).
* ``campaign-complete`` (end) — spend-vs-budget + stop-reason + code-extracted
  outcome table + an EMPTY proposed_interpretations slot.

Fixtures mirror ``tests/meta/campaign/atoms/`` (write_run_sidecar + journal
RunRecords + write_manifest); the anomaly path reuses the circuit-breaker
seeding to drive ``campaign-advance`` to a loud-fail terminator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.campaign_blocks import (
    CampaignCompleteSpec,
    CampaignGreenlightSpec,
    CampaignWatchSpec,
)
from hpc_agent.meta.campaign.blocks import (
    campaign_complete,
    campaign_greenlight,
    campaign_watch,
)
from hpc_agent.meta.campaign.manifest import read_manifest, write_manifest
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def _journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the run-record journal to a tmp home (mirrors atoms tests)."""
    from hpc_agent.state import run_record

    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


def _seed_iteration(
    experiment_dir: Path,
    *,
    run_id: str,
    campaign_id: str,
    status: str,
) -> None:
    """Seed a sidecar + a journal RunRecord with an explicit terminal status."""
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="0" * 12,
        hpc_agent_version="0.0.0+test",
        submitted_at="2026-01-01T00:00:00Z",
        executor="hpc_user_tasks",
        result_dir_template="results/{run_id}/{task_id}",
        task_count=1,
        tasks_py_sha="0" * 12,
        campaign_id=campaign_id,
        profile="ml",
        cluster="hoffman2",
        remote_path="/u/scratch/exp",
    )
    upsert_run(
        experiment_dir,
        RunRecord(
            run_id=run_id,
            profile="ml",
            cluster="hoffman2",
            ssh_target="user@host",
            remote_path="/scratch/exp",
            job_name="ml",
            job_ids=["1"],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment_dir.resolve()),
            campaign_id=campaign_id,
            status=status,
        ),
    )


def _seed_metrics(experiment_dir: Path, *, run_id: str, value: float) -> None:
    """Drop a metrics.json under the result dir so prior() counts the iteration."""
    import json

    metrics_dir = experiment_dir / "results" / run_id / "0"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "metrics.json").write_text(json.dumps({"loss": value}))


# ─── greenlight ─────────────────────────────────────────────────────────────


def test_greenlight_digests_ungreenlit_spec(tmp_path: Path) -> None:
    """An un-greenlit manifest digests to a needs_greenlight brief; nothing is
    stamped, nothing is journaled."""
    write_manifest(
        tmp_path,
        campaign_id="camp",
        goal="tune lr",
        budget={"max_jobs": 10},
        strategy={"name": "tpe", "params": {"seed": 1}},
        stop_criteria={"max_iters": 20},
        anomaly_policy={"on_anomaly": "surface", "resubmit_cap": 3},
    )

    res = campaign_greenlight(tmp_path, spec=CampaignGreenlightSpec(campaign_id="camp"))

    assert res.block == "greenlight"
    assert res.stage_reached == "needs_greenlight"
    assert res.needs_decision is True
    assert res.brief["goal"] == "tune lr"
    assert res.brief["budget"] == {"max_jobs": 10}
    assert res.brief["strategy"]["name"] == "tpe"
    assert res.brief["anomaly_policy"]["on_anomaly"] == "surface"
    assert res.brief["greenlit"] is False

    # Nothing stamped: the on-disk manifest is still un-greenlit.
    manifest = read_manifest(tmp_path, "camp")
    assert manifest is not None
    assert "greenlit" not in manifest
    # Nothing journaled.
    assert read_decisions(tmp_path, "campaign", "camp") == []


def test_greenlight_confirm_stamps_and_journals(tmp_path: Path) -> None:
    """confirm-mode stamps the greenlit marker AND appends a decision record."""
    write_manifest(tmp_path, campaign_id="camp", goal="tune lr")

    res = campaign_greenlight(
        tmp_path,
        spec=CampaignGreenlightSpec(
            campaign_id="camp",
            confirm=True,
            response="y",
            proposal="greenlight the tpe sweep, budget 10 jobs",
        ),
    )

    assert res.stage_reached == "greenlit"
    assert res.needs_decision is False
    assert res.brief["greenlit"] is True
    assert res.brief["greenlit_at"] is not None

    # Marker persisted to the manifest.
    manifest = read_manifest(tmp_path, "camp")
    assert manifest is not None
    assert manifest["greenlit"] is True
    assert manifest["greenlit_at"] == res.brief["greenlit_at"]

    # Decision journaled (campaign scope).
    decisions = read_decisions(tmp_path, "campaign", "camp")
    assert len(decisions) == 1
    rec = decisions[0]
    assert rec["block"] == "campaign-greenlight"
    assert rec["response"] == "y"
    assert rec["resolved"]["greenlit"] is True
    assert rec["proposal"] == "greenlight the tpe sweep, budget 10 jobs"


def test_greenlight_confirm_without_journal(tmp_path: Path) -> None:
    """journal=False stamps the marker without a decision record (e.g. re-stamp)."""
    write_manifest(tmp_path, campaign_id="camp")
    res = campaign_greenlight(
        tmp_path,
        spec=CampaignGreenlightSpec(campaign_id="camp", confirm=True, journal=False),
    )
    assert res.stage_reached == "greenlit"
    assert read_manifest(tmp_path, "camp")["greenlit"] is True  # type: ignore[index]
    assert read_decisions(tmp_path, "campaign", "camp") == []


def test_greenlight_already_greenlit_is_idempotent(tmp_path: Path) -> None:
    """A second (non-confirm) read of an already-greenlit manifest needs no
    decision and stamps / journals nothing new."""
    write_manifest(tmp_path, campaign_id="camp", goal="tune")
    campaign_greenlight(tmp_path, spec=CampaignGreenlightSpec(campaign_id="camp", confirm=True))
    first = read_manifest(tmp_path, "camp")
    assert first is not None
    stamped_at = first["greenlit_at"]

    res = campaign_greenlight(tmp_path, spec=CampaignGreenlightSpec(campaign_id="camp"))

    assert res.stage_reached == "already_greenlit"
    assert res.needs_decision is False
    assert res.brief["greenlit"] is True
    # Idempotent: the timestamp is unchanged and no new journal line was added.
    assert read_manifest(tmp_path, "camp")["greenlit_at"] == stamped_at  # type: ignore[index]
    assert len(read_decisions(tmp_path, "campaign", "camp")) == 1


def test_greenlight_missing_manifest_fails_loudly(tmp_path: Path) -> None:
    """The marker rides the spec — greenlighting a campaign with no manifest is
    a loud SpecInvalid, not a silent no-op."""
    with pytest.raises(errors.SpecInvalid):
        campaign_greenlight(tmp_path, spec=CampaignGreenlightSpec(campaign_id="never_made"))


# ─── watch ──────────────────────────────────────────────────────────────────


def test_watch_healthy_no_boundary(_journal_home: Path, tmp_path: Path) -> None:
    """A nominal campaign (no stop criterion) is a healthy, no-decision watch."""
    write_manifest(tmp_path, campaign_id="A", goal="tune")
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="complete")

    res = campaign_watch(tmp_path, spec=CampaignWatchSpec(campaign_id="A"))

    assert res.block == "watch"
    assert res.stage_reached == "watching_healthy"
    assert res.needs_decision is False
    assert res.brief["decision"] == "continue"
    assert res.brief["anomaly_brief"] is None


def test_watch_surfaces_anomaly_brief(_journal_home: Path, tmp_path: Path) -> None:
    """A circuit-breaker trip is an anomaly terminator (needs_decision=True) that
    surfaces the drafted anomaly_brief."""
    write_manifest(
        tmp_path,
        campaign_id="A",
        anomaly_policy={"on_anomaly": "surface", "circuit_breaker_failures": 3},
    )
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r2", campaign_id="A", status="failed")

    res = campaign_watch(tmp_path, spec=CampaignWatchSpec(campaign_id="A"))

    assert res.stage_reached == "watching_anomaly"
    assert res.needs_decision is True
    brief = res.brief["anomaly_brief"]
    assert brief is not None
    assert brief["tripped"] == "circuit_breaker"
    assert brief["decision"] == "stop_circuit_breaker"
    assert brief["evidence"]["count"] == 3


def test_watch_converged_hands_off_to_complete(_journal_home: Path, tmp_path: Path) -> None:
    """A fired stop criterion (max_iters) hands off to campaign-complete without
    a watch-level decision."""
    write_manifest(tmp_path, campaign_id="A", stop_criteria={"max_iters": 2})
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="complete")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="complete")
    _seed_metrics(tmp_path, run_id="r0", value=0.5)
    _seed_metrics(tmp_path, run_id="r1", value=0.3)

    res = campaign_watch(tmp_path, spec=CampaignWatchSpec(campaign_id="A"))

    assert res.stage_reached == "watching_complete"
    assert res.needs_decision is False
    assert res.brief["decision"] == "stop_converged"


# ─── complete ───────────────────────────────────────────────────────────────


def test_complete_emits_brief_with_empty_interpretations(
    _journal_home: Path, tmp_path: Path
) -> None:
    """The completion brief carries spend / budget / stop reason / a per-iteration
    outcome table and an EMPTY proposed_interpretations slot."""
    write_manifest(
        tmp_path,
        campaign_id="A",
        goal="minimize loss",
        budget={"max_jobs": 10},
        stop_criteria={"max_iters": 2},
    )
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="complete")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="complete")
    _seed_metrics(tmp_path, run_id="r0", value=0.5)
    _seed_metrics(tmp_path, run_id="r1", value=0.3)

    res = campaign_complete(tmp_path, spec=CampaignCompleteSpec(campaign_id="A"))

    assert res.block == "complete"
    assert res.stage_reached == "complete"
    assert res.needs_decision is True
    assert res.brief["goal"] == "minimize loss"
    assert res.brief["iterations"] == 2
    assert res.brief["budget"]["max_jobs"] == 10
    assert "spent" not in res.brief  # spend is under "spend"
    assert res.brief["spend"]["jobs"] == 2
    assert res.brief["stop_reason"]["decision"] == "stop_converged"
    # Code extracts the outcomes; the interpretation slot is handed over EMPTY.
    assert res.brief["proposed_interpretations"] == []
    # Outcome table is a stable per-iteration projection.
    table = res.brief["outcome_table"]
    assert len(table) == 2
    assert {row["run_id"] for row in table} == {"r0", "r1"}
