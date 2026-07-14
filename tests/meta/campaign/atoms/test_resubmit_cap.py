"""Loop-safety: cap total per-task resubmit attempts across the campaign.

The within-run auto-retry cap (``DEFAULT_AUTO_RETRY_POLICY``) resets every
time the campaign submits a fresh run, so a task slot that needs a retry
each iteration burns resubmits without any one run hitting its cap. The
campaign-level cap sums ``RunRecord.retries[tid]["attempts"]`` per task
slot across the campaign's runs and ``campaign-advance`` emits
``stop_resubmit_cap`` when the worst slot meets the supplied threshold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent.meta.campaign.atoms.advance import campaign_advance
from hpc_agent.meta.campaign.atoms.resubmit_cap import max_task_resubmits
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


def _seed_iteration(
    experiment_dir: Path,
    *,
    run_id: str,
    campaign_id: str,
    status: str = "complete",
    retries: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Seed a sidecar + a journal RunRecord carrying a ``retries`` map."""
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
            retries=retries or {},
        ),
    )


# ─── helper: max_task_resubmits ─────────────────────────────────────────────


def test_sums_attempts_per_slot_across_runs(journal_home: Path, tmp_path: Path) -> None:
    # Slot "0" retried twice in run r0 and once in run r1 → campaign total 3.
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", retries={"0": {"attempts": 2}})
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", retries={"0": {"attempts": 1}})

    from hpc_agent.state.index import find_runs_by_campaign

    out = max_task_resubmits(find_runs_by_campaign(tmp_path, "A"))
    assert out["count"] == 3
    assert out["task_id"] == "0"
    assert out["per_task"] == {"0": 3}


def test_reports_worst_slot(journal_home: Path, tmp_path: Path) -> None:
    _seed_iteration(
        tmp_path,
        run_id="r0",
        campaign_id="A",
        retries={"0": {"attempts": 1}, "1": {"attempts": 4}},
    )
    from hpc_agent.state.index import find_runs_by_campaign

    out = max_task_resubmits(find_runs_by_campaign(tmp_path, "A"))
    assert out["count"] == 4
    assert out["task_id"] == "1"
    assert out["per_task"] == {"0": 1, "1": 4}


def test_no_resubmits_is_zero(journal_home: Path, tmp_path: Path) -> None:
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A")  # no retries
    from hpc_agent.state.index import find_runs_by_campaign

    out = max_task_resubmits(find_runs_by_campaign(tmp_path, "A"))
    assert out["count"] == 0
    assert out["task_id"] is None
    assert out["per_task"] == {}


# ─── end-to-end: campaign-advance stop_resubmit_cap ─────────────────────────


def test_advance_stops_on_resubmit_cap(journal_home: Path, tmp_path: Path) -> None:
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", retries={"0": {"attempts": 2}})
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", retries={"0": {"attempts": 1}})

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", max_task_resubmits=3)
    assert out["decision"] == "stop_resubmit_cap"
    assert out["resubmit_cap"]["count"] == 3
    assert "'0'" in out["reason"]


def test_advance_under_cap_continues(journal_home: Path, tmp_path: Path) -> None:
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", retries={"0": {"attempts": 2}})

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", max_task_resubmits=3)
    assert out["decision"] == "continue"
    assert out["resubmit_cap"]["count"] == 2


def test_advance_default_cap_fires_when_silent(journal_home: Path, tmp_path: Path) -> None:
    """Loud-fail DEFAULT (design §5): with no CLI arg and a silent manifest the
    resubmit backstop still fires at the framework default (2) — it is on by
    default, not opt-in."""
    from hpc_agent.meta.campaign.atoms.resubmit_cap import DEFAULT_MAX_TASK_RESUBMITS

    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", retries={"0": {"attempts": 2}})

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A")
    assert out["decision"] == "stop_resubmit_cap"
    assert out["resubmit_cap"]["threshold"] == DEFAULT_MAX_TASK_RESUBMITS == 2


def test_advance_below_default_cap_continues(journal_home: Path, tmp_path: Path) -> None:
    """One resubmit is under the default backstop (2) → continue, not a halt."""
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", retries={"0": {"attempts": 1}})

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A")
    assert out["decision"] == "continue"
    assert out["resubmit_cap"]["threshold"] == 2
    assert out["anomaly_brief"] is None


def test_advance_resubmit_cap_defaults_from_anomaly_policy(
    journal_home: Path, tmp_path: Path
) -> None:
    """``anomaly_policy.resubmit_cap`` is a fallback default source (after an
    explicit arg / ``stop_criteria`` and before the framework backstop)."""
    from hpc_agent.meta.campaign.manifest import write_manifest

    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", retries={"0": {"attempts": 4}})
    # Raise the cap above the framework default via the anomaly policy → 4 < 5.
    write_manifest(tmp_path, campaign_id="A", anomaly_policy={"resubmit_cap": 5})
    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A")
    assert out["decision"] == "continue"
    assert out["resubmit_cap"]["threshold"] == 5


def test_advance_emits_resubmit_anomaly_brief(journal_home: Path, tmp_path: Path) -> None:
    """A resubmit-cap trip emits a structured anomaly brief: what tripped,
    evidence counts, a drafted recommendation, and the surface/park action."""
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", retries={"0": {"attempts": 3}})

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", max_task_resubmits=3)
    brief = out["anomaly_brief"]
    assert brief is not None
    assert brief["tripped"] == "resubmit_cap"
    assert brief["decision"] == "stop_resubmit_cap"
    assert brief["evidence"]["count"] == 3
    assert brief["evidence"]["threshold"] == 3
    assert brief["evidence"]["task_id"] == "0"
    # Default policy is "surface": recommend a y/nudge decision, never autonomy.
    assert brief["on_anomaly"] == "surface"
    assert brief["recommended_action"] == "surface_for_decision"
    assert "'0'" in brief["recommendation"]


def test_advance_park_policy_shapes_brief_recommendation(
    journal_home: Path, tmp_path: Path
) -> None:
    """``anomaly_policy.on_anomaly='park'`` shapes the brief's recommendation to
    a park (data only) WITHOUT changing the decision."""
    from hpc_agent.meta.campaign.manifest import write_manifest

    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", retries={"0": {"attempts": 2}})
    write_manifest(tmp_path, campaign_id="A", anomaly_policy={"on_anomaly": "park"})

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A")
    assert out["decision"] == "stop_resubmit_cap"  # unchanged by the policy
    brief = out["anomaly_brief"]
    assert brief["on_anomaly"] == "park"
    assert brief["recommended_action"] == "park_campaign"
    assert "park" in brief["recommendation"]


def test_advance_resubmit_cap_defaults_from_manifest(journal_home: Path, tmp_path: Path) -> None:
    from hpc_agent.meta.campaign.manifest import write_manifest

    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", retries={"0": {"attempts": 2}})
    write_manifest(tmp_path, campaign_id="A", stop_criteria={"max_task_resubmits": 2})
    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A")
    assert out["decision"] == "stop_resubmit_cap"


def test_advance_in_flight_takes_precedence_over_resubmit_cap(
    journal_home: Path, tmp_path: Path
) -> None:
    # An in-flight retry must get the chance to succeed before the cap halts.
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", retries={"0": {"attempts": 3}})
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="in_flight")

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", max_task_resubmits=3)
    assert out["decision"] == "wait_in_flight"


def test_init_persists_max_task_resubmits(tmp_path: Path) -> None:
    from hpc_agent.meta.campaign.atoms.init import campaign_init
    from hpc_agent.meta.campaign.manifest import read_manifest

    campaign_init(experiment_dir=tmp_path, campaign_id="camp_z", max_task_resubmits=5)
    manifest = read_manifest(tmp_path, "camp_z")
    assert manifest is not None
    assert manifest["stop_criteria"]["max_task_resubmits"] == 5
