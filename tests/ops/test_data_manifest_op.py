"""Verb + disclosure tests for ``data-manifest`` (``ops/data_manifest.py``).

Toy fixtures only (text files / random bytes; no parquet, no domain vocabulary).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.data_manifest import DataManifestSpec
from hpc_agent.ops.data_manifest import data_manifest, render_manifest_disclosure
from hpc_agent.state import data_manifest as dm
from tests.contracts.never_blocking import assert_never_blocking


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)


def _make_inputs(root: Path) -> None:
    _write(root / "data" / "a.txt", "alpha\n")
    _write(root / "data" / "b.bin", os.urandom(32))


def _declare(root: Path, roots: list[str]) -> None:
    (root / "interview.json").write_text(
        json.dumps({"audited_source": {"input_roots": roots}}), encoding="utf-8"
    )


# ── the verb ──────────────────────────────────────────────────────────────────


def test_verb_mints_with_explicit_roots(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    result = data_manifest(experiment_dir=tmp_path, spec=DataManifestSpec(roots=["data"]))
    assert result.file_count == 2
    assert result.roots == ["data"]
    assert len(result.manifest_doc_sha) == 64
    assert (tmp_path / ".hpc" / "data_manifest.json").is_file()


def test_verb_defaults_roots_to_declaration(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    _declare(tmp_path, ["data"])
    result = data_manifest(experiment_dir=tmp_path, spec=DataManifestSpec())
    assert result.roots == ["data"]
    assert result.file_count == 2


def test_verb_refuses_no_roots_and_no_declaration(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    with pytest.raises(errors.SpecInvalid) as exc:
        data_manifest(experiment_dir=tmp_path, spec=DataManifestSpec())
    assert "audited_source.input_roots" in str(exc.value)


def test_verb_spec_none_is_accepted(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    _declare(tmp_path, ["data"])
    result = data_manifest(experiment_dir=tmp_path, spec=None)
    assert result.file_count == 2


# ── the disclosure (consumer #2) ──────────────────────────────────────────────


def test_disclosure_none_when_nothing_declared_or_minted(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    assert render_manifest_disclosure(tmp_path) is None


def test_disclosure_standing_no_manifest_line(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    _declare(tmp_path, ["data"])
    disc = render_manifest_disclosure(tmp_path)
    assert disc is not None
    assert disc["status"] == "no-manifest"
    assert "no data manifest" in str(disc["line"])


def test_disclosure_counts_are_verdict_free(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    _write(tmp_path / "data" / "a.txt", "rebuilt\n")
    disc = render_manifest_disclosure(tmp_path)
    assert disc is not None
    assert disc["counts"] == {"matched": 1, "drifted": 1, "new": 0, "missing": 0}
    assert disc["drifted"] == ["data/a.txt"]
    # verdict-free: no judgment vocabulary in the rendered line
    line = str(disc["line"]).lower()
    for banned in ("corrupt", "updated", "appended", "restated", "bad", "error"):
        assert banned not in line


# ── the never-blocking pin ────────────────────────────────────────────────────


def test_disclosure_path_never_blocks() -> None:
    assert_never_blocking(render_manifest_disclosure)
    assert_never_blocking(dm.compute_drift)
