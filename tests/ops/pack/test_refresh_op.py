"""Integration tests for ``pack-refresh`` (domain-packs auto-remedy, end-to-end).

Drives the REAL verbs (``pack-bind`` / ``pack-record-receipt``) against the real
``.hpc/packs/<name>.decisions.jsonl`` journal (T8 landed). Covers: the minimal
stale set (editing one pack never rebinds another), the re-seal + rebind moving the
manifest sha (old→new journaled — the drift archive), the slots-to-reearn report
carrying the caller-side check command, and the not-opted-in silent no-op.
Toy-domain vocabulary only.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent._wire.actions.pack_record_receipt import PackRecordReceiptSpec
from hpc_agent._wire.actions.pack_refresh import PackRefreshSpec
from hpc_agent.ops.pack.record_receipt_op import pack_record_receipt
from hpc_agent.ops.pack.refresh_op import pack_refresh
from hpc_agent.state.pack_sweep import reseal_manifest

if TYPE_CHECKING:
    from pathlib import Path

_SLOT = "widget-audit"
_CHECK_CMD = "python packs/toy/check_widgets.py --experiment-dir ."


def _build_pack(experiment: Path, name: str, *, with_recipe: bool = True) -> str:
    """Write a toy pack (template + swept doc + sweep.json), seal + return manifest rel."""
    pack_root = experiment / "packs" / name
    (pack_root / "templates").mkdir(parents=True, exist_ok=True)
    (pack_root / "templates" / "audit.py").write_text("# %% audit\n", encoding="utf-8")
    recipe = {
        "name": name,
        "version": "1.0.0",
        "seams": {"audit_template": "templates/audit.py"},
        "fills_slots": [_SLOT],
        "pack_files": ["templates/audit.py"],
        "sweep": [],
    }
    manifest_rel = f"packs/{name}/manifest.json"
    if with_recipe:
        (pack_root / "sweep.json").write_text(json.dumps(recipe, indent=2), encoding="utf-8")
        reseal_manifest(experiment / manifest_rel, pack_root / "sweep.json")
    else:
        # Seal once, then remove the recipe (a manifest with no sweep.json).
        (pack_root / "sweep.json").write_text(json.dumps(recipe, indent=2), encoding="utf-8")
        reseal_manifest(experiment / manifest_rel, pack_root / "sweep.json")
        (pack_root / "sweep.json").unlink()
    return manifest_rel


def _bind(experiment: Path, name: str, manifest_rel: str) -> None:
    from hpc_agent._wire.actions.pack_bind import PackBindSpec
    from hpc_agent.ops.pack.bind_op import pack_bind

    pack_bind(experiment_dir=experiment, spec=PackBindSpec(manifest=manifest_rel, pack=name))


def _write_interview(experiment: Path, entries: list[dict]) -> None:
    (experiment / "interview.json").write_text(
        json.dumps({"goal": "toy", "packs": entries}), encoding="utf-8"
    )


def _record_receipt(experiment: Path, name: str) -> None:
    pack_record_receipt(
        experiment_dir=experiment,
        spec=PackRecordReceiptSpec(
            pack=name,
            slot=_SLOT,
            checked=[f"packs/{name}/templates/audit.py"],
            passed=True,
        ),
    )


def test_not_opted_in_is_silent_noop(tmp_path: Path) -> None:
    res = pack_refresh(experiment_dir=tmp_path, spec=PackRefreshSpec())
    assert res.refreshed == {}
    assert res.any_rebound is False


def test_reseal_rebind_and_report_slot_to_reearn(tmp_path: Path) -> None:
    """Edit a sealed file → refresh re-seals, rebinds (sha moves), and reports the
    slot to re-earn WITH its caller-side check command."""
    rel = _build_pack(tmp_path, "toy")
    _bind(tmp_path, "toy", rel)
    _write_interview(
        tmp_path,
        [
            {
                "pack": "toy",
                "manifest": rel,
                "receipt_bindings": [{"slot": _SLOT, "pack": "toy", "check": _CHECK_CMD}],
            }
        ],
    )
    _record_receipt(tmp_path, "toy")  # slot now current+passed

    # Edit a sealed pack file → drift.
    (tmp_path / "packs" / "toy" / "templates" / "audit.py").write_text(
        "# %% audit v2\n", encoding="utf-8"
    )

    res = pack_refresh(experiment_dir=tmp_path, spec=PackRefreshSpec())
    entry = res.refreshed["toy"]
    assert entry.recipe_found is True
    assert entry.stale is True
    assert entry.rebound is True
    assert entry.old_manifest_sha != entry.new_manifest_sha
    assert "templates/audit.py" in entry.changed_files
    assert res.any_rebound is True
    # The slot is now stale (rebind moved the manifest sha) and reported with its
    # caller-side check command.
    reearn = {s.slot: s for s in entry.slots_to_reearn}
    assert _SLOT in reearn
    assert reearn[_SLOT].status == "stale"
    assert reearn[_SLOT].check == _CHECK_CMD


def test_minimal_set_only_stale_pack_rebinds(tmp_path: Path) -> None:
    """Editing pack A leaves pack B unbound-untouched (a stale rv never rebuilds quant)."""
    rel_a = _build_pack(tmp_path, "packa")
    rel_b = _build_pack(tmp_path, "packb")
    _bind(tmp_path, "packa", rel_a)
    _bind(tmp_path, "packb", rel_b)
    _write_interview(
        tmp_path,
        [
            {"pack": "packa", "manifest": rel_a, "receipt_bindings": []},
            {"pack": "packb", "manifest": rel_b, "receipt_bindings": []},
        ],
    )
    # Edit only A.
    (tmp_path / "packs" / "packa" / "templates" / "audit.py").write_text("# x\n", encoding="utf-8")

    res = pack_refresh(experiment_dir=tmp_path, spec=PackRefreshSpec())
    assert res.refreshed["packa"].rebound is True
    assert res.refreshed["packb"].rebound is False
    assert res.refreshed["packb"].stale is False


def test_no_recipe_is_reported_not_rebuilt(tmp_path: Path) -> None:
    rel = _build_pack(tmp_path, "toy", with_recipe=False)
    _bind(tmp_path, "toy", rel)
    _write_interview(tmp_path, [{"pack": "toy", "manifest": rel, "receipt_bindings": []}])
    res = pack_refresh(experiment_dir=tmp_path, spec=PackRefreshSpec())
    entry = res.refreshed["toy"]
    assert entry.recipe_found is False
    assert entry.rebound is False
    assert entry.note is not None and "sweep.json" in entry.note
