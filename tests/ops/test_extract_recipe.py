"""Tests for the ``extract-recipe`` query verb (clean-reproduction extraction #1).

Exercises the artifact → minimal-run-set → recipe walk over a tmp experiment with
REAL records: the minimal set correct on a fixture campaign with dead ends +
supersession + canary (each exclusion disclosed + counted), the wheel sha present
in every fingerprint, a pack *.csv accepted OPAQUE (content never parsed), and
the G4 gaps disclosed — never papered.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent._wire.queries.extract_recipe import ExtractRecipeInput
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.ops.extract_recipe import extract_recipe
from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

_TS = "2026-07-17T12:00:00+00:00"


def _record(run_id: str, *, campaign_id: str = "", supersedes: str = "") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="host",
        remote_path="/remote",
        job_name="job",
        job_ids=["1"],
        total_tasks=1,
        submitted_at=_TS,
        experiment_dir="/exp",
        campaign_id=campaign_id,
        supersedes=supersedes,
    )


def _sidecar(
    experiment_dir: Path, run_id: str, *, campaign_id: str = "", version: str = "9.9.9"
) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha=f"cmd-{run_id}",
        hpc_agent_version=version,
        submitted_at=_TS,
        executor="exec.py",
        result_dir_template="results/{i}",
        task_count=1,
        tasks_py_sha=f"tsha-{run_id}",
        cluster="hoffman2",
        profile="p",
        campaign_id=campaign_id or None,
        data_sha=f"data-{run_id}",
        data_manifest_sha=f"dman-{run_id}",
        env_hash=f"env-{run_id}",
    )


def _harvest(experiment_dir: Path, run_id: str) -> None:
    """Write a durable harvest receipt so the run reads as harvested (not dead-end)."""
    append_jsonl_line(
        harvest_marker_path(experiment_dir, run_id),
        {"run_id": run_id, "harvested_at": _TS, "harvest_ok": True},
    )


def _seed(
    experiment_dir: Path,
    run_id: str,
    *,
    campaign_id: str = "",
    supersedes: str = "",
    version: str = "9.9.9",
    harvested: bool = True,
) -> None:
    _sidecar(experiment_dir, run_id, campaign_id=campaign_id, version=version)
    upsert_run(experiment_dir, _record(run_id, campaign_id=campaign_id, supersedes=supersedes))
    if harvested:
        _harvest(experiment_dir, run_id)


# ── the campaign fixture: a good run, its canary, a superseded pair, a dead end ──


def _seed_campaign(experiment_dir: Path, campaign_id: str = "camp") -> None:
    _seed(experiment_dir, "exp-good-aaaaaaaa", campaign_id=campaign_id)
    # canary sibling — same campaign subtree, must be EXCLUDED (canary family).
    _seed(experiment_dir, "exp-good-aaaaaaaa-canary", campaign_id=campaign_id)
    # supersession pair: the old member is superseded by the new; keep newest.
    _seed(experiment_dir, "exp-old-bbbbbbbb", campaign_id=campaign_id)
    _seed(
        experiment_dir,
        "exp-new-cccccccc",
        campaign_id=campaign_id,
        supersedes="exp-old-bbbbbbbb",
    )
    # dead end: a real run that never harvested into the citable table.
    _seed(experiment_dir, "exp-dead-dddddddd", campaign_id=campaign_id, harvested=False)


def test_minimal_run_set_excludes_canary_superseded_and_dead_end(tmp_path: Path) -> None:
    _seed_campaign(tmp_path)
    recipe = extract_recipe(tmp_path, spec=ExtractRecipeInput(campaign_id="camp"))

    assert set(recipe["minimal_run_ids"]) == {"exp-good-aaaaaaaa", "exp-new-cccccccc"}

    # Every exclusion is disclosed with its reason and counted.
    reasons = {e["run_id"]: e["reason"] for e in recipe["excluded"]}
    assert reasons["exp-good-aaaaaaaa-canary"] == "canary"
    assert reasons["exp-old-bbbbbbbb"] == "superseded"
    assert reasons["exp-dead-dddddddd"].startswith("dead-end")
    assert len(recipe["excluded"]) == 3


def test_wheel_sha_present_in_every_fingerprint(tmp_path: Path) -> None:
    _seed_campaign(tmp_path)
    recipe = extract_recipe(tmp_path, spec=ExtractRecipeInput(campaign_id="camp"))
    assert recipe["runs"], "expected fingerprints for the minimal set"
    for run in recipe["runs"]:
        assert run["hpc_agent_version"] == "9.9.9", "the wheel sha must ride each fingerprint"
        # identity legs are present (params / code / data / env)
        assert run["cmd_sha"] == f"cmd-{run['run_id']}"
        assert run["tasks_py_sha"] == f"tsha-{run['run_id']}"


def test_recipe_signature_is_deterministic_over_the_minimal_set(tmp_path: Path) -> None:
    _seed_campaign(tmp_path)
    a = extract_recipe(tmp_path, spec=ExtractRecipeInput(campaign_id="camp"))
    b = extract_recipe(tmp_path, spec=ExtractRecipeInput(campaign_id="camp"))
    assert a["recipe_signature"] == b["recipe_signature"]
    assert len(a["recipe_signature"]) == 64


def test_inside_flow_aggregate_json_uses_contributing_run_ids(tmp_path: Path) -> None:
    _seed(tmp_path, "exp-solo-eeeeeeee")
    agg = tmp_path / "_aggregated" / "exp-solo-eeeeeeee" / "metrics_aggregate.json"
    agg.parent.mkdir(parents=True, exist_ok=True)
    agg.write_text(
        json.dumps(
            {
                "aggregated_metrics": {"exp-solo-eeeeeeee": {"n": 3}},
                "provenance": {
                    "incomplete_waves": [],
                    "source": "local_reduce",
                    "reduced_at": _TS,
                    "contributing_run_ids": ["exp-solo-eeeeeeee"],
                    "piece_cmd_shas": ["cmd-exp-solo-eeeeeeee"],
                    "hpc_agent_version": "9.9.9",
                },
            }
        ),
        encoding="utf-8",
    )
    recipe = extract_recipe(tmp_path, spec=ExtractRecipeInput(aggregate_path=str(agg)))
    assert recipe["seed_kind"] == "aggregate"
    assert recipe["minimal_run_ids"] == ["exp-solo-eeeeeeee"]
    assert recipe["gaps"] == []  # first-class link present — no gap


def test_old_shape_table_discloses_the_run_set_link_gap_not_papered(tmp_path: Path) -> None:
    agg = tmp_path / "_aggregated" / "exp-legacy-ffffffff" / "metrics_aggregate.json"
    agg.parent.mkdir(parents=True, exist_ok=True)
    # Pre-Task-1 provenance: no contributing_run_ids.
    agg.write_text(
        json.dumps(
            {
                "aggregated_metrics": {},
                "provenance": {"incomplete_waves": [], "source": "local_reduce"},
            }
        ),
        encoding="utf-8",
    )
    recipe = extract_recipe(tmp_path, spec=ExtractRecipeInput(aggregate_path=str(agg)))
    codes = {g["code"] for g in recipe["gaps"]}
    assert "table-run-set-link-absent" in codes
    assert recipe["minimal_run_ids"] == []


def test_pack_csv_is_opaque_and_its_content_is_never_parsed(tmp_path: Path) -> None:
    _seed(tmp_path, "exp-pack-77777777")
    csv = tmp_path / "_aggregated" / "exp-pack-77777777" / "metrics_table.csv"
    csv.parent.mkdir(parents=True, exist_ok=True)
    secret_number = "0.98765432101234"
    csv.write_text(f"estimator,qlike\nlinear,{secret_number}\n", encoding="utf-8")

    recipe = extract_recipe(tmp_path, spec=ExtractRecipeInput(aggregate_path=str(csv)))
    assert recipe["artifact_opaque"] is True
    codes = {g["code"] for g in recipe["gaps"]}
    assert "pack-csv-opaque" in codes
    # provenance is the containing run's; content NEVER parsed.
    assert recipe["minimal_run_ids"] == ["exp-pack-77777777"]
    blob = json.dumps(recipe)
    assert secret_number not in blob, "the CSV's content leaked — it must never be parsed"


def test_operator_bypass_source_is_disclosed_as_journal_provenance_absent(tmp_path: Path) -> None:
    _seed(tmp_path, "exp-bypass-88888888")
    agg = tmp_path / "_aggregated" / "exp-bypass-88888888" / "metrics_aggregate.json"
    agg.parent.mkdir(parents=True, exist_ok=True)
    agg.write_text(
        json.dumps(
            {
                "aggregated_metrics": {},
                "provenance": {
                    "source": "human-directed",
                    "contributing_run_ids": ["exp-bypass-88888888"],
                },
            }
        ),
        encoding="utf-8",
    )
    recipe = extract_recipe(tmp_path, spec=ExtractRecipeInput(aggregate_path=str(agg)))
    codes = {g["code"] for g in recipe["gaps"]}
    assert "operator-bypass" in codes


def test_receipts_and_rederivation_steps_are_emitted(tmp_path: Path) -> None:
    _seed_campaign(tmp_path)
    recipe = extract_recipe(tmp_path, spec=ExtractRecipeInput(campaign_id="camp"))
    # a receipt per kept run, with the harvest receipt present.
    receipts = {r["run_id"]: r for r in recipe["receipts"]}
    assert receipts["exp-good-aaaaaaaa"]["harvest_receipt"] is True
    # runnable steps: a reproduce-run per run + a final aggregate.
    verbs = [s["verb"] for s in recipe["rederivation_steps"]]
    assert verbs.count("reproduce-run") == len(recipe["minimal_run_ids"])
    assert verbs[-1] == "aggregate"
    assert recipe["markdown"].startswith("# Clean-reproduction recipe")


def test_exactly_one_seed_required(tmp_path: Path) -> None:
    import pytest

    from hpc_agent import errors

    with pytest.raises(errors.SpecInvalid):
        extract_recipe(tmp_path, spec=ExtractRecipeInput())
    with pytest.raises(errors.SpecInvalid):
        extract_recipe(tmp_path, spec=ExtractRecipeInput(run_id="a", campaign_id="b"))
