"""Behaviour-pinning coverage for the DATA-IDENTITY leg (``state/data_manifest.py``).

Companion to ``test_data_manifest.py`` (mint / doc-sha / cache / drift basics) and
to the record-substrate ``test_run_record_coverage.py``. The 2026-07-17 mutation
triage found the data-identity leg — the honest ``None``-without-declaration and
the fresh-recompute discrimination that stops a tampered manifest field asserting
a false identity — covered-but-UNASSERTED: a boundary / operator / return mutation
would survive the suite. Data identity is the leg that turns quiet corruption from
invisible to attributed, so a silent bug here is a silent reproducibility failure.

Each test KILLS a specific surviving mutant; the docstring names it. Toy fixtures
only — text + random bytes, never a parquet, never domain vocabulary (the
agnosticism boundary test, held in the tests too). Basics already batteried in
``test_data_manifest.py`` are NOT duplicated — this file covers the GAPS.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from hpc_agent.state import data_manifest as dm
from hpc_agent.state import determinism


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


def _declare(root: Path, roots: list[str], *, at_hpc: bool = False) -> None:
    rel = ".hpc/interview.json" if at_hpc else "interview.json"
    _write(root / rel, json.dumps({"audited_source": {"input_roots": roots}}))


# ── declared_input_roots: the wrong-shape tolerances (exact interview field path) ─


def test_declared_input_roots_filters_non_string_and_empty_items(tmp_path: Path) -> None:
    """The declaration is cleaned to NON-EMPTY strings only — non-str and blank
    entries are dropped, and a list that reduces to nothing reads as 'declared
    nothing' → None. Kills the ``isinstance(r, str) and r`` item filter."""
    _declare(tmp_path, ["data", "", "vendor"])
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": {"input_roots": ["data", "", None, 5, "vendor"]}}),
        encoding="utf-8",
    )
    assert dm.declared_input_roots(tmp_path) == ["data", "vendor"]

    # A list of ONLY junk reduces to empty → None (not [] , not a crash).
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": {"input_roots": ["", None, 7]}}), encoding="utf-8"
    )
    assert dm.declared_input_roots(tmp_path) is None


def test_declared_input_roots_none_when_block_not_a_dict(tmp_path: Path) -> None:
    """A present-but-malformed ``audited_source`` (not an object) reads as 'not
    declared' → None, never raises. Kills the ``isinstance(block, dict)`` guard."""
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": "data,vendor"}), encoding="utf-8"
    )
    assert dm.declared_input_roots(tmp_path) is None


def test_declared_input_roots_none_when_input_roots_not_a_list(tmp_path: Path) -> None:
    """``input_roots`` as a scalar (a common hand-edit slip) reads as 'not declared'
    → None. Kills the ``isinstance(roots, list)`` guard (a str is iterable, so a
    dropped guard would silently split 'data' into per-character roots)."""
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": {"input_roots": "data"}}), encoding="utf-8"
    )
    assert dm.declared_input_roots(tmp_path) is None


def test_declared_input_roots_reads_defensive_hpc_path(tmp_path: Path) -> None:
    """With only ``.hpc/interview.json`` present (the detect_entry_point
    convention), the roots are still found. Pins the defensive candidate the
    ``iter_interview_docs`` skeleton walks after the canonical root."""
    _declare(tmp_path, ["data"], at_hpc=True)
    assert dm.declared_input_roots(tmp_path) == ["data"]


# ── data_identity: the honest null vs. captured discrimination (priority) ──────


def test_data_identity_none_without_declared_roots(tmp_path: Path) -> None:
    """No declaration → None (data identity NOT captured, honestly disclosed, never
    fabricated) EVEN when a manifest exists on disk. Kills the
    ``if declared_input_roots(...) is None: return None`` guard — the leg must not
    assert an identity the experiment never declared inputs for."""
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])  # a manifest exists...
    # ...but no interview.json declares any input roots.
    assert dm.declared_input_roots(tmp_path) is None
    assert dm.data_identity(tmp_path) is None


def test_data_identity_none_when_no_manifest_minted(tmp_path: Path) -> None:
    """Roots declared but no manifest yet → None (unknown, not captured). Kills the
    ``if manifest is None: return None`` guard."""
    _make_inputs(tmp_path)
    _declare(tmp_path, ["data"])
    assert dm.read_manifest(tmp_path) is None
    assert dm.data_identity(tmp_path) is None


def test_data_identity_none_when_manifest_has_no_files(tmp_path: Path) -> None:
    """A manifest that recorded ZERO files (declared root absent/empty) → None: a
    0-file identity is not a usable data fingerprint. Kills the ``not files``
    empty-map guard."""
    _declare(tmp_path, ["data"])  # declared, but no data/ dir exists on disk
    manifest = dm.mint_manifest(tmp_path, ["data"])
    assert manifest["files"] == {}
    assert dm.data_identity(tmp_path) is None


def test_data_identity_recomputed_fresh_ignoring_a_tampered_stored_field(tmp_path: Path) -> None:
    """The captured identity is ``manifest_doc_sha`` recomputed FRESH from
    ``manifest['files']`` — NOT the stored ``manifest_doc_sha`` field. A tampered
    stored field cannot assert a false identity. Kills a mutant that returns the
    stored field instead of recomputing."""
    _make_inputs(tmp_path)
    _declare(tmp_path, ["data"])
    dm.mint_manifest(tmp_path, ["data"])

    path = dm.manifest_path(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    honest = dm.manifest_doc_sha(doc["files"])
    # Vandalize the stored field to a plausible-looking but WRONG sha.
    doc["manifest_doc_sha"] = "f" * 64
    path.write_text(json.dumps(doc), encoding="utf-8")

    identity = dm.data_identity(tmp_path)
    assert identity == honest  # recomputed from files, not the tampered field
    assert identity != "f" * 64


def test_data_identity_moves_iff_a_declared_file_changes(tmp_path: Path) -> None:
    """The captured identity is stable across a re-mint of unchanged data and MOVES
    exactly when a declared-input file's bytes change (the quiet-corruption class).
    Pins the drift-sensitivity + stability contract the fingerprint leg relies on."""
    _make_inputs(tmp_path)
    _declare(tmp_path, ["data"])
    dm.mint_manifest(tmp_path, ["data"])
    id1 = dm.data_identity(tmp_path)
    assert id1 is not None

    # Re-mint of UNCHANGED data → identity is stable.
    dm.mint_manifest(tmp_path, ["data"])
    assert dm.data_identity(tmp_path) == id1

    # A file's bytes change (same name, silently rebuilt) → re-mint → identity moves.
    _write(tmp_path / "data" / "a.txt", "quietly rebuilt bytes\n")
    dm.mint_manifest(tmp_path, ["data"])
    id2 = dm.data_identity(tmp_path)
    assert id2 is not None and id2 != id1


def test_data_identity_honors_output_path_override(tmp_path: Path) -> None:
    """The manifest-vs-sidecar seam: the same ``output_path`` the sidecar's
    ``data_manifest_sha`` is computed against resolves the identity. A default-path
    read finds nothing; the override read finds the minted manifest."""
    _make_inputs(tmp_path)
    _declare(tmp_path, ["data"])
    dm.mint_manifest(tmp_path, ["data"], output_path="custom/manifest.json")
    assert dm.data_identity(tmp_path) is None  # nothing at the default path
    got = dm.data_identity(tmp_path, output_path="custom/manifest.json")
    assert got is not None
    minted = dm.read_manifest(tmp_path, output_path="custom/manifest.json")
    assert minted is not None
    assert got == dm.manifest_doc_sha(minted["files"])


# ── _iter_files: root-is-a-file, non-existent root, deterministic order ────────


def test_iter_files_root_may_be_a_single_file(tmp_path: Path) -> None:
    """A declared root may be a FILE, not a directory — it contributes exactly that
    one file. Kills the ``base.is_file()`` branch (its removal would drop
    file-shaped roots entirely)."""
    _write(tmp_path / "data" / "a.txt", "alpha\n")
    manifest = dm.mint_manifest(tmp_path, ["data/a.txt"])
    assert set(manifest["files"]) == {"data/a.txt"}


def test_iter_files_nonexistent_root_contributes_nothing_no_crash(tmp_path: Path) -> None:
    """A declared root that does not exist yields no records and never raises — it
    surfaces later as ``missing`` drift, not a crash. Kills the
    ``if not base.is_dir(): continue`` guard."""
    _write(tmp_path / "data" / "a.txt", "alpha\n")
    manifest = dm.mint_manifest(tmp_path, ["data", "ghost_dir_that_is_absent"])
    assert set(manifest["files"]) == {"data/a.txt"}


# ── build_records: prior tolerance for the opaque built_by carry ──────────────


def test_build_records_tolerates_prior_without_files(tmp_path: Path) -> None:
    """``build_records`` with a prior manifest that carries no ``files`` map (or a
    non-dict prior) builds cleanly and omits ``built_by`` — the opaque carry is
    skipped, not crashed. Kills the ``isinstance(prior_files, dict)`` fallback."""
    _write(tmp_path / "data" / "a.txt", "alpha\n")
    records, _cache = dm.build_records(tmp_path, ["data"], prior={"no_files_key": 1})
    assert set(records) == {"data/a.txt"}
    assert "built_by" not in records["data/a.txt"]
    # A non-dict prior is tolerated too.
    records2, _c2 = dm.build_records(tmp_path, ["data"], prior=None)
    assert set(records2) == {"data/a.txt"}


# ── DriftReport: has_drift is drifted|missing ONLY (not new) ───────────────────


def test_drift_report_has_drift_excludes_new_only(tmp_path: Path) -> None:
    """``has_drift`` is the needs-attention class: a TRACKED file changed or
    vanished. A brand-NEW untracked file alone is NOT drift. Kills a mutant that
    widens ``has_drift`` to include ``new`` (which would raise a false alarm every
    time an unrelated file lands under a declared root)."""
    new_only = dm.DriftReport(unmanifested=False, matched=("x",), new=("fresh.txt",))
    assert new_only.has_drift is False
    assert dm.DriftReport(unmanifested=False, drifted=("x",)).has_drift is True
    assert dm.DriftReport(unmanifested=False, missing=("x",)).has_drift is True


# ── compute_drift / read_manifest: malformed-doc tolerances ───────────────────


def test_compute_drift_tolerates_non_dict_recorded_files(tmp_path: Path) -> None:
    """A manifest whose ``files`` is not a dict is treated as an empty recorded set
    — every on-disk file reads as ``new``, nothing crashes. Kills the
    ``isinstance(recorded, dict)`` guard."""
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    path = dm.manifest_path(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["files"] = "corrupted-not-a-dict"
    path.write_text(json.dumps(doc), encoding="utf-8")

    report = dm.compute_drift(tmp_path)
    assert not report.unmanifested
    assert report.counts["matched"] == 0
    assert set(report.new) == {"data/a.txt", "data/b.bin", "data/sub/c.txt"}


def test_compute_drift_tolerates_non_list_roots(tmp_path: Path) -> None:
    """A manifest whose ``roots`` is not a list normalizes to no roots — no current
    files are discovered, so every RECORDED file reads as ``missing``. Kills the
    ``isinstance(roots, list)`` guard."""
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    path = dm.manifest_path(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["roots"] = "data"  # scalar, not a list
    path.write_text(json.dumps(doc), encoding="utf-8")

    report = dm.compute_drift(tmp_path)
    assert set(report.missing) == {"data/a.txt", "data/b.bin", "data/sub/c.txt"}
    assert report.matched == ()


def test_read_manifest_none_for_non_object_json(tmp_path: Path) -> None:
    """A manifest file that parses to a NON-object (a JSON array) reads as None, not
    a list handed to callers expecting a dict. Kills the ``isinstance(doc, dict)``
    guard in ``read_manifest``."""
    path = dm.manifest_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert dm.read_manifest(tmp_path) is None


# ── file_sha256: raw-byte, chunk-spanning ─────────────────────────────────────


def test_file_sha256_streams_across_chunk_boundary(tmp_path: Path) -> None:
    """The streamed raw-byte hash equals a one-shot sha256 over the exact bytes,
    even for a file LARGER than one read chunk — so the chunk loop reassembles the
    whole file. Kills a mutant that breaks the ``while chunk := ...`` accumulation
    (e.g. hashing only the first/last chunk)."""
    import hashlib

    blob = os.urandom(dm._HASH_CHUNK * 2 + 123)  # spans 3 reads
    target = tmp_path / "big.bin"
    target.write_bytes(blob)
    assert dm.file_sha256(target) == hashlib.sha256(blob).hexdigest()


# ── manifest_doc_sha: the ONE canonical-sha definition (empty-map identity) ────


def test_manifest_doc_sha_is_canonical_sha_including_empty(tmp_path: Path) -> None:
    """The doc sha IS ``determinism.canonical_sha`` over the records map — including
    the 0-file case. Pins the one-definition re-point so a sibling copy can't drift."""
    records = {"data/a.txt": {"sha256": "ab" * 32, "size": 6}}
    assert dm.manifest_doc_sha(records) == determinism.canonical_sha(records)
    assert dm.manifest_doc_sha({}) == determinism.canonical_sha({})
