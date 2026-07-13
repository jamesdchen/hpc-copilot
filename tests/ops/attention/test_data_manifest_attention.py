"""Attention routing for data-manifest drift (``ops/attention_queue.py``).

The tier map (``docs/design/data-manifest.md`` attention contract): tracked
sha-change/missing = verdict (needs-attention); new untracked = informational
(low); no-manifest-but-declared = one standing informational disclosure.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

from hpc_agent.ops import attention_queue as q
from hpc_agent.state import data_manifest as dm

_NOW = "2026-07-08T00:00:00+00:00"


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)


def _make_inputs(root: Path) -> None:
    _write(root / "data" / "a.txt", "alpha\n")
    _write(root / "data" / "b.bin", os.urandom(16))


def _declare(root: Path, roots: list[str]) -> None:
    (root / "interview.json").write_text(
        json.dumps({"audited_source": {"input_roots": roots}}), encoding="utf-8"
    )


def test_no_declaration_no_manifest_yields_nothing(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    assert q.collect_data_manifest(tmp_path, now=_NOW) == []


def test_declared_but_unminted_is_one_standing_disclosure(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    _declare(tmp_path, ["data"])
    items = q.collect_data_manifest(tmp_path, now=_NOW)
    assert len(items) == 1
    assert items[0].kind == q.DATA_UNMANIFESTED
    assert items[0].item_class == q.INFORMATIONAL


def test_clean_manifest_yields_nothing(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    assert q.collect_data_manifest(tmp_path, now=_NOW) == []


def test_drifted_tracked_file_is_verdict_needs_attention(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    _write(tmp_path / "data" / "a.txt", "quietly rebuilt\n")
    items = q.collect_data_manifest(tmp_path, now=_NOW)
    drift = [i for i in items if i.kind == q.DATA_DRIFT]
    assert len(drift) == 1
    assert drift[0].item_class == q.VERDICT
    assert drift[0].scope_id == "data/a.txt"
    assert drift[0].scope_kind == "data"


def test_missing_tracked_file_is_data_drift(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    (tmp_path / "data" / "b.bin").unlink()
    items = q.collect_data_manifest(tmp_path, now=_NOW)
    drift = [i for i in items if i.kind == q.DATA_DRIFT]
    assert len(drift) == 1
    assert drift[0].evidence["change"] == "missing"


def test_new_untracked_file_is_one_informational_line(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    _write(tmp_path / "data" / "c.txt", "new\n")
    _write(tmp_path / "data" / "d.txt", "also new\n")
    items = q.collect_data_manifest(tmp_path, now=_NOW)
    new_items = [i for i in items if i.kind == q.DATA_NEW]
    assert len(new_items) == 1  # ONE aggregate line, not per-file
    assert new_items[0].item_class == q.INFORMATIONAL
    assert new_items[0].evidence["count"] == 2


def test_remint_clears_the_drift_item(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    _write(tmp_path / "data" / "a.txt", "changed\n")
    assert any(i.kind == q.DATA_DRIFT for i in q.collect_data_manifest(tmp_path, now=_NOW))
    dm.mint_manifest(tmp_path, ["data"])  # acknowledgment = re-mint
    assert not any(i.kind == q.DATA_DRIFT for i in q.collect_data_manifest(tmp_path, now=_NOW))


def test_collect_items_includes_data_manifest(tmp_path: Path) -> None:
    _make_inputs(tmp_path)
    dm.mint_manifest(tmp_path, ["data"])
    _write(tmp_path / "data" / "a.txt", "changed\n")
    kinds = {i.kind for i in q.collect_items(tmp_path, now=_NOW).items}
    assert q.DATA_DRIFT in kinds


def test_collector_routes_through_compute_drift() -> None:
    src = inspect.getsource(q.collect_data_manifest)
    assert "compute_drift(" in src  # the ONE drift definition (D5 route-through)
