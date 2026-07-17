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


def test_wheel_source_prefers_signed_manifest_and_discloses(tmp_path: Path) -> None:
    from hpc_agent.ops.provenance_manifest import write_provenance_manifest

    _seed_campaign(tmp_path)

    # No manifest yet: the sidecar projection is the disclosed source.
    r0 = extract_recipe(tmp_path, spec=ExtractRecipeInput(campaign_id="camp"))
    assert r0["runs"]
    for run in r0["runs"]:
        assert run["hpc_agent_version_source"] == "sidecar"
        assert run["hpc_agent_version"] == "9.9.9"

    # Write the signed v2 provenance manifest → the wheel sha is now signed.
    write_provenance_manifest(tmp_path, "camp")
    r1 = extract_recipe(tmp_path, spec=ExtractRecipeInput(campaign_id="camp"))
    for run in r1["runs"]:
        assert run["hpc_agent_version_source"] == "signed-manifest"
        assert run["hpc_agent_version"] == "9.9.9"

    # PREFERENCE proof: drift the sidecar AFTER signing; the SIGNED value wins.
    _sidecar(tmp_path, "exp-good-aaaaaaaa", campaign_id="camp", version="0.0.0-drift")
    r2 = extract_recipe(tmp_path, spec=ExtractRecipeInput(campaign_id="camp"))
    good = next(r for r in r2["runs"] if r["run_id"] == "exp-good-aaaaaaaa")
    assert good["hpc_agent_version"] == "9.9.9", (
        "the signed manifest value must win over a drifted sidecar"
    )
    assert good["hpc_agent_version_source"] == "signed-manifest"


def test_env_lock_source_prefers_signed_manifest_and_discloses(tmp_path: Path) -> None:
    from hpc_agent.ops.provenance_manifest import write_provenance_manifest
    from hpc_agent.state.env_lock import STATUS_CAPTURED
    from hpc_agent.state.runs import stamp_run_sidecar_env_lock

    _seed_campaign(tmp_path)
    # Stamp a captured env lock on the good run BEFORE signing.
    good_env = "a" * 64
    stamp_run_sidecar_env_lock(
        tmp_path, "exp-good-aaaaaaaa", env_lock_sha=good_env, env_lock_status=STATUS_CAPTURED
    )

    # No manifest yet: the sidecar projection is the disclosed source.
    r0 = extract_recipe(tmp_path, spec=ExtractRecipeInput(campaign_id="camp"))
    good0 = next(r for r in r0["runs"] if r["run_id"] == "exp-good-aaaaaaaa")
    assert good0["env_lock_sha_source"] == "sidecar"
    assert good0["env_lock_sha"] == good_env

    # Write the signed v3 provenance manifest → the env lock is now signed.
    write_provenance_manifest(tmp_path, "camp")
    r1 = extract_recipe(tmp_path, spec=ExtractRecipeInput(campaign_id="camp"))
    good1 = next(r for r in r1["runs"] if r["run_id"] == "exp-good-aaaaaaaa")
    assert good1["env_lock_sha_source"] == "signed-manifest"
    assert good1["env_lock_sha"] == good_env

    # PREFERENCE proof: drift the sidecar env lock AFTER signing; the SIGNED wins.
    _sidecar(tmp_path, "exp-good-aaaaaaaa", campaign_id="camp")  # rewrite → env_lock back to None
    stamp_run_sidecar_env_lock(
        tmp_path, "exp-good-aaaaaaaa", env_lock_sha="d" * 64, env_lock_status=STATUS_CAPTURED
    )
    r2 = extract_recipe(tmp_path, spec=ExtractRecipeInput(campaign_id="camp"))
    good2 = next(r for r in r2["runs"] if r["run_id"] == "exp-good-aaaaaaaa")
    assert good2["env_lock_sha"] == good_env, "the signed env lock must win over a drifted sidecar"
    assert good2["env_lock_sha_source"] == "signed-manifest"


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


def test_cluster_reduced_table_reads_contributing_run_ids_not_lineage(tmp_path: Path) -> None:
    """Gap-closing pin (G4a cluster leg): a table reduced by the cluster ``--final``
    combiner now carries ``contributing_run_ids`` in its footer, so extract-recipe
    reads the real minimal run-set instead of degrading to the lineage fallback and
    disclosing the ``table-run-set-link-absent`` gap."""
    import pytest

    from hpc_agent.execution.mapreduce import combiner

    run_id = "exp-cluster-99999999"
    _seed(tmp_path, run_id)  # run record + sidecar (.hpc/runs) + harvest receipt
    # Run-scoped combiner partials for the run, then the cluster --final reduce. The
    # combiner runs cwd-relative under the remote project root; chdir so ``.hpc/``
    # and ``_combiner/`` sit directly under cwd exactly as on the cluster.
    scoped = tmp_path / "_combiner" / run_id
    scoped.mkdir(parents=True)
    (scoped / "wave_0.json").write_text(
        json.dumps(
            {"wave": 0, "run_id": run_id, "grid_points": {"g0": {"acc": 0.9, "n_samples": 1}}}
        ),
        encoding="utf-8",
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(tmp_path)
        combiner.main(argv=["--final", "--run-id", run_id])

    agg = tmp_path / "_aggregated" / run_id / "metrics_aggregate.json"
    recipe = extract_recipe(tmp_path, spec=ExtractRecipeInput(aggregate_path=str(agg)))
    assert recipe["minimal_run_ids"] == [run_id]
    # The G4a table->run-set-link gap is NOT disclosed — the link is now first-class.
    codes = {g["code"] for g in recipe["gaps"]}
    assert "table-run-set-link-absent" not in codes


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


def test_recipe_citation_resolver_round_trips_signature_and_summary(tmp_path: Path) -> None:
    from hpc_agent.ops.extract_recipe import recipe_citation_ref, resolve_recipe_citation

    _seed_campaign(tmp_path)
    recipe = extract_recipe(tmp_path, spec=ExtractRecipeInput(campaign_id="camp"))

    ref = recipe_citation_ref("campaign", "camp")
    resolution = resolve_recipe_citation(tmp_path, ref)
    assert resolution is not None
    signature, summary = resolution
    # parity: the resolved signature IS the recipe's own signature (re-derived).
    assert signature == recipe["recipe_signature"]
    # the disclosure summary carries the minimal-set size / exclusions / gaps / wheel-src.
    assert "minimal 2" in summary
    assert "excluded 3" in summary
    assert "gaps 0" in summary
    assert "wheel-src sidecar" in summary


def test_recipe_citation_resolver_discloses_when_not_derivable(tmp_path: Path) -> None:
    from hpc_agent.ops.extract_recipe import resolve_recipe_citation

    # A malformed ref, a bad seed kind, or an empty seed_ref → not derivable (None),
    # never a raise (the read-side disclosure posture).
    assert resolve_recipe_citation(tmp_path, "not-a-ref") is None
    assert resolve_recipe_citation(tmp_path, "campaign:") is None
    assert resolve_recipe_citation(tmp_path, "bogus:x") is None
    # An aggregate seed whose path does not exist → extract-recipe refuses → None.
    assert resolve_recipe_citation(tmp_path, "aggregate:/no/such/table.json") is None


def test_exactly_one_seed_required(tmp_path: Path) -> None:
    import pytest

    from hpc_agent import errors

    with pytest.raises(errors.SpecInvalid):
        extract_recipe(tmp_path, spec=ExtractRecipeInput())
    with pytest.raises(errors.SpecInvalid):
        extract_recipe(tmp_path, spec=ExtractRecipeInput(run_id="a", campaign_id="b"))


# ── G2: mechanical dead-end disambiguation (docs/design/dead-end-disambiguation.md) ──


def _persist_agg(
    experiment_dir: Path, run_id: str, contributing: list[str], *, source: str = "local_reduce"
) -> None:
    """Persist a run's reduced table with a Task-1 contributing_run_ids block."""
    agg = experiment_dir / "_aggregated" / run_id / "metrics_aggregate.json"
    agg.parent.mkdir(parents=True, exist_ok=True)
    agg.write_text(
        json.dumps(
            {
                "aggregated_metrics": {},
                "provenance": {
                    "incomplete_waves": [],
                    "source": source,
                    "reduced_at": _TS,
                    "contributing_run_ids": list(contributing),
                    "piece_cmd_shas": [],
                    "hpc_agent_version": "9.9.9",
                },
            }
        ),
        encoding="utf-8",
    )


def test_anchored_seed_harvested_but_non_contributing_sibling_is_mechanical_dead_end(
    tmp_path: Path,
) -> None:
    """A campaign sibling that HARVESTED but never fed the cited table is a DEAD-END.

    This is the G2 hole the harvest-receipt proxy left open: the dead-end run has a
    harvest receipt (it ran to completion), so the proxy would KEEP it and pollute
    the minimal recipe. Anchoring on the cited table's contributing_run_ids
    mechanically excludes it — the disclosure names the mechanical reason.
    """
    cited = "exp-cited-11111111"
    dead_harvested = "exp-deadhv-22222222"
    _seed(tmp_path, cited, campaign_id="campM")  # harvested=True (default)
    _seed(tmp_path, dead_harvested, campaign_id="campM")  # ALSO harvested
    _persist_agg(tmp_path, cited, [cited])  # the cited table fed ONLY by `cited`

    recipe = extract_recipe(tmp_path, spec=ExtractRecipeInput(run_id=cited))

    assert recipe["minimal_run_ids"] == [cited]
    reasons = {e["run_id"]: e["reason"] for e in recipe["excluded"]}
    assert dead_harvested in reasons, "the harvested-but-non-contributing sibling was kept"
    reason = reasons[dead_harvested]
    assert reason.startswith("dead-end")
    # MECHANICAL, not the proxy: the reason names the contributing set, not a receipt.
    assert "contributing_run_ids" in reason
    assert "proxy" not in reason


def test_anchored_seed_keeps_contributor_without_harvest_receipt_graft_class(
    tmp_path: Path,
) -> None:
    """A run PROVABLY in contributing_run_ids is kept even with NO harvest receipt.

    The run-13 graft class: a repair re-ran arms under a new run id into another
    run's tree, so it appears in the table's contributing_run_ids but was never
    independently harvested (no receipt of its own). Membership outranks the
    receipt proxy — the graft must NOT be excluded as a dead end.
    """
    host = "exp-host-33333333"
    graft = "exp-graft-44444444"
    _seed(tmp_path, host, campaign_id="campG")  # harvested
    _seed(tmp_path, graft, campaign_id="campG", harvested=False)  # NO harvest receipt
    _persist_agg(tmp_path, host, [host, graft])  # both fed the cited table

    recipe = extract_recipe(tmp_path, spec=ExtractRecipeInput(run_id=host))

    assert graft in recipe["minimal_run_ids"], (
        "a proven contributor was excluded for lacking a harvest receipt"
    )
    reasons = {e["run_id"]: e["reason"] for e in recipe["excluded"]}
    assert graft not in reasons


def test_anchored_seed_supersession_ancestor_is_superseded_not_dead_end(tmp_path: Path) -> None:
    """A campaign sibling superseded by a contributor reads SUPERSEDED, not dead-end.

    The ancestor did not itself feed the cited table, but its lineage descendant (a
    contributor) did — so it collapses toward the head as `superseded`, distinct
    from a genuine dead end.
    """
    new = "exp-new-55555555"
    old = "exp-old-66666666"
    _seed(tmp_path, old, campaign_id="campSup")
    _seed(tmp_path, new, campaign_id="campSup", supersedes=old)
    _persist_agg(tmp_path, new, [new])  # only the newest fed the table

    recipe = extract_recipe(tmp_path, spec=ExtractRecipeInput(run_id=new))

    assert recipe["minimal_run_ids"] == [new]
    reasons = {e["run_id"]: e["reason"] for e in recipe["excluded"]}
    assert reasons[old] == "superseded"


def test_campaign_seed_dead_end_reason_discloses_the_harvest_proxy(tmp_path: Path) -> None:
    """A bare campaign seed has no single cited table → the dead-end reason SAYS proxy.

    Behaviour is unchanged (the harvest-receipt heuristic still carves the set), but
    the reason now discloses it is a PROXY and points at the anchored seed that
    gives the mechanical answer — so a campaign-seed dead-end is never misread as
    the mechanical contributing set.
    """
    _seed_campaign(tmp_path)
    recipe = extract_recipe(tmp_path, spec=ExtractRecipeInput(campaign_id="camp"))
    reasons = {e["run_id"]: e["reason"] for e in recipe["excluded"]}
    dead_reason = reasons["exp-dead-dddddddd"]
    assert dead_reason.startswith("dead-end")
    assert "proxy" in dead_reason
    assert "--aggregate-path" in dead_reason and "--run-id" in dead_reason
