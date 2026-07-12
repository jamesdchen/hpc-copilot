"""Tests for the ``load-context`` primitive.

``load-context`` reconstructs workflow context from on-disk state so a
fresh-context step (subagent / restarted session / cron tick) never has
to rely on conversational memory. These tests pin that contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.meta.campaign.atoms.load_context import load_context
from hpc_agent.meta.campaign.cursor import advance_cursor
from hpc_agent.state import run_record
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the journal home into a per-test tmp directory."""
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    """A throwaway experiment dir on disk."""
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _write_sidecar(experiment: Path, run_id: str, **overrides: Any) -> None:
    base: dict[str, Any] = dict(
        run_id=run_id,
        cmd_sha="a" * 64,
        hpc_agent_version="0.4.0",
        submitted_at="2026-05-21T12:00:00+00:00",
        executor="python3 exec.py",
        result_dir_template="results/{task_id}",
        task_count=12,
        tasks_py_sha="b" * 64,
        cluster="greene",
        profile="gpu-sweep",
        campaign_id="optuna-1",
        resources={"gpus": 1, "walltime": "04:00:00"},
        remote_path="/scratch/me/exp",
        job_ids=["9001"],
    )
    base.update(overrides)
    write_run_sidecar(experiment, **base)


def _onboard(experiment: Path) -> None:
    """Mark *experiment* as onboarded by creating ``.hpc/tasks.py``.

    ``load-context`` keys ``needs_onboarding`` on this file's presence
    (the dispatch contract a submit requires), mirroring the signal
    ``hpc-agent setup`` uses.
    """
    hpc = experiment / ".hpc"
    hpc.mkdir(parents=True, exist_ok=True)
    (hpc / "tasks.py").write_text("# dispatch contract\n", encoding="utf-8")


def _make_record(run_id: str, **overrides: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": run_id,
        "profile": "gpu-sweep",
        "cluster": "greene",
        "ssh_target": "me@greene.nyu.edu",
        "remote_path": "/scratch/me/exp",
        "job_name": "gpu-sweep",
        "job_ids": ["9001"],
        "total_tasks": 12,
        "submitted_at": "2026-05-21T12:00:00+00:00",
        "experiment_dir": "/tmp/exp",
    }
    base.update(overrides)
    return RunRecord(**base)


def test_empty_experiment_hints_onboard(journal_home, experiment):
    # A fresh repo with no .hpc/tasks.py is not onboarded — route to
    # wrap-entry-point, not submit (there is nothing to submit yet).
    ctx = load_context(experiment_dir=experiment)
    assert ctx["latest_run"] is None
    assert ctx["in_flight"] == []
    assert ctx["campaigns"] == []
    assert ctx["warnings"] == []
    assert ctx["needs_onboarding"] is True
    assert ctx["next_step_hint"] == "onboard"


def test_onboard_delegate_routes_to_wrap_entry_point(journal_home, experiment):
    delegate = load_context(experiment_dir=experiment)["delegate"]
    assert delegate["kind"] == "agent"
    assert delegate["step"] == "onboard"
    # Onboarding is not one of the spawn-contract workflows, so no
    # spawn_request — the prompt names wrap-entry-point as the remedy.
    assert delegate["spawn_request"] is None
    assert "wrap-entry-point" in delegate["prompt"]


def test_onboarded_no_runs_hints_submit(journal_home, experiment):
    # tasks.py present, no run history -> ready to submit.
    _onboard(experiment)
    ctx = load_context(experiment_dir=experiment)
    assert ctx["needs_onboarding"] is False
    assert ctx["next_step_hint"] == "submit"


def test_latest_run_surfaces_config_snapshot(journal_home, experiment):
    _write_sidecar(experiment, "20260521-120000-aaa")
    ctx = load_context(experiment_dir=experiment)
    latest = ctx["latest_run"]
    assert latest is not None
    # The values skills currently cache conversationally are all present.
    assert latest["run_id"] == "20260521-120000-aaa"
    assert latest["cluster"] == "greene"
    assert latest["profile"] == "gpu-sweep"
    assert latest["campaign_id"] == "optuna-1"
    assert latest["remote_path"] == "/scratch/me/exp"
    assert latest["resources"] == {"gpus": 1, "walltime": "04:00:00"}
    assert latest["job_ids"] == ["9001"]
    assert latest["is_orphan"] is False


def test_latest_run_is_newest_of_several(journal_home, experiment):
    _write_sidecar(experiment, "20260521-120000-old")
    _write_sidecar(experiment, "20260521-130000-new", profile="cpu-sweep")
    ctx = load_context(experiment_dir=experiment)
    assert ctx["latest_run"]["run_id"] == "20260521-130000-new"
    assert ctx["latest_run"]["profile"] == "cpu-sweep"


def test_orphan_sidecar_emits_warning(journal_home, experiment):
    # No job_ids and no journal record -> orphan.
    _write_sidecar(experiment, "20260521-120000-orph", job_ids=None)
    ctx = load_context(experiment_dir=experiment)
    assert ctx["latest_run"]["is_orphan"] is True
    assert any("orphan" in w for w in ctx["warnings"])


def test_in_flight_run_hints_monitor(journal_home, experiment):
    upsert_run(experiment, _make_record("20260521-120000-aaa", stage="monitor"))
    ctx = load_context(experiment_dir=experiment)
    assert len(ctx["in_flight"]) == 1
    row = ctx["in_flight"][0]
    assert row["run_id"] == "20260521-120000-aaa"
    assert row["cluster"] == "greene"
    assert row["ssh_target"] == "me@greene.nyu.edu"
    assert row["stage"] == "monitor"
    assert ctx["next_step_hint"] == "monitor"


def test_in_flight_past_monitor_hints_aggregate(journal_home, experiment):
    upsert_run(experiment, _make_record("20260521-120000-aaa", stage="aggregate"))
    ctx = load_context(experiment_dir=experiment)
    assert ctx["next_step_hint"] == "aggregate"


def test_campaign_cursor_surfaced(journal_home, experiment):
    _write_sidecar(experiment, "20260521-120000-aaa", campaign_id="optuna-1")
    advance_cursor(experiment, "optuna-1", last_run_id="20260521-120000-aaa")
    advance_cursor(experiment, "optuna-1", last_run_id="20260521-130000-bbb")
    ctx = load_context(experiment_dir=experiment)
    assert len(ctx["campaigns"]) == 1
    camp = ctx["campaigns"][0]
    assert camp["campaign_id"] == "optuna-1"
    assert camp["iterations_submitted"] == 1
    assert camp["cursor_iteration"] == 2
    assert camp["cursor_last_run_id"] == "20260521-130000-bbb"


def test_campaign_without_cursor_omits_cursor_fields(journal_home, experiment):
    _write_sidecar(experiment, "20260521-120000-aaa", campaign_id="optuna-1")
    ctx = load_context(experiment_dir=experiment)
    camp = ctx["campaigns"][0]
    assert camp["campaign_id"] == "optuna-1"
    assert "cursor_iteration" not in camp


@pytest.mark.parametrize(
    "cursor_doc",
    [
        # Newer schema than this framework reads.
        {"cursor_schema_version": 999, "iteration": 3},
        # Non-int schema version (manual JSON edit / corruption).
        {"cursor_schema_version": "one", "iteration": 3},
    ],
    ids=["newer-schema", "non-int-schema"],
)
def test_corrupt_cursor_downgrades_to_warning(journal_home, experiment, cursor_doc):
    """``read_cursor`` raises ``errors.JournalCorrupt`` for a bad
    ``cursor_schema_version``; one corrupt cursor.json must surface as the
    documented per-campaign warning, not crash load-context for the repo."""
    import json

    _write_sidecar(experiment, "20260521-120000-aaa", campaign_id="optuna-1")
    camp_dir = experiment / ".hpc" / "campaigns" / "optuna-1"
    camp_dir.mkdir(parents=True, exist_ok=True)
    (camp_dir / "cursor.json").write_text(json.dumps(cursor_doc), encoding="utf-8")

    ctx = load_context(experiment_dir=experiment)

    camp = ctx["campaigns"][0]
    assert camp["campaign_id"] == "optuna-1"
    assert "cursor_iteration" not in camp
    assert any("cursor unreadable" in w and "optuna-1" in w for w in ctx["warnings"])


def test_delegate_submit_is_agent_kind(journal_home, experiment):
    _onboard(experiment)
    delegate = load_context(experiment_dir=experiment)["delegate"]
    assert delegate["kind"] == "agent"
    assert delegate["step"] == "submit"
    assert delegate["run_id"] is None
    assert delegate["prompt"]


def test_delegate_agent_step_routes_to_block_drive(journal_home, experiment):
    # An agent step is a human decision boundary: no spawn_request (the
    # bare-worker transport was deleted in the §6 worker removal) — the
    # prompt routes the reader to the block-drive chain instead.
    _onboard(experiment)
    delegate = load_context(experiment_dir=experiment)["delegate"]
    assert delegate["spawn_request"] is None
    assert "submit-s1" in delegate["prompt"]
    assert "block-drive" in delegate["prompt"]


def test_delegate_monitor_is_cli_kind(journal_home, experiment):
    upsert_run(experiment, _make_record("20260521-120000-aaa", stage="monitor"))
    delegate = load_context(experiment_dir=experiment)["delegate"]
    assert delegate["kind"] == "cli"
    assert delegate["step"] == "monitor"
    assert delegate["run_id"] == "20260521-120000-aaa"
    # cli steps run directly — no subagent, so no spawn request.
    assert delegate["spawn_request"] is None


def test_delegate_aggregate_picks_non_monitor_run(journal_home, experiment):
    upsert_run(experiment, _make_record("20260521-120000-aaa", stage="aggregate"))
    delegate = load_context(experiment_dir=experiment)["delegate"]
    assert delegate["kind"] == "cli"
    assert delegate["step"] == "aggregate"
    assert delegate["run_id"] == "20260521-120000-aaa"


def test_decide_hint_when_campaign_idle(journal_home, experiment):
    # A campaign sidecar exists and nothing is in flight -> the next
    # step is to decide the campaign's next iteration, not a cold submit.
    _write_sidecar(experiment, "20260521-120000-aaa", campaign_id="optuna-1")
    ctx = load_context(experiment_dir=experiment)
    assert ctx["next_step_hint"] == "decide"
    delegate = ctx["delegate"]
    assert delegate["kind"] == "agent"
    assert delegate["step"] == "decide"
    assert delegate["campaign_id"] == "optuna-1"
    assert delegate["run_id"] is None
    # A decide step routes to the campaign block flow; no worker spawn.
    assert delegate["spawn_request"] is None
    assert "block-drive" in delegate["prompt"]


def test_submit_hint_when_idle_and_no_campaign(journal_home, experiment):
    # An idle non-campaign run stays a cold submit, not decide.
    _onboard(experiment)
    _write_sidecar(experiment, "20260521-120000-aaa", campaign_id=None)
    ctx = load_context(experiment_dir=experiment)
    assert ctx["next_step_hint"] == "submit"
    assert ctx["delegate"]["step"] == "submit"
    assert ctx["delegate"]["campaign_id"] is None


# ─── async-refill routing (#362, plan §1.3) ─────────────────────────────────


def _seed_in_flight_campaign_run(experiment: Path, run_id: str, cid: str) -> None:
    """A campaign run that is BOTH in flight (journal, monitor stage) and has a
    sidecar (so the campaign shows up in ``campaigns``)."""
    _write_sidecar(experiment, run_id, campaign_id=cid)
    upsert_run(experiment, _make_record(run_id, stage="monitor", campaign_id=cid))


def test_async_refill_decides_while_runs_in_flight(journal_home, experiment):
    """An async-refill campaign with a free slot routes a decide/refill step
    EVEN with a run still in flight — the load-bearing async change (#362)."""
    from hpc_agent.meta.campaign.manifest import write_manifest

    _seed_in_flight_campaign_run(experiment, "20260521-120000-aaa", "optuna-1")
    write_manifest(experiment, campaign_id="optuna-1", async_refill=True, max_in_flight=4)

    ctx = load_context(experiment_dir=experiment)
    # A run IS in flight (monitor stage), yet the hint is decide (refill).
    assert len(ctx["in_flight"]) == 1
    assert ctx["next_step_hint"] == "decide"
    delegate = ctx["delegate"]
    assert delegate["kind"] == "agent"
    assert delegate["step"] == "decide"
    assert delegate["campaign_id"] == "optuna-1"
    assert delegate["spawn_request"] is None
    assert "campaign" in delegate["prompt"]


def test_async_refill_greenlit_routes_cli_refill(journal_home, experiment):
    """RFC #362 (flag ON): async ON + manifest GREENLIT + advance decides refill →
    the delegate is a DETERMINISTIC ``kind="cli"`` campaign-refill step (no
    judgement), not the agent decide chain. Exercises the ``_refill_is_deterministic``
    True path + the ``kind="cli"`` refill arm of ``_build_delegate`` (the house rule
    that every new branch gets a test with the flag ON — the un-greenlit sibling
    above pins the ``kind="agent"`` fallback)."""
    from hpc_agent.meta.campaign.manifest import write_manifest

    _seed_in_flight_campaign_run(experiment, "20260521-120000-aaa", "optuna-1")
    write_manifest(
        experiment,
        campaign_id="optuna-1",
        async_refill=True,
        max_in_flight=4,
        greenlit=True,
        greenlit_at="2026-07-12T00:00:00Z",
    )

    ctx = load_context(experiment_dir=experiment)
    # 1 in flight < K=4, unbounded budget → real campaign-advance decides refill.
    assert ctx["next_step_hint"] == "decide"
    delegate = ctx["delegate"]
    assert delegate["kind"] == "cli"
    assert delegate["step"] == "refill"
    assert delegate["campaign_id"] == "optuna-1"
    # Refill is keyed on campaign_id, never a run_id (no per-run dispatch shape).
    assert delegate["run_id"] is None
    assert delegate["spawn_request"] is None
    assert "campaign-refill" in delegate["prompt"]


def test_async_off_still_monitors_in_flight(journal_home, experiment):
    """Default-off: the same in-flight campaign run routes monitor, not refill —
    synchronous routing is byte-identical when no async manifest is present."""
    _seed_in_flight_campaign_run(experiment, "20260521-120000-aaa", "optuna-1")
    # No manifest written → async opt-in absent → synchronous behavior.

    ctx = load_context(experiment_dir=experiment)
    assert ctx["next_step_hint"] == "monitor"


def test_async_pool_full_routes_monitor(journal_home, experiment):
    """At K in flight the async campaign has no free slot, so monitoring (drain)
    takes over instead of refilling."""
    from hpc_agent.meta.campaign.manifest import write_manifest

    _seed_in_flight_campaign_run(experiment, "20260521-120000-aaa", "optuna-1")
    _seed_in_flight_campaign_run(experiment, "20260521-130000-bbb", "optuna-1")
    write_manifest(experiment, campaign_id="optuna-1", async_refill=True, max_in_flight=2)

    ctx = load_context(experiment_dir=experiment)
    assert len(ctx["in_flight"]) == 2  # == K
    assert ctx["next_step_hint"] == "monitor"


def test_async_budget_halt_drains_in_flight_not_loop_on_decide(journal_home, experiment):
    """A budget-halted async campaign with a run in flight routes monitor (drain),
    NOT a no-op decide step.

    Free slot by K alone (in_flight 1 < K 4) used to route ``decide`` forever even
    though campaign-advance would only answer ``stop_over_budget`` / ``wait_in_flight``
    — the in-flight run never got monitored, so it never drained (a livelock). Routing
    now defers to advance: not ``refill`` → fall through to monitor/aggregate to drain.
    """
    from hpc_agent.meta.campaign.manifest import write_manifest

    _seed_in_flight_campaign_run(experiment, "20260521-120000-aaa", "optuna-1")
    # max_jobs=1 with the 1 in-flight sidecar already counted as spent → budget met.
    write_manifest(
        experiment,
        campaign_id="optuna-1",
        async_refill=True,
        max_in_flight=4,
        budget={"max_jobs": 1},
    )

    ctx = load_context(experiment_dir=experiment)
    assert len(ctx["in_flight"]) == 1  # below K, but advance won't refill
    assert ctx["next_step_hint"] == "monitor"  # drains, does not loop on decide
