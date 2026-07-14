"""Budget halt acknowledgement (#224): ``stop_over_budget`` is a halt the
loop cannot silently pass.

Once realised spend meets a cap, ``campaign-advance`` keeps returning
``stop_over_budget`` (with ``needs_acknowledgement``) until the spend is
explicitly acknowledged via ``campaign-acknowledge-budget``. A bare ack
snapshots spend and authorises one more leg; the next task that burns
compute re-arms the halt. Raising a cap in the same gesture buys real
headroom.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.meta.campaign.atoms.acknowledge_budget import campaign_acknowledge_budget
from hpc_agent.meta.campaign.atoms.advance import campaign_advance
from hpc_agent.meta.campaign.budget_ack import ack_covers_spend, read_budget_ack
from hpc_agent.meta.campaign.manifest import read_manifest, write_manifest
from hpc_agent.state.runs import write_run_sidecar
from hpc_agent.state.runtime_prior import append_sample

if TYPE_CHECKING:
    from pathlib import Path

_PROFILE = "ml"
_CLUSTER = "hoffman2"


def _seed_run(experiment_dir: Path, *, run_id: str) -> None:
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
        campaign_id="camp_a",
        profile=_PROFILE,
        cluster=_CLUSTER,
        remote_path="/u/scratch/exp",
    )


def _seed_spend(experiment_dir: Path, *, run_id: str, elapsed_sec: int) -> None:
    _seed_run(experiment_dir, run_id=run_id)
    append_sample(
        experiment_dir,
        profile=_PROFILE,
        cluster=_CLUSTER,
        run_id=run_id,
        task_id=0,
        gpu_type="",
        node="d11-07",
        elapsed_sec=elapsed_sec,
        exit_code=0,
    )


# ─── helper: ack_covers_spend ───────────────────────────────────────────────


def test_ack_covers_equal_spend() -> None:
    ack = {"acknowledged_spend": {"walltime_sec": 500, "core_hours": 2.0, "jobs": 1, "tasks": 1}}
    assert ack_covers_spend(ack, {"walltime_sec": 500, "core_hours": 2.0, "jobs": 1, "tasks": 1})


def test_ack_stale_when_spend_grows() -> None:
    ack = {"acknowledged_spend": {"walltime_sec": 500, "core_hours": 2.0, "jobs": 1, "tasks": 1}}
    grown = {"walltime_sec": 501, "core_hours": 2.0, "jobs": 1, "tasks": 1}
    assert not ack_covers_spend(ack, grown)


def test_ack_without_snapshot_covers_nothing() -> None:
    assert not ack_covers_spend({}, {"walltime_sec": 0})


# ─── end-to-end through campaign-advance ────────────────────────────────────


def test_advance_halts_unacknowledged(journal_home: Path, tmp_path: Path) -> None:
    _seed_spend(tmp_path, run_id="run_0000", elapsed_sec=500)
    write_manifest(tmp_path, campaign_id="camp_a", budget={"max_walltime_sec": 400})

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="camp_a")
    assert out["decision"] == "stop_over_budget"
    assert out["needs_acknowledgement"] is True


def test_acknowledge_then_continue(journal_home: Path, tmp_path: Path) -> None:
    _seed_spend(tmp_path, run_id="run_0000", elapsed_sec=500)
    write_manifest(tmp_path, campaign_id="camp_a", budget={"max_walltime_sec": 400})

    ack = campaign_acknowledge_budget(experiment_dir=tmp_path, campaign_id="camp_a")
    assert ack["was_over_budget"] is True
    assert ack["acknowledged_spend"]["walltime_sec"] == 500
    assert read_budget_ack(tmp_path, "camp_a") is not None

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="camp_a")
    assert out["decision"] == "continue"
    assert out["needs_acknowledgement"] is False


def test_ack_goes_stale_when_more_spend_lands(journal_home: Path, tmp_path: Path) -> None:
    _seed_spend(tmp_path, run_id="run_0000", elapsed_sec=500)
    write_manifest(tmp_path, campaign_id="camp_a", budget={"max_walltime_sec": 400})
    campaign_acknowledge_budget(experiment_dir=tmp_path, campaign_id="camp_a")

    # One more task burns compute → spend grows past the snapshot → re-armed.
    _seed_spend(tmp_path, run_id="run_0001", elapsed_sec=100)
    out = campaign_advance(experiment_dir=tmp_path, campaign_id="camp_a")
    assert out["decision"] == "stop_over_budget"
    assert out["needs_acknowledgement"] is True
    assert "stale" in out["reason"]


def test_acknowledge_raising_cap_clears_halt(journal_home: Path, tmp_path: Path) -> None:
    _seed_spend(tmp_path, run_id="run_0000", elapsed_sec=500)
    write_manifest(tmp_path, campaign_id="camp_a", budget={"max_walltime_sec": 400})

    ack = campaign_acknowledge_budget(
        experiment_dir=tmp_path, campaign_id="camp_a", max_walltime_sec=10_000
    )
    # Cap raised above current spend → no longer over budget at all.
    assert ack["was_over_budget"] is False
    assert ack["raised_caps"] == {"max_walltime_sec": 10_000}

    # The raised cap is durable in the manifest.
    manifest = read_manifest(tmp_path, "camp_a")
    assert manifest is not None
    assert manifest["budget"]["max_walltime_sec"] == 10_000

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="camp_a")
    assert out["decision"] == "continue"


def test_raising_cap_preserves_other_manifest_sections(journal_home: Path, tmp_path: Path) -> None:
    _seed_spend(tmp_path, run_id="run_0000", elapsed_sec=500)
    write_manifest(
        tmp_path,
        campaign_id="camp_a",
        goal="tune ridge",
        budget={"max_walltime_sec": 400, "max_jobs": 50},
        stop_criteria={"max_iters": 20},
    )
    campaign_acknowledge_budget(
        experiment_dir=tmp_path, campaign_id="camp_a", max_walltime_sec=10_000
    )

    manifest = read_manifest(tmp_path, "camp_a")
    assert manifest is not None
    assert manifest["goal"] == "tune ridge"
    assert manifest["stop_criteria"]["max_iters"] == 20
    # Untouched budget cap preserved; raised cap applied.
    assert manifest["budget"]["max_jobs"] == 50
    assert manifest["budget"]["max_walltime_sec"] == 10_000


def test_raising_cap_preserves_async_greenlight_and_anomaly_sections(
    journal_home: Path, tmp_path: Path
) -> None:
    """The cap raise rewrites the manifest in place — every section it does
    not touch survives byte-for-byte, including async_refill / max_in_flight,
    the greenlit / greenlit_at provenance marker, and anomaly_policy (these
    were silently dropped by the earlier goal/budget/stop_criteria/strategy
    whitelist rewrite)."""
    _seed_spend(tmp_path, run_id="run_0000", elapsed_sec=500)
    write_manifest(
        tmp_path,
        campaign_id="camp_a",
        goal="tune ridge",
        budget={"max_walltime_sec": 400},
        strategy={"name": "pbt", "params": {"population": 8}},
        anomaly_policy={"on_anomaly": "park", "resubmit_cap": 3},
        async_refill=True,
        max_in_flight=4,
        greenlit=True,
        greenlit_at="2026-07-01T00:00:00+00:00",
    )
    campaign_acknowledge_budget(
        experiment_dir=tmp_path, campaign_id="camp_a", max_walltime_sec=10_000
    )

    manifest = read_manifest(tmp_path, "camp_a")
    assert manifest is not None
    assert manifest["budget"]["max_walltime_sec"] == 10_000
    assert manifest["async_refill"] is True
    assert manifest["max_in_flight"] == 4
    assert manifest["greenlit"] is True
    assert manifest["greenlit_at"] == "2026-07-01T00:00:00+00:00"
    assert manifest["anomaly_policy"] == {"on_anomaly": "park", "resubmit_cap": 3}
    assert manifest["strategy"] == {"name": "pbt", "params": {"population": 8}}


def test_raising_cap_degrades_schema_invalid_manifest_to_fresh_caps(
    journal_home: Path, tmp_path: Path
) -> None:
    """A schema-invalid manifest must not block clearing a budget halt: the
    ack degrades to a minimal fresh manifest carrying the raised caps."""
    import json

    from hpc_agent.meta.campaign.manifest import manifest_path

    _seed_spend(tmp_path, run_id="run_0000", elapsed_sec=500)
    path = manifest_path(tmp_path, "camp_a")
    path.write_text(
        json.dumps(
            {
                "manifest_schema_version": 1,
                "campaign_id": "camp_a",
                "not_a_manifest_field": True,
            }
        ),
        encoding="utf-8",
    )

    campaign_acknowledge_budget(
        experiment_dir=tmp_path, campaign_id="camp_a", max_walltime_sec=10_000
    )

    manifest = read_manifest(tmp_path, "camp_a")
    assert manifest is not None
    assert manifest["budget"]["max_walltime_sec"] == 10_000
    assert "not_a_manifest_field" not in manifest
