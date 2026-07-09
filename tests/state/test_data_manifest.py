"""Unit tests for the data-manifest state layer (``state/data_manifest.py``).

Toy fixtures only — text files and random bytes, never a parquet, never any
domain vocabulary (the agnosticism boundary test, held in the tests too).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hpc_agent.state import data_manifest as dm


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)


def _make_inputs(root: Path) -> None:
    _write(root / "data" / "a.txt", "alpha\n")
    _write(root / "data" / "b.bin", os.urandom(64))
    _write(root / "data" / "sub" / "c.txt", "gamma\n")


# ── mint ──────────────────────────────────────────────────────────────────────


def test_mint_records_sha_size_for_every_file(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    manifest = dm.mint_manifest(tmp_path, ["data"])
    files = manifest["files"]
    assert set(files) == {"data/a.txt", "data/b.bin", "data/sub/c.txt"}
    for entry in files.values():
        assert isinstance(entry["sha256"], str) and len(entry["sha256"]) == 64
        assert isinstance(entry["size"], int)
    # a.txt sha is the RAW-byte hash of the exact bytes on disk
    assert files["data/a.txt"]["sha256"] == dm.file_sha256(tmp_path / "data" / "a.txt")


def test_mint_writes_manifest_to_default_home(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    assert (tmp_path / ".hpc" / "data_manifest.json").is_file()


def test_output_path_override(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"], output_path="custom/manifest.json")
    assert (tmp_path / "custom" / "manifest.json").is_file()
    assert dm.read_manifest(tmp_path, output_path="custom/manifest.json") is not None


def test_hpc_dir_never_records_itself(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    # a stray file inside .hpc must not enter a manifest rooted at the repo
    _write(tmp_path / ".hpc" / "junk.txt", "x")
    manifest = dm.mint_manifest(tmp_path, ["."])
    assert not any(rel.startswith(".hpc/") for rel in manifest["files"])


# ── doc-sha stability ─────────────────────────────────────────────────────────


def test_doc_sha_is_stable_across_remint_of_unchanged_data(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    first = dm.mint_manifest(tmp_path, ["data"])["manifest_doc_sha"]
    second = dm.mint_manifest(tmp_path, ["data"])["manifest_doc_sha"]
    assert first == second


def test_doc_sha_ignores_key_order() -> None:
    a = {"z.txt": {"sha256": "1", "size": 1}, "a.txt": {"sha256": "2", "size": 2}}
    b = {"a.txt": {"sha256": "2", "size": 2}, "z.txt": {"sha256": "1", "size": 1}}
    assert dm.manifest_doc_sha(a) == dm.manifest_doc_sha(b)


def test_doc_sha_moves_when_bytes_change(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    first = dm.mint_manifest(tmp_path, ["data"])["manifest_doc_sha"]
    _write(tmp_path / "data" / "a.txt", "ALPHA CHANGED\n")
    second = dm.mint_manifest(tmp_path, ["data"])["manifest_doc_sha"]
    assert first != second


# ── cache fast-path ───────────────────────────────────────────────────────────


def test_remint_uses_cache_and_does_not_rehash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])  # populates the cache

    calls: list[str] = []
    real = dm.file_sha256

    def _counting(path: Path) -> str:
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(dm, "file_sha256", _counting)
    dm.mint_manifest(tmp_path, ["data"])  # unchanged → all cache hits
    assert calls == [], f"unchanged files were re-hashed: {calls}"


def test_changed_file_is_rehashed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    _write(tmp_path / "data" / "a.txt", "changed content, new size\n")

    calls: list[str] = []
    real = dm.file_sha256

    def _counting(path: Path) -> str:
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(dm, "file_sha256", _counting)
    dm.mint_manifest(tmp_path, ["data"])
    assert any("a.txt" in c for c in calls)


# ── built_by opaque carry ─────────────────────────────────────────────────────


def test_built_by_is_carried_opaquely_across_remint(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    # a caller hand-annotates built_by on the manifest (opaque free text)
    path = dm.manifest_path(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["files"]["data/a.txt"]["built_by"] = "nightly-ingest v3 // arbitrary text"
    path.write_text(json.dumps(doc), encoding="utf-8")

    remint = dm.mint_manifest(tmp_path, ["data"])
    assert remint["files"]["data/a.txt"]["built_by"] == "nightly-ingest v3 // arbitrary text"


# ── journaled mint ────────────────────────────────────────────────────────────


def test_mint_is_journaled_with_doc_sha(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    m1 = dm.mint_manifest(tmp_path, ["data"])
    _write(tmp_path / "data" / "a.txt", "v2\n")
    m2 = dm.mint_manifest(tmp_path, ["data"])

    lines = dm.journal_path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    recs = [json.loads(x) for x in lines]
    assert [r["action"] for r in recs] == ["mint", "mint"]
    assert recs[0]["manifest_doc_sha"] == m1["manifest_doc_sha"]
    assert recs[1]["manifest_doc_sha"] == m2["manifest_doc_sha"]
    assert recs[0]["file_count"] == 3


# ── drift computation ─────────────────────────────────────────────────────────


def test_drift_all_match_after_mint(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    report = dm.compute_drift(tmp_path)
    assert not report.unmanifested
    assert report.counts == {"matched": 3, "drifted": 0, "new": 0, "missing": 0}
    assert not report.has_drift


def test_drift_detects_changed_new_and_missing(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    _write(tmp_path / "data" / "a.txt", "quietly rebuilt bytes\n")  # drifted
    (tmp_path / "data" / "b.bin").unlink()  # missing
    _write(tmp_path / "data" / "d.txt", "new file\n")  # new

    report = dm.compute_drift(tmp_path)
    assert report.drifted == ("data/a.txt",)
    assert report.missing == ("data/b.bin",)
    assert report.new == ("data/d.txt",)
    assert report.matched == ("data/sub/c.txt",)
    assert report.has_drift


def test_drift_unmanifested_when_no_manifest(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    report = dm.compute_drift(tmp_path)
    assert report.unmanifested


def test_compute_drift_writes_nothing(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    _write(tmp_path / "data" / "a.txt", "changed\n")
    before = {p: p.stat().st_mtime_ns for p in (tmp_path / ".hpc").iterdir()}
    dm.compute_drift(tmp_path)  # read-only
    after = {p: p.stat().st_mtime_ns for p in (tmp_path / ".hpc").iterdir()}
    assert before == after


# ── declared-roots resolution ─────────────────────────────────────────────────


def test_declared_input_roots_reads_audited_source(tmp_path: Path) -> None:
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": {"input_roots": ["data", "vendor"]}}),
        encoding="utf-8",
    )
    assert dm.declared_input_roots(tmp_path) == ["data", "vendor"]


def test_declared_input_roots_none_when_absent(tmp_path: Path) -> None:
    assert dm.declared_input_roots(tmp_path) is None


def test_declared_input_roots_none_when_empty_list(tmp_path: Path) -> None:
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": {"input_roots": []}}), encoding="utf-8"
    )
    assert dm.declared_input_roots(tmp_path) is None


# --- P-S1 one-definition pin (the re-point to determinism.canonical_sha) ------


def test_manifest_doc_sha_routes_to_canonical_sha_byte_for_byte() -> None:
    """P-S1: ``manifest_doc_sha`` IS ``determinism.canonical_sha`` over the records.

    Pins the re-point byte-for-byte so the sibling copy can never silently
    diverge from the ONE harness-contract canonicalization again.
    """
    from hpc_agent.state import determinism

    records = {
        "data/train.csv": {"sha256": "ab" * 32, "size": 10, "built_by": "etl v1"},
        "data/labels.parquet": {"sha256": "cd" * 32, "size": 99},
    }
    assert dm.manifest_doc_sha(records) == determinism.canonical_sha(records)
    # An empty record map is a legal (0-file) manifest identity.
    assert dm.manifest_doc_sha({}) == determinism.canonical_sha({})
