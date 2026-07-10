"""``pack-status`` COMPOSES the audit-template default from the bound pack's seam.

Run-#12 finding 1: the on-ramp should assume the prepared audit template (the lab
pack's ``audit_template`` seam) is what an experiment builds off — a confirm-default,
not an open path question. ``pack-status`` surfaces the seam's experiment-dir-relative
path so the on-ramp can present it. Identity/pointer only; ``None`` when the pack
declares no such seam or is not current-bound. Toy-domain vocabulary only.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent._wire.actions.pack_bind import PackBindSpec
from hpc_agent._wire.actions.pack_status import PackStatusSpec
from hpc_agent.ops.pack.bind_op import pack_bind
from hpc_agent.ops.pack.status_op import pack_status
from hpc_agent.state.pack_sweep import reseal_manifest

if TYPE_CHECKING:
    from pathlib import Path


def _build(experiment: Path, *, seams: dict[str, str], pack_files: list[str]) -> str:
    pack_root = experiment / "packs" / "toy"
    (pack_root / "templates").mkdir(parents=True, exist_ok=True)
    (pack_root / "templates" / "audit.py").write_text("# %% audit\n", encoding="utf-8")
    (pack_root / "readers.json").write_text(json.dumps(["widgets.load"]), encoding="utf-8")
    recipe = {
        "name": "toy",
        "version": "1.0.0",
        "seams": seams,
        "fills_slots": [],
        "pack_files": pack_files,
        "sweep": [],
    }
    (pack_root / "sweep.json").write_text(json.dumps(recipe, indent=2), encoding="utf-8")
    manifest_rel = "packs/toy/manifest.json"
    reseal_manifest(experiment / manifest_rel, pack_root / "sweep.json")
    pack_bind(experiment_dir=experiment, spec=PackBindSpec(manifest=manifest_rel, pack="toy"))
    (experiment / "interview.json").write_text(
        json.dumps({"goal": "toy", "packs": [{"pack": "toy", "manifest": manifest_rel}]}),
        encoding="utf-8",
    )
    return manifest_rel


def test_audit_template_composed_from_seam(tmp_path: Path) -> None:
    _build(
        tmp_path,
        seams={"audit_template": "templates/audit.py"},
        pack_files=["templates/audit.py"],
    )
    res = pack_status(experiment_dir=tmp_path, spec=PackStatusSpec())
    entry = res.packs["toy"]
    # Experiment-dir-relative path to the seam file — the on-ramp's confirm-default.
    assert entry.audit_template == "packs/toy/templates/audit.py"


def test_audit_template_none_when_seam_absent(tmp_path: Path) -> None:
    _build(
        tmp_path,
        seams={"reader_calls": "readers.json"},
        pack_files=["readers.json"],
    )
    res = pack_status(experiment_dir=tmp_path, spec=PackStatusSpec())
    assert res.packs["toy"].audit_template is None
