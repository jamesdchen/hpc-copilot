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

import json
import zipfile
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.export_dossier import ExportDossierSpec
from hpc_agent.ops.export_dossier import (
    DOSSIER_SOURCES,
    compute_dossier_signature,
    export_dossier,
)
from hpc_agent.state.block_terminal import record_terminal
from hpc_agent.state.decision_briefs import append_brief
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.fingerprint_store import fingerprint_path
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import run_sidecar_path, write_run_sidecar
from hpc_agent.state.scopes import record_lock, record_look

if TYPE_CHECKING:
    from pathlib import Path


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


_FINGERPRINT_LEDGER_BYTES = (
    b'{"ts":"2026-01-01T00:00:00Z","schema_version":1,"subject_kind":'
    b'"determinism-fingerprint","source":"double-canary","verdict":"auto_cleared"}\n'
)


def _seed_full_run(
    experiment_dir: Path,
    run_id: str,
    *,
    scopes: list[str] | None = None,
    with_aggregated: bool = True,
    aggregated_bytes: bytes | None = None,
    supersedes: str = "",
    with_fingerprint: bool = True,
    fingerprint_bytes: bytes | None = None,
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
    if with_fingerprint:
        # The cmd_sha-addressed determinism-fingerprint ledger, written as RAW
        # BYTES (the bundler seals it verbatim, never parses it). cmd_sha matches
        # the sidecar default so the identity-addressed path resolves.
        ledger = fingerprint_path(experiment_dir, "0" * 64)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_bytes(
            fingerprint_bytes if fingerprint_bytes is not None else _FINGERPRINT_LEDGER_BYTES
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
    assert counts["determinism-fingerprint"] == 1  # the cmd_sha-addressed ledger
    # No entry escapes the closed store vocabulary.
    assert set(sources) <= DOSSIER_SOURCES
    assert not result.gaps
    assert result.entry_count == len(result.manifest["entries"])
    assert result.run_ids == [_RID]


def test_corrupt_sidecar_degrades_to_gap_not_crash(journal_home: Path, experiment: Path) -> None:
    """#43: a torn / hand-edited / newer-schema sidecar must degrade the identity
    projection (null-padded), not crash export_dossier / the recompute lock. The
    raw sidecar bytes are still sealed at the no-parse boundary."""
    _seed_full_run(experiment, _RID, scopes=["holdout"])
    # Corrupt the sidecar AFTER seeding — a truncated write the strict
    # read_run_sidecar parse would raise json.JSONDecodeError on.
    run_sidecar_path(experiment, _RID).write_text('{"run_id": "', encoding="utf-8")

    # Succeeds (no uncaught traceback); the recompute signature is total.
    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))
    assert result.run_ids == [_RID]
    assert compute_dossier_signature(experiment_dir=experiment, run_id=_RID)
    # The raw (corrupt) sidecar bytes are still sealed as a source entry.
    assert "sidecar" in _sources_of(result.manifest)


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
    assert "fingerprints/0000000000000000.jsonl" in names  # cmd_sha[:16]-addressed ledger
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


# --- audited-source trail (notebook-audit T14) -------------------------------


def test_audited_run_seals_source_template_and_notebook_journal(
    journal_home: Path, experiment: Path
) -> None:
    """An audited run (sidecar echoes audited_source) seals the source .py + the
    template .py under ``audited-source`` and the attestation journal under
    ``notebook-journal``, with correct sha256s; audit_id enters identity."""
    import hashlib

    from hpc_agent.state.decision_journal import decisions_path

    audit_id = "pi-audit-001"
    (experiment / "src.py").write_bytes(b"# %%\n# hpc-audit-section: run\nx = 1\n")
    (experiment / "tpl.py").write_bytes(b"# %%\n# hpc-audit-section: run\ny = 2\n")
    echo = {"source": "src.py", "template": "tpl.py", "audit_id": audit_id}
    _sidecar(experiment, _RID, audited_source=echo)
    append_decision(
        experiment,
        scope_kind="notebook",
        scope_id=audit_id,
        block="notebook-sign-off",
        response="y",
        resolved={"audit_id": audit_id, "section": "run", "section_sha": "0" * 64},
    )

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    by_path = {e["path"]: e for e in result.manifest["entries"]}
    src_entry = by_path[f"runs/{_RID}/audited/source.py"]
    tpl_entry = by_path[f"runs/{_RID}/audited/template.py"]
    nb_entry = by_path[f"runs/{_RID}/notebook.decisions.jsonl"]
    assert src_entry["source"] == "audited-source"
    assert tpl_entry["source"] == "audited-source"
    assert nb_entry["source"] == "notebook-journal"
    # Correct integrity fingerprints over the sealed bytes.
    assert src_entry["sha256"] == hashlib.sha256((experiment / "src.py").read_bytes()).hexdigest()
    assert tpl_entry["sha256"] == hashlib.sha256((experiment / "tpl.py").read_bytes()).hexdigest()
    nb_bytes = decisions_path(experiment, "notebook", audit_id).read_bytes()
    assert nb_entry["sha256"] == hashlib.sha256(nb_bytes).hexdigest()
    # Entries keep the exact 4-key store-provenance shape (no meaning field).
    assert set(src_entry) == {"source", "path", "sha256", "bytes"}
    # audit_id projected into run identity (the opaque slug is identity).
    (proj,) = result.manifest["runs"]
    assert proj["audit_id"] == audit_id
    # No AUDIT gap — all three audit stores were present (other stores this
    # minimal seed omitted may gap; those are unrelated to T14).
    assert not [g for g in result.gaps if g["source"] in {"audited-source", "notebook-journal"}]


def test_audited_run_missing_template_records_a_gap_and_still_succeeds(
    journal_home: Path, experiment: Path
) -> None:
    """A declared audited file that is not on disk is a RECORDED gap, not a crash;
    the present files are still sealed and the bundle is still written."""
    audit_id = "pi-audit-002"
    (experiment / "src.py").write_bytes(b"# %%\n# hpc-audit-section: run\nx = 1\n")  # tpl absent
    echo = {"source": "src.py", "template": "missing_tpl.py", "audit_id": audit_id}
    _sidecar(experiment, _RID, audited_source=echo)

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    tpl_gaps = [g for g in result.gaps if g["source"] == "audited-source"]
    assert len(tpl_gaps) == 1
    assert "missing_tpl.py" in tpl_gaps[0]["note"]
    assert tpl_gaps[0]["run_id"] == _RID
    # The present source .py was still sealed.
    assert f"runs/{_RID}/audited/source.py" in {e["path"] for e in result.manifest["entries"]}
    assert zipfile.is_zipfile(result.archive_path)


def test_audited_run_seals_trusted_display_renders(journal_home: Path, experiment: Path) -> None:
    """F6: the trusted-display render files (``.hpc/renders/<audit_id>/``) are sealed
    under the ``renders`` store noun, so the dossier reproduces what-the-human-saw."""
    import hashlib

    from hpc_agent._kernel.contract.layout import RepoLayout

    audit_id = "pi-audit-003"
    (experiment / "src.py").write_bytes(b"# %%\n# hpc-audit-section: run\nx = 1\n")
    _sidecar(experiment, _RID, audited_source={"source": "src.py", "audit_id": audit_id})
    renders_dir = RepoLayout(experiment).hpc / "renders" / audit_id
    renders_dir.mkdir(parents=True, exist_ok=True)
    render_file = renders_dir / "run.abc123def456.md"
    render_file.write_bytes(b"<!-- hpc-render audit_id: pi-audit-003 -->\n\nbody\n")

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    by_path = {e["path"]: e for e in result.manifest["entries"]}
    entry = by_path[f"runs/{_RID}/renders/run.abc123def456.md"]
    assert entry["source"] == "renders"
    assert set(entry) == {"source", "path", "sha256", "bytes"}
    assert entry["sha256"] == hashlib.sha256(render_file.read_bytes()).hexdigest()
    assert not [g for g in result.gaps if g["source"] == "renders"]


def test_audited_run_missing_renders_records_a_gap_and_still_succeeds(
    journal_home: Path, experiment: Path
) -> None:
    """F6: an audited run with no renders on disk records a ``renders`` gap, never
    a crash — present-or-gap accounting."""
    audit_id = "pi-audit-004"
    (experiment / "src.py").write_bytes(b"# %%\n# hpc-audit-section: run\nx = 1\n")
    _sidecar(experiment, _RID, audited_source={"source": "src.py", "audit_id": audit_id})

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    r_gaps = [g for g in result.gaps if g["source"] == "renders"]
    assert len(r_gaps) == 1
    assert audit_id in r_gaps[0]["note"]
    assert r_gaps[0]["run_id"] == _RID


def test_non_audited_run_seals_no_audit_stores(journal_home: Path, experiment: Path) -> None:
    """A run whose sidecar echoes no audited_source seals no audit stores, records
    no audit gap, and leaks no audit_id into the identity projection — byte-for-
    byte the pre-T14 dossier shape."""
    _seed_full_run(experiment, _RID)

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    sources = set(_sources_of(result.manifest))
    assert "audited-source" not in sources
    assert "notebook-journal" not in sources
    assert not [g for g in result.gaps if g["source"] in {"audited-source", "notebook-journal"}]
    (proj,) = result.manifest["runs"]
    assert "audit_id" not in proj


# --- determinism-fingerprint ledger (T8) -------------------------------------


def test_fingerprint_ledger_is_sealed_byte_for_byte(journal_home: Path, experiment: Path) -> None:
    """A run whose identity has a fingerprint ledger seals it under the
    ``determinism-fingerprint`` noun as RAW BYTES — byte-identical round-trip out
    of the zip and a matching sha256 (never parsed, never re-serialized)."""
    import hashlib

    # A deliberately-torn ledger (a truncated JSON line) must still round-trip:
    # the bundler seals bytes, it never parses the JSONL.
    ledger_bytes = b'{"schema_version":1,"source":"double-canary","verdict":"auto_cle'
    _seed_full_run(experiment, _RID, fingerprint_bytes=ledger_bytes)

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    by_path = {e["path"]: e for e in result.manifest["entries"]}
    entry = by_path["fingerprints/0000000000000000.jsonl"]
    assert entry["source"] == "determinism-fingerprint"
    assert set(entry) == {"source", "path", "sha256", "bytes"}  # no meaning field
    assert entry["sha256"] == hashlib.sha256(ledger_bytes).hexdigest()
    assert entry["bytes"] == len(ledger_bytes)
    with zipfile.ZipFile(result.archive_path) as zf:
        got = zf.read("fingerprints/0000000000000000.jsonl")
    assert got == ledger_bytes  # opaque bytes — never parsed, never mutated
    # The on-disk ledger equals what was sealed.
    assert fingerprint_path(experiment, "0" * 64).read_bytes() == ledger_bytes
    assert not [g for g in result.gaps if g["source"] == "determinism-fingerprint"]


def test_missing_fingerprint_ledger_records_a_gap_and_still_succeeds(
    journal_home: Path, experiment: Path
) -> None:
    """A resolvable identity whose ledger is not on disk (no sample ever minted)
    records a ``determinism-fingerprint`` gap naming the run and still exports."""
    _seed_full_run(experiment, _RID, with_fingerprint=False)

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    fp_gaps = [g for g in result.gaps if g["source"] == "determinism-fingerprint"]
    assert len(fp_gaps) == 1
    assert fp_gaps[0]["run_id"] == _RID
    assert "0000000000000000" in fp_gaps[0]["note"]
    assert "determinism-fingerprint" not in _sources_of(result.manifest)
    assert zipfile.is_zipfile(result.archive_path)


def test_dry_signature_reflects_the_fingerprint_ledger_identically(
    journal_home: Path, experiment: Path
) -> None:
    """The one-seam property extends to the new noun: the dry signature and the
    exported archive seal the SAME ledger bytes and fingerprint identically."""
    _seed_full_run(experiment, _RID, scopes=["holdout"])

    sig = compute_dossier_signature(experiment, _RID)
    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    assert sig.bundle_sha256 == result.bundle_sha256
    assert sig.entries == result.manifest["entries"]
    # The ledger bytes made it into the seam's write_map exactly as the archive.
    key = "fingerprints/0000000000000000.jsonl"
    assert sig.write_map[key] == _FINGERPRINT_LEDGER_BYTES
    with zipfile.ZipFile(result.archive_path) as zf:
        assert zf.read(key) == sig.write_map[key]


def _write_conformance_ledger(experiment_dir: Path, reg_id: str, data: bytes) -> Path:
    """Write a registration-conformance ledger straight to disk (opaque to the bundler)."""
    base = experiment_dir / "_aggregated" / "_conformance"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{reg_id}.jsonl"
    path.write_bytes(data)
    return path


def test_conformance_ledger_is_sealed_byte_for_byte(journal_home: Path, experiment: Path) -> None:
    """A registration-conformance ledger seals under the ``live-conformance`` noun as
    RAW BYTES (C-dossier: a re-registration carries the live record that motivated
    it). A deliberately-torn ledger must round-trip byte-identical — never parsed."""
    import hashlib

    _seed_full_run(experiment, _RID)
    ledger_bytes = b'{"schema_version":1,"subject_kind":"conformance-observation","payload":{"rea'
    _write_conformance_ledger(experiment, "reg-sensor-7", ledger_bytes)

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    by_path = {e["path"]: e for e in result.manifest["entries"]}
    entry = by_path["conformance/reg-sensor-7.jsonl"]
    assert entry["source"] == "live-conformance"
    assert set(entry) == {"source", "path", "sha256", "bytes"}  # no meaning field
    assert entry["sha256"] == hashlib.sha256(ledger_bytes).hexdigest()
    with zipfile.ZipFile(result.archive_path) as zf:
        assert zf.read("conformance/reg-sensor-7.jsonl") == ledger_bytes  # opaque, never mutated


def test_absent_conformance_dir_seals_nothing_and_records_no_gap(
    journal_home: Path, experiment: Path
) -> None:
    """Absent-tolerant (unlike aggregated): no conformance dir → no entry, no gap."""
    _seed_full_run(experiment, _RID)
    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))
    assert "live-conformance" not in _sources_of(result.manifest)
    assert not [g for g in result.gaps if g["source"] == "live-conformance"]


def test_appending_a_sample_between_exports_changes_the_bundle_sha(
    journal_home: Path, experiment: Path
) -> None:
    """The DISCLOSED staleness consequence (design center 4 / drift-log item 5):
    every appended fingerprint sample moves the sealed bytes, so a re-export
    fingerprints differently — a registration's dossier leg reads stale until
    re-exported."""
    _seed_full_run(experiment, _RID)

    first = export_dossier(
        experiment_dir=experiment,
        spec=ExportDossierSpec(run_id=_RID, output_path=str(experiment / "a.zip")),
    )
    # Append one more sample line to the append-only ledger (the accrual the
    # design says moves the disclosure surface).
    ledger = fingerprint_path(experiment, "0" * 64)
    with ledger.open("ab") as fh:
        fh.write(b'{"schema_version":1,"source":"verify-reproduction","verdict":"auto_cleared"}\n')
    second = export_dossier(
        experiment_dir=experiment,
        spec=ExportDossierSpec(run_id=_RID, output_path=str(experiment / "b.zip")),
    )

    assert first.bundle_sha256 != second.bundle_sha256  # the sealed bytes moved
    # Idempotent again once the ledger is stable: a third export re-fingerprints
    # exactly the second.
    third = export_dossier(
        experiment_dir=experiment,
        spec=ExportDossierSpec(run_id=_RID, output_path=str(experiment / "c.zip")),
    )
    assert third.bundle_sha256 == second.bundle_sha256


# --- clean-reproduction recipe member (BR-4) ---------------------------------


def test_recipe_member_is_sealed_and_disclosed(journal_home: Path, experiment: Path) -> None:
    """The dossier carries the derived clean-reproduction recipe as a first-class
    sealed member: a ``recipe`` source entry with the exact 4-key store shape, the
    member in the zip at ``recipe/recipe.json``, and a manifest ``recipe`` block
    disclosing its own provenance (present / member_path / extracted_at / seed)."""
    _seed_full_run(experiment, _RID, scopes=["holdout"])

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    by_path = {e["path"]: e for e in result.manifest["entries"]}
    entry = by_path["recipe/recipe.json"]
    assert entry["source"] == "recipe"
    assert set(entry) == {"source", "path", "sha256", "bytes"}  # no meaning field

    # The member is in the zip and parses to a recipe walked back from the run.
    with zipfile.ZipFile(result.archive_path) as zf:
        member = zf.read("recipe/recipe.json")
    recipe = json.loads(member)
    assert recipe["seed_kind"] == "run"
    assert recipe["seed_ref"] == _RID
    assert len(recipe["recipe_signature"]) == 64

    # The manifest's recipe block discloses the recipe's OWN provenance.
    block = result.manifest["recipe"]
    assert block["present"] is True
    assert block["member_path"] == "recipe/recipe.json"
    assert block["seed"] == {"kind": "run", "ref": _RID}
    assert block["extracted_at"] == result.manifest["generated_at"]
    assert block["note"] is None
    assert result.manifest["dossier_schema_version"] == 2


def test_recipe_member_equals_a_direct_extract_recipe_on_the_same_seed(
    journal_home: Path, experiment: Path
) -> None:
    """Parity pin: the recipe sealed INSIDE the dossier equals a direct
    extract-recipe run on the same seed — the dossier composes the shipped walk,
    it never forks a second recipe."""
    from hpc_agent._wire.queries.extract_recipe import ExtractRecipeInput
    from hpc_agent.ops.extract_recipe import extract_recipe

    _seed_full_run(experiment, _RID, scopes=["holdout"])

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))
    with zipfile.ZipFile(result.archive_path) as zf:
        sealed = json.loads(zf.read("recipe/recipe.json"))

    direct = extract_recipe(experiment, spec=ExtractRecipeInput(run_id=_RID))
    assert sealed == direct


def test_recipe_member_rides_the_seal_and_a_tamper_is_caught(
    journal_home: Path, experiment: Path
) -> None:
    """The recipe member IS part of ``bundle_sha256`` (the seal), matching every
    other member's discipline: its entry sha256 binds the sealed bytes, and a
    tampered recipe re-hashes to a different sha — so seal verification catches
    it, and flipping the recipe entry's sha changes the whole bundle signature."""
    import hashlib

    from hpc_agent.ops.provenance_manifest import manifest_signature

    _seed_full_run(experiment, _RID, scopes=["holdout"])

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))
    entries = result.manifest["entries"]
    (recipe_entry,) = [e for e in entries if e["source"] == "recipe"]

    # The entry sha256 binds the sealed member bytes (integrity binding).
    with zipfile.ZipFile(result.archive_path) as zf:
        member = zf.read("recipe/recipe.json")
    assert recipe_entry["sha256"] == hashlib.sha256(member).hexdigest()
    # A tamper changes the member sha — verification against the sealed sha fails.
    assert hashlib.sha256(member + b" tampered").hexdigest() != recipe_entry["sha256"]

    # And the recipe entry is IN the bundle_sha256 pre-image: flipping its sha
    # changes the whole bundle signature (the member is inside the seal).
    assert manifest_signature(entries) == result.bundle_sha256  # type: ignore[arg-type]
    tampered = [dict(e) for e in entries]
    for e in tampered:
        if e["source"] == "recipe":
            e["sha256"] = "0" * 64
    assert manifest_signature(tampered) != result.bundle_sha256  # type: ignore[arg-type]


def test_recipe_extraction_failure_degrades_disclosed_and_never_blocks_export(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disclosure-not-gate: an extract-recipe walk that raises NEVER blocks the
    export — the dossier is still written, no recipe member is sealed, and a
    ``recipe`` gap + a manifest recipe block with present=false say WHY."""
    import hpc_agent.ops.extract_recipe as recipe_mod

    _seed_full_run(experiment, _RID, scopes=["holdout"])

    def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated recipe walk failure")

    monkeypatch.setattr(recipe_mod, "extract_recipe", _boom)

    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    # Export still succeeded and the archive is intact.
    assert zipfile.is_zipfile(result.archive_path)
    # No recipe member sealed; a disclosed recipe gap instead.
    assert "recipe" not in _sources_of(result.manifest)
    recipe_gaps = [g for g in result.gaps if g["source"] == "recipe"]
    assert len(recipe_gaps) == 1
    assert recipe_gaps[0]["run_id"] == _RID
    assert "simulated recipe walk failure" in recipe_gaps[0]["note"]
    # The manifest recipe block discloses the absence + the reason.
    block = result.manifest["recipe"]
    assert block["present"] is False
    assert block["member_path"] is None
    assert block["note"] and "simulated recipe walk failure" in block["note"]


def test_recipe_member_is_additive_existing_stores_unaffected(
    journal_home: Path, experiment: Path
) -> None:
    """Back-compat: the recipe member is purely additive — every pre-BR-4 store
    still seals exactly as before alongside the new ``recipe`` member, and the
    seam still equals the export byte-for-byte with the recipe inside."""
    _seed_full_run(experiment, _RID, scopes=["holdout"])

    sig = compute_dossier_signature(experiment, _RID)
    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    sources = set(_sources_of(result.manifest))
    # The prior stores are all still present.
    assert {"sidecar", "decision-journal", "briefs", "journal-record", "aggregated"} <= sources
    # The recipe is sealed in BOTH the dry seam and the export (one-seam property
    # extends to the new member).
    assert "recipe" in {e["source"] for e in sig.entries}
    assert sig.bundle_sha256 == result.bundle_sha256
    assert sig.entries == result.manifest["entries"]


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


# --- compute_dossier_signature — the ONE signature seam (T3) ------------------


def test_dry_signature_equals_exported_bundle_sha_byte_for_byte(
    journal_home: Path, experiment: Path
) -> None:
    """The dry seam's ``bundle_sha256`` is byte-for-byte the exported archive's
    ``bundle_sha256`` (and the manifest's) — export routes through the seam, so
    there is never a second signature definition (the enforcement row)."""
    _seed_full_run(experiment, _RID, scopes=["holdout"])

    sig = compute_dossier_signature(experiment, _RID)
    result = export_dossier(experiment_dir=experiment, spec=ExportDossierSpec(run_id=_RID))

    # The seam's signature IS the exported one, char-for-char.
    assert sig.bundle_sha256 == result.bundle_sha256
    assert sig.bundle_sha256 == result.manifest["bundle_sha256"]
    # And the pre-image is identical: the exact same path-sorted entries list.
    assert sig.entries == result.manifest["entries"]
    # The bundle sha the archive EMBEDS matches too (read back from the zip).
    with zipfile.ZipFile(result.archive_path) as zf:
        embedded = json.loads(zf.read("manifest.json"))
    assert embedded["bundle_sha256"] == sig.bundle_sha256


def test_dry_signature_with_lineage_matches_export(journal_home: Path, experiment: Path) -> None:
    """include_lineage widens the seam identically to export — same run set, same
    signature."""
    root = "20260101-000001-rrrrrrr"
    child = "20260101-000002-cccccc"
    _seed_full_run(experiment, root, scopes=["embargo"])
    _seed_full_run(experiment, child, scopes=["holdout"], supersedes=root)

    sig = compute_dossier_signature(experiment, child, include_lineage=True)
    result = export_dossier(
        experiment_dir=experiment,
        spec=ExportDossierSpec(run_id=child, include_lineage=True),
    )

    assert sig.run_ids == [child, root] == result.run_ids
    assert sig.bundle_sha256 == result.bundle_sha256
    assert sig.entries == result.manifest["entries"]


def test_dry_call_writes_nothing(journal_home: Path, experiment: Path) -> None:
    """The seam has NO side effects: no ``.zip``, no ``manifest.json``, no
    ``_dossier/`` dir — only the bytes it read to compute the signature."""
    _seed_full_run(experiment, _RID, scopes=["holdout"])
    before = {p for p in experiment.rglob("*") if p.is_file()}

    sig = compute_dossier_signature(experiment, _RID)

    after = {p for p in experiment.rglob("*") if p.is_file()}
    assert after == before  # not one new file on disk
    assert not (experiment / "_dossier").exists()  # the default archive dir never appears
    # The bytes still made it into the in-memory write_map (the zip writer's
    # input), keyed by archive path — nothing was written, everything gathered.
    assert sig.write_map
    assert set(sig.write_map) == {e["path"] for e in sig.entries}
    assert sig.bundle_sha256


def test_store_edit_between_dry_calls_changes_the_signature(
    journal_home: Path, experiment: Path
) -> None:
    """A store edited between two dry gathers changes ``bundle_sha256`` — the
    recompute-lock property R2 relies on: a moved sealed store is caught."""
    _seed_full_run(experiment, _RID, scopes=["holdout"])

    first = compute_dossier_signature(experiment, _RID)
    # Move a sealed store out from under the (already-exported) evidence.
    (experiment / "_aggregated" / f"{_RID}.json").write_bytes(b'{"reduced": false, "moved": 1}')
    second = compute_dossier_signature(experiment, _RID)

    assert first.bundle_sha256 != second.bundle_sha256
    # Idempotent when nothing moves: a third identical gather re-fingerprints
    # exactly the second.
    third = compute_dossier_signature(experiment, _RID)
    assert third.bundle_sha256 == second.bundle_sha256


def test_dry_signature_missing_run_raises(journal_home: Path, experiment: Path) -> None:
    """The seam carries the same missing-run guard as export — no sidecar and no
    journal record is a SpecInvalid, not an empty signature."""
    with pytest.raises(errors.SpecInvalid):
        compute_dossier_signature(experiment, "20260101-999999-nope000")
