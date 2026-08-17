"""Adopted-run (unobserved) disclosure behaviour of ``export-bundle``.

The post-exploration checker (``docs/design/post-exploration-checker.md``)
adopts runs submitted OUTSIDE hpc-agent. An adopted run has a sidecar (with the
``extra.adopted`` pocket), a journal record, possibly a claim-check receipt —
but NO fingerprint samples (the ledger is observed-runs-only) and no env/data
manifests unless supplied. These tests pin what the bundle does about that:

* the adoption decision-journal entries and the claim-check receipt(s) and the
  aggregate reducer output are SEALED inside ``dossier-evidence`` when present;
* the missing fingerprint / manifest axes appear as EXPLICIT
  ``not captured (unobserved run)`` disclosure entries, never absent keys and
  never an exception;
* DISCLOSURE ONLY — link statuses, the verdict, and every computed value are
  untouched, and an OBSERVED-run bundle is byte-identical to before the
  adoption disclosures existed (``_adoption_disclosures`` contributes nothing).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest


def _seed_run(
    experiment: Path,
    run_id: str,
    *,
    adopted: bool,
    campaign_id: str | None = None,
) -> None:
    """Seed a run through the REAL writers (the bundle-boundary seeding, plus
    the ``extra.adopted`` pocket + the ``block == "adopt-run"`` journal record
    for an adopted run)."""
    from hpc_agent.state.decision_journal import append_decision
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 run.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=2,
        tasks_py_sha="1" * 64,
        campaign_id=campaign_id,
        extra={"adopted": {"by": "adopt-run", "at": "2026-01-01T00:00:00Z"}} if adopted else None,
    )
    upsert_run(
        experiment,
        RunRecord(
            run_id=run_id,
            profile="adopted" if adopted else "p",
            cluster="hoffman2",
            ssh_target="user@host",
            remote_path="/remote",
            job_name="p",
            job_ids=[] if adopted else ["9001"],
            total_tasks=2,
            submitted_at="2026-01-01T00:00:00Z",
            experiment_dir=str(run_id),
        ),
    )
    if adopted:
        append_decision(
            experiment,
            scope_kind="run",
            scope_id=run_id,
            block="adopt-run",
            response="y",
            proposal="reporter RC=0 all tasks; result tree on disk",
            provenance={"directed": True, "kind": "adopt-run-directed-settle"},
        )
    else:
        append_decision(experiment, scope_kind="run", scope_id=run_id, block="s1", response="y")


def _seed_table(experiment: Path, run_id: str, value: str) -> None:
    agg = experiment / "_aggregated" / run_id / "metrics_aggregate.json"
    agg.parent.mkdir(parents=True, exist_ok=True)
    agg.write_text(
        json.dumps(
            {
                "aggregated_metrics": {run_id: {"score": value}},
                "provenance": {"source": "local_reduce", "contributing_run_ids": [run_id]},
            }
        ),
        encoding="utf-8",
    )


def _seed_claim_check_receipt(experiment: Path, run_id: str) -> None:
    """A claim-check receipt in its own ledger (the verify-reproduction shape)."""
    path = experiment / "_aggregated" / run_id / "claim_check_receipts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {
        "ts": "2026-01-02T00:00:00Z",
        "receipt_kind": "claim-check",
        "schema_version": 1,
        "claim": {"claimed_values": {"score": 0.9421}, "tolerance": None, "claimed_data_sha": None},
        "overall": "match",
    }
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")


def _export(experiment: Path, run_id: str) -> Any:
    from hpc_agent._wire.actions.publication_bundle import ExportBundleSpec
    from hpc_agent.ops.publication_bundle import export_bundle

    return export_bundle(experiment_dir=experiment, spec=ExportBundleSpec(run_id=run_id))


def _adoption_entries(disclosures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in disclosures if d.get("origin") == "adoption"]


def test_adopted_run_bundle_seals_receipts_journal_and_discloses_axes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adopted-run bundle: no crash, journal + claim-check receipt + reducer
    output sealed, and every missing axis an explicit disclosure entry."""
    from hpc_agent.state import run_record

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    experiment = tmp_path / "exp"
    experiment.mkdir()
    run_id = "20260101-000010-adopted"
    _seed_run(experiment, run_id, adopted=True)
    _seed_table(experiment, run_id, "0.9421")
    _seed_claim_check_receipt(experiment, run_id)

    result = _export(experiment, run_id)  # NOT an exception — the no-crash pin

    with zipfile.ZipFile(Path(result.bundle_path)) as zf:
        names = set(zf.namelist())
        # The adoption decision journal is a sealed dossier store.
        assert f"dossier/runs/{run_id}/decisions.jsonl" in names
        journal = zf.read(f"dossier/runs/{run_id}/decisions.jsonl").decode("utf-8")
        assert '"adopt-run"' in journal, "the adoption journal entry rides the sealed journal"
        # The claim-check receipt ledger + the aggregate reducer output are
        # sealed (the whole _aggregated/<run_id>/ tree is a dossier store).
        assert f"dossier/aggregated/{run_id}/claim_check_receipts.jsonl" in names
        assert f"dossier/aggregated/{run_id}/metrics_aggregate.json" in names
        verify = json.loads(zf.read("VERIFY.json"))
        # The human render surfaces the adoption disclosures (bundle_render's
        # existing {origin, code, detail} ledger loop — no render change needed).
        render = zf.read("VERIFY.md").decode("utf-8")
        assert "[adoption]" in render
        assert "not captured (unobserved run)" in render

    adoption = _adoption_entries(verify["disclosures"])
    codes = {d["code"] for d in adoption}
    assert "unobserved-run" in codes
    assert "fingerprint-not-captured" in codes
    assert "manifest-not-captured" in codes  # no data_sha / env_lock_sha at adoption
    # The explicit "not captured (unobserved run)" phrasing, never an absent key.
    assert any("not captured (unobserved run)" in d["detail"] for d in adoption)
    assert all(d.get("run_id") == run_id for d in adoption)
    # The disclosures ride the result wire too.
    assert _adoption_entries(result.disclosures) == adoption


def test_adoption_disclosures_never_change_links_or_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DISCLOSURE ONLY: adopted vs observed twins classify + verdict identically."""
    from hpc_agent.state import run_record

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    experiment = tmp_path / "exp"
    experiment.mkdir()
    obs, adp = "20260101-000011-obs", "20260101-000012-adp"
    for rid, adopted in ((obs, False), (adp, True)):
        _seed_run(experiment, rid, adopted=adopted)
        _seed_table(experiment, rid, "0.9421")

    r_obs = _export(experiment, obs)
    r_adp = _export(experiment, adp)

    # Same link classification and the same code-emitted verdict.
    assert r_obs.verify_manifest["links"] == r_adp.verify_manifest["links"]
    assert r_obs.verdict == r_adp.verdict

    # The adopted bundle's disclosures = the observed ones + the adoption block
    # (up to the run_id each twin's dossier gaps naturally embed).
    def _norm(items: list[dict[str, Any]], rid: str) -> str:
        return json.dumps(items, sort_keys=True).replace(rid, "<RID>")

    non_adoption = [d for d in r_adp.disclosures if d.get("origin") != "adoption"]
    assert _norm(non_adoption, adp) == _norm(r_obs.disclosures, obs)
    assert _adoption_entries(r_adp.disclosures)
    assert not _adoption_entries(r_obs.disclosures)


def test_observed_run_bundle_is_byte_identical_to_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION: over observed runs ``_adoption_disclosures`` contributes
    NOTHING — the ledger, the VERIFY manifest, and the sealed archive carry no
    adoption vocabulary, so the output is byte-identical to before the
    disclosure existed (the only change was an ``extend`` of its result)."""
    from hpc_agent.ops.publication_bundle import _adoption_disclosures
    from hpc_agent.state import run_record

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    experiment = tmp_path / "exp"
    experiment.mkdir()
    run_id = "20260101-000013-observed"
    _seed_run(experiment, run_id, adopted=False)
    _seed_table(experiment, run_id, "0.9421")

    # The seam itself: empty for an observed run.
    assert _adoption_disclosures(experiment, [run_id]) == []

    result = _export(experiment, run_id)
    assert not _adoption_entries(result.disclosures)
    with zipfile.ZipFile(Path(result.bundle_path)) as zf:
        verify_bytes = zf.read("VERIFY.json").decode("utf-8")
        render = zf.read("VERIFY.md").decode("utf-8")
    for marker in ("adoption", "unobserved", "not captured"):
        assert marker not in verify_bytes, f"observed-run VERIFY.json leaked {marker!r}"
        assert marker not in render, f"observed-run VERIFY.md leaked {marker!r}"


def test_adopted_run_with_no_fingerprint_ledger_never_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-crash pin: an adopted run with no fingerprint ledger, no receipts, and
    no aggregate still bundles — everything missing is a gap/disclosure."""
    from hpc_agent.state import run_record

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    experiment = tmp_path / "exp"
    experiment.mkdir()
    run_id = "20260101-000014-bare"
    _seed_run(experiment, run_id, adopted=True)  # no table, no receipt, no ledger

    result = _export(experiment, run_id)  # must not raise
    codes = {d["code"] for d in _adoption_entries(result.disclosures)}
    assert {"unobserved-run", "fingerprint-not-captured", "manifest-not-captured"} <= codes


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
