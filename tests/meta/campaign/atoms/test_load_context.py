"""Tests for the ``load-context`` primitive.

``load-context`` reconstructs workflow context from on-disk state so a
fresh-context step (subagent / restarted session / cron tick) never has
to rely on conversational memory. These tests pin that contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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


def _write_sidecar(experiment: Path, run_id: str, **overrides) -> None:
    base = dict(
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


def _make_record(run_id: str, **overrides) -> RunRecord:
    base = {
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


def test_delegate_submit_is_agent_kind(journal_home, experiment):
    _onboard(experiment)
    delegate = load_context(experiment_dir=experiment)["delegate"]
    assert delegate["kind"] == "agent"
    assert delegate["step"] == "submit"
    assert delegate["run_id"] is None
    assert delegate["prompt"]


def test_delegate_agent_step_carries_a_spawn_request(journal_home, experiment):
    # An agent step is delegated through a pinned hpc_spawn request, not
    # a hand-written prompt — the orchestrator passes spawn_request to
    # Task and the spawn_guard hook renders it.
    _onboard(experiment)
    delegate = load_context(experiment_dir=experiment)["delegate"]
    spawn = delegate["spawn_request"]
    assert spawn["workflow"] == "submit"
    assert spawn["experiment_dir"] == delegate["experiment_dir"]
    assert isinstance(spawn["fields"], dict)
    # prompt is the rendered canonical text, the same SoT as the hook.
    assert "submit PROCEDURE" in delegate["prompt"]


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
    # A decide step delegates the hpc-campaign workflow, pinned.
    assert delegate["spawn_request"]["workflow"] == "campaign"
    assert "hpc-campaign" in delegate["prompt"]


def test_submit_hint_when_idle_and_no_campaign(journal_home, experiment):
    # An idle non-campaign run stays a cold submit, not decide.
    _onboard(experiment)
    _write_sidecar(experiment, "20260521-120000-aaa", campaign_id=None)
    ctx = load_context(experiment_dir=experiment)
    assert ctx["next_step_hint"] == "submit"
    assert ctx["delegate"]["step"] == "submit"
    assert ctx["delegate"]["campaign_id"] is None
