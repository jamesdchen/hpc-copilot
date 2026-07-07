"""Tests for the ``export-dossier`` bundler (``ops/export_dossier.py``).

Fixtures are built through the REAL writers — ``write_run_sidecar`` (with
scopes), ``append_decision``, ``append_brief``, ``record_terminal``,
``upsert_run``, ``record_lock``, ``record_look`` — so the pins exercise the
same on-disk stores the framework writes in production, not hand-rolled JSON.

Pinned properties:

* manifest completeness — every seeded store yields exactly one entry with the
  correct source, and the closed source vocabulary is respected.
* hash stability — two exports of an unchanged store produce an identical
  ``bundle_sha256`` and an identical path-sorted ``entries`` list.
* opaque-bytes fidelity — an ``_aggregated`` file containing DELIBERATELY
  INVALID JSON round-trips byte-identical out of the zip (proves no parsing).
* missing ``_aggregated`` → a gap + success.
* include_lineage bundles the ancestor's trail and unions scope tags.
* a run with neither sidecar nor journal record → ``SpecInvalid``.
* a second export overwrites the archive cleanly.
"""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.export_dossier import ExportDossierSpec
from hpc_agent.ops.export_dossier import DOSSIER_SOURCES, export_dossier
from hpc_agent.state import run_record
from hpc_agent.state.block_terminal import record_terminal
from hpc_agent.state.decision_briefs import append_brief
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar
from hpc_agent.state.scopes import record_lock, record_look

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the per-user journal home into the test's tmp dir."""
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


_RID = "20260101-000001-aaaaaaa"


def _sidecar(experiment_dir: Path, run_id: str, **overrides: Any) -> None:
    kwargs: dict[str, Any] = dict(
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 run.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=2,
        tasks_py_sha="1" * 64,
    )
    kwargs.update(overrides)
    write_run_sidecar(experiment_dir, **kwargs)


def _record(run_id: str, **overrides: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": run_id,
        "profile": "p",
        "cluster": "hoffman2",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "p",
        "job_ids": ["9001"],
        "total_tasks": 2,
        "submitted_at": "2026-01-01T00:00:00Z",
        "experiment_dir": str(run_id),
    }
    base.update(overrides)
    return RunRecord(**base)


def _seed_full_run(
    experiment_dir: Path,
    run_id: str,
    *,
    scopes: list[str] | None = None,
    with_aggregated: bool = True,
    aggregated_bytes: bytes | None = None,
    supersedes: str = "",
) -> None:
    """Write every per-run + per-scope store for *run_id* through the real writers."""
    tags = scopes or []
    _sidecar(experiment_dir, run_id, scopes=tags or None)
    upsert_run(experiment_dir, _record(run_id, supersedes=supersedes))
    append_decision(experiment_dir, scope_kind="run", scope_id=run_id, block="s1", response="y")
    append_brief(experiment_dir, run_id=run_id, block="s2", brief={"question": "greenlight?"})
    record_terminal(
        experiment_dir,
        run_id=run_id,
        block="s2",
        cmd_sha="0" * 64,
        result_dump={"block": "s2", "needs_decision": False},
    )
    for tag in tags:
        record_lock(experiment_dir, tag, reason="freeze after canary")
        record_look(
            experiment_dir,
            tag,
            run_id=run_id,
            cmd_sha="0" * 64,
            lineage_root=run_id,
            reducer_block="reduce",
        )
    if with_aggregated:
        agg_dir = experiment_dir / "_aggregated" / run_id
        agg_dir.mkdir(parents=True, exist_ok=True)
        (agg_dir / "metrics_aggregate.json").write_bytes(b'{"mean": 3.14}')
        (experiment_dir / "_aggregated" / f"{run_id}.json").write_bytes(
            aggregated_bytes if aggregated_bytes is not None else b'{"reduced": true}'
        )


def _sources_of(manifest: dict[str, Any]) -> list[str]:
    return [e["source"] for e in manifest["entries"]]


# --- manifest completeness ---------------------------------------------------


def test_every_seeded_store_becomes_exactly_one_entry(journal_home: Path, experiment: Path) -> None:
    _seed_full_run(experiment, _RID, scopes=["holdout"])

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    sources = _sources_of(result.manifest)
    # One of each per-run store + one scope-journal + one look-ledger + one dir
    # aggregate + one file aggregate.
    from collections import Counter

    counts = Counter(sources)
    assert counts["sidecar"] == 1
    assert counts["decision-journal"] == 1
    assert counts["briefs"] == 1
    assert counts["block-terminal"] == 1
    assert counts["journal-record"] == 1
    assert counts["scope-journal"] == 1
    assert counts["look-ledger"] == 1
    assert counts["aggregated"] == 2  # dir file + file variant
    # No entry escapes the closed store vocabulary.
    assert set(sources) <= DOSSIER_SOURCES
    assert not result.gaps
    assert result.entry_count == len(result.manifest["entries"])
    assert result.run_ids == [_RID]


def test_archive_layout_and_manifest_are_written(journal_home: Path, experiment: Path) -> None:
    _seed_full_run(experiment, _RID, scopes=["holdout"])

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    with zipfile.ZipFile(result.archive_path) as zf:
        names = set(zf.namelist())
    assert f"runs/{_RID}/sidecar.json" in names
    assert f"runs/{_RID}/decisions.jsonl" in names
    assert f"runs/{_RID}/briefs.jsonl" in names
    assert f"runs/{_RID}/s2.terminal.json" in names
    assert f"runs/{_RID}/journal.json" in names
    assert "scopes/holdout.decisions.jsonl" in names
    assert "scopes/holdout.looks.jsonl" in names
    assert f"aggregated/{_RID}/metrics_aggregate.json" in names
    assert f"aggregated/{_RID}.json" in names
    assert "manifest.json" in names
    # manifest.json is the seal, never itself a source entry.
    assert "manifest.json" not in {e["path"] for e in result.manifest["entries"]}
    # Identity projection is an allowlist, never the whole sidecar.
    (proj,) = result.manifest["runs"]
    assert proj["run_id"] == _RID
    assert proj["cmd_sha"] == "0" * 64
    assert proj["scopes"] == ["holdout"]
    assert "executor" not in proj  # a non-allowlisted sidecar field never leaks


def test_default_output_path_is_derived_under_dossier(journal_home: Path, experiment: Path) -> None:
    _seed_full_run(experiment, _RID)
    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))
    assert result.archive_path == str(experiment / "_dossier" / f"{_RID}.zip")
    assert (experiment / "_dossier" / f"{_RID}.zip").is_file()


# --- hash stability ----------------------------------------------------------


def test_two_exports_produce_identical_bundle_sha_and_entries(
    journal_home: Path, experiment: Path
) -> None:
    _seed_full_run(experiment, _RID, scopes=["holdout"])
    out1 = experiment / "a.zip"
    out2 = experiment / "b.zip"

    r1 = export_dossier(
        experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID, output_path=str(out1))
    )
    r2 = export_dossier(
        experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID, output_path=str(out2))
    )

    assert r1.bundle_sha256 == r2.bundle_sha256
    assert r1.manifest["entries"] == r2.manifest["entries"]
    # Entries are path-sorted (deterministic regardless of gather order).
    paths = [e["path"] for e in r1.manifest["entries"]]
    assert paths == sorted(paths)


# --- opaque-bytes fidelity ---------------------------------------------------


def test_invalid_json_aggregate_round_trips_byte_identical(
    journal_home: Path, experiment: Path
) -> None:
    garbage = b"{ this is deliberately: not valid json ]]]\x00\xff"
    _seed_full_run(experiment, _RID, aggregated_bytes=garbage)

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    with zipfile.ZipFile(result.archive_path) as zf:
        got = zf.read(f"aggregated/{_RID}.json")
    assert got == garbage  # never parsed, never re-serialized — pure bytes


# --- gaps --------------------------------------------------------------------


def test_missing_aggregated_records_a_gap_and_still_succeeds(
    journal_home: Path, experiment: Path
) -> None:
    _seed_full_run(experiment, _RID, with_aggregated=False)

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    agg_gaps = [g for g in result.gaps if g["source"] == "aggregated"]
    assert len(agg_gaps) == 1
    assert agg_gaps[0]["run_id"] == _RID
    # No aggregated entry was sealed, but the bundle is still written.
    assert "aggregated" not in _sources_of(result.manifest)
    assert zipfile.is_zipfile(result.archive_path)


# --- include_lineage ---------------------------------------------------------


def test_include_lineage_bundles_ancestor_trail_and_unions_scope_tags(
    journal_home: Path, experiment: Path
) -> None:
    root = "20260101-000001-rrrrrrr"
    child = "20260101-000002-cccccc"
    _seed_full_run(experiment, root, scopes=["embargo"])
    _seed_full_run(experiment, child, scopes=["holdout"], supersedes=root)

    result = export_dossier(
        experiment_dir=experiment,
        spec=ExportDossierSpec(run_id=child, include_lineage=True),
    )

    # Newest→root order.
    assert result.run_ids == [child, root]
    with zipfile.ZipFile(result.archive_path) as zf:
        names = set(zf.namelist())
    # Both runs' sidecars are bundled.
    assert f"runs/{child}/sidecar.json" in names
    assert f"runs/{root}/sidecar.json" in names
    # Scope tags are UNIONED across the lineage.
    assert "scopes/holdout.decisions.jsonl" in names
    assert "scopes/embargo.decisions.jsonl" in names
    assert {p["run_id"] for p in result.manifest["runs"]} == {child, root}


# --- missing run -------------------------------------------------------------


def test_missing_run_with_no_sidecar_and_no_record_raises(
    journal_home: Path, experiment: Path
) -> None:
    with pytest.raises(errors.SpecInvalid):
        export_dossier(
            experiment_dir=experiment,
            spec=ExportDossierSpec(run_id="20260101-999999-nope000"),
        )


# --- idempotent overwrite ----------------------------------------------------


def test_second_export_overwrites_the_archive_cleanly(journal_home: Path, experiment: Path) -> None:
    _seed_full_run(experiment, _RID, scopes=["holdout"])
    out = experiment / "dossier.zip"

    export_dossier(
        experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID, output_path=str(out))
    )
    r2 = export_dossier(
        experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID, output_path=str(out))
    )

    # A clean overwrite: exactly the sealed entries + one manifest.json, no
    # stale/duplicated members from the first write.
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert len(names) == len(set(names))  # no duplicate members
    assert names.count("manifest.json") == 1
    assert len(names) == r2.entry_count + 1  # entries + manifest.json
