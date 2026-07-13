"""T10: the sidecar ``packs`` echo + the dossier ``pack-manifest`` / ``pack-journal``
store nouns.

The run sidecar gains an opaque ``packs`` echo (one ``{pack, version, sha,
manifest}`` per bound pack the experiment opted into), additive and
byte-identical when absent. ``export-dossier`` seals each echoed pack's manifest
file + decision journal as RAW BYTES under two new store nouns, absent-tolerant
(gaps, never a crash), never parsing them. Toy-domain vocabulary only.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent._kernel.contract.layout import RepoLayout
from hpc_agent._wire.actions.export_dossier import ExportDossierSpec
from hpc_agent.ops.export_dossier import (
    DOSSIER_SOURCES,
    compute_dossier_signature,
    export_dossier,
)
from hpc_agent.state.pack_declarations import resolve_pack_echoes
from hpc_agent.state.pack_receipts import PACK_BIND_BLOCK
from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

_PACK = "toy-widgets"
_MANIFEST_REL = "packs/toy/manifest.json"


def _raw_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_pack(experiment: Path) -> tuple[str, dict[str, Any], bytes]:
    """Write a toy pack; return (manifest_sha, manifest, manifest_bytes)."""
    pack_root = experiment / "packs" / "toy"
    pack_root.mkdir(parents=True, exist_ok=True)
    reader_blob = json.dumps(["widgets.load_widget"]).encode("utf-8")
    (pack_root / "readers.json").write_bytes(reader_blob)
    manifest = {
        "name": _PACK,
        "version": "1.0.0",
        "files": [{"path": "readers.json", "sha256": _raw_sha(reader_blob)}],
        "seams": {"reader_calls": "readers.json"},
    }
    manifest_blob = json.dumps(manifest).encode("utf-8")
    (pack_root / "manifest.json").write_bytes(manifest_blob)
    return _raw_sha(manifest_blob), manifest, manifest_blob


def _write_bind(experiment: Path, manifest: dict[str, Any], manifest_sha: str) -> bytes:
    record = {
        "block": PACK_BIND_BLOCK,
        "resolved": {
            "pack": manifest["name"],
            "version": manifest["version"],
            "manifest_sha": manifest_sha,
            "files": manifest["files"],
            "seams": list(manifest["seams"]),
        },
    }
    path = RepoLayout(experiment).hpc / "packs" / f"{manifest['name']}.decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = (json.dumps(record) + "\n").encode("utf-8")
    path.write_bytes(blob)
    return blob


def _write_interview(experiment: Path, *, opted_in: bool = True) -> None:
    doc: dict[str, Any] = {"goal": "toy"}
    if opted_in:
        doc["packs"] = [{"pack": _PACK, "manifest": _MANIFEST_REL, "receipt_bindings": []}]
    (experiment / "interview.json").write_text(json.dumps(doc), encoding="utf-8")


def _write_sidecar(experiment: Path, run_id: str, *, packs: list[dict[str, Any]] | None) -> None:
    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{task_id}",
        task_count=1,
        tasks_py_sha="",
        packs=packs,
    )


# ── resolve_pack_echoes ──────────────────────────────────────────────────────


def test_echoes_empty_when_not_opted_in(tmp_path: Path) -> None:
    assert resolve_pack_echoes(tmp_path) == []


def test_echoes_present_for_bound_pack(tmp_path: Path) -> None:
    manifest_sha, manifest, _ = _build_pack(tmp_path)
    _write_bind(tmp_path, manifest, manifest_sha)
    _write_interview(tmp_path)
    echoes = resolve_pack_echoes(tmp_path)
    assert echoes == [
        {"pack": _PACK, "version": "1.0.0", "sha": manifest_sha, "manifest": _MANIFEST_REL}
    ]


def test_echoes_fail_open_on_manifest_drift(tmp_path: Path) -> None:
    """A manifest drifted from its bind → no echo (fail-open, never raises)."""
    manifest_sha, manifest, _ = _build_pack(tmp_path)
    _write_bind(tmp_path, manifest, manifest_sha)
    _write_interview(tmp_path)
    (tmp_path / "packs" / "toy" / "manifest.json").write_text(
        json.dumps(manifest) + "  ", encoding="utf-8"
    )
    assert resolve_pack_echoes(tmp_path) == []


# ── sidecar byte-identity ────────────────────────────────────────────────────


def test_sidecar_omits_packs_when_absent_byte_identical(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "pi-00000001", packs=None)
    sidecar = read_run_sidecar(tmp_path, "pi-00000001")
    raw = (RepoLayout(tmp_path).runs / "pi-00000001.json").read_text(encoding="utf-8")
    assert "packs" not in raw  # the key is omitted on write (byte-identical)
    assert sidecar.get("packs") is None


def test_sidecar_carries_packs_when_present(tmp_path: Path) -> None:
    echo = [{"pack": _PACK, "version": "1.0.0", "sha": "d" * 64, "manifest": _MANIFEST_REL}]
    _write_sidecar(tmp_path, "pi-00000002", packs=echo)
    assert read_run_sidecar(tmp_path, "pi-00000002")["packs"] == echo


# ── dossier sealing ──────────────────────────────────────────────────────────


def _zip_bytes(archive_path: str, member: str) -> bytes:
    with zipfile.ZipFile(archive_path) as zf:
        return zf.read(member)


def _entries_by_source(manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for e in manifest["entries"]:
        out.setdefault(e["source"], []).append(e)
    return out


def test_dossier_seals_pack_manifest_and_journal_byte_equal(tmp_path: Path) -> None:
    run_id = "pi-0000000a"
    manifest_sha, manifest, manifest_blob = _build_pack(tmp_path)
    journal_blob = _write_bind(tmp_path, manifest, manifest_sha)
    _write_interview(tmp_path)
    echo = resolve_pack_echoes(tmp_path)
    _write_sidecar(tmp_path, run_id, packs=echo)

    res = export_dossier(experiment_dir=tmp_path, spec=ExportDossierSpec(run_id=run_id))
    by_source = _entries_by_source(res.manifest)

    assert "pack-manifest" in by_source
    assert "pack-journal" in by_source
    man_entry = by_source["pack-manifest"][0]
    jrn_entry = by_source["pack-journal"][0]
    assert man_entry["sha256"] == _raw_sha(manifest_blob)
    assert jrn_entry["sha256"] == _raw_sha(journal_blob)
    # Byte-equal round trip out of the sealed zip.
    assert _zip_bytes(res.archive_path, man_entry["path"]) == manifest_blob
    assert _zip_bytes(res.archive_path, jrn_entry["path"]) == journal_blob


def test_dossier_records_gaps_when_pack_stores_missing(tmp_path: Path) -> None:
    """A sidecar packs echo whose manifest + journal are absent → gaps, no crash."""
    run_id = "pi-0000000b"
    echo = [{"pack": _PACK, "version": "1.0.0", "sha": "d" * 64, "manifest": _MANIFEST_REL}]
    _write_sidecar(tmp_path, run_id, packs=echo)  # no pack files / journal on disk

    res = export_dossier(experiment_dir=tmp_path, spec=ExportDossierSpec(run_id=run_id))
    gap_sources = {g["source"] for g in res.gaps}
    assert "pack-manifest" in gap_sources
    assert "pack-journal" in gap_sources
    # No pack entries were sealed.
    assert not any(e["source"].startswith("pack-") for e in res.manifest["entries"])


def test_dossier_pack_free_run_seals_nothing_no_gap(tmp_path: Path) -> None:
    run_id = "pi-0000000c"
    _write_sidecar(tmp_path, run_id, packs=None)
    res = export_dossier(experiment_dir=tmp_path, spec=ExportDossierSpec(run_id=run_id))
    assert not any(e["source"].startswith("pack-") for e in res.manifest["entries"])
    assert not any(g["source"].startswith("pack-") for g in res.gaps)


def test_dry_signature_equals_export_bundle_sha(tmp_path: Path) -> None:
    """The dry gather (compute_dossier_signature) and export share ONE signature,
    including the pack stores."""
    run_id = "pi-0000000d"
    manifest_sha, manifest, _ = _build_pack(tmp_path)
    _write_bind(tmp_path, manifest, manifest_sha)
    _write_interview(tmp_path)
    _write_sidecar(tmp_path, run_id, packs=resolve_pack_echoes(tmp_path))

    sig = compute_dossier_signature(tmp_path, run_id)
    res = export_dossier(experiment_dir=tmp_path, spec=ExportDossierSpec(run_id=run_id))
    assert res.bundle_sha256 == sig.bundle_sha256
    assert {"pack-manifest", "pack-journal"} <= {e["source"] for e in sig.entries}


def test_new_pack_nouns_are_in_closed_vocabulary() -> None:
    assert {"pack-manifest", "pack-journal"} <= DOSSIER_SOURCES


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
