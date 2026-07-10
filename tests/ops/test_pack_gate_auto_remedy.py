"""The submit gate's AUTO-REMEDY path fires (domain-packs, 2026-07-10 ruling).

"The pack gate MAY auto-remedy; latency is to be OBLITERATED." On a drift/missing-
receipt refusal the gate first runs the pack-refresh core (re-seal + rebind any
STALE manifest from its sweep.json recipe, journaled old→new — DP2 holds, no pack
code runs), then refuses ONLY IF a caller-side check must still re-run — carrying
the exact check command(s) so the driving skill re-earns the receipt UNPROMPTED.

The guard-can-actually-fire test (``docs/internals/engineering-principles.md``):
a recipe-backed drift that WOULD have been a loud ``SpecInvalid`` is now mechanically
re-sealed, and the refusal becomes a ``PackReceiptsMissing`` whose remedy carries
the caller-side check command — then re-earning the receipt clears the gate. Drives
the REAL verbs against the real journal. Toy-domain vocabulary only.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.pack_bind import PackBindSpec
from hpc_agent._wire.actions.pack_record_receipt import PackRecordReceiptSpec
from hpc_agent.ops.pack.bind_op import pack_bind
from hpc_agent.ops.pack.record_receipt_op import pack_record_receipt
from hpc_agent.ops.pack_gate import assert_pack_receipts_current
from hpc_agent.state.pack_sweep import reseal_manifest

if TYPE_CHECKING:
    from pathlib import Path

_SLOT = "widget-audit"
_TEMPLATE_REL = "packs/toy/templates/audit.py"
_CHECK_CMD = "python packs/toy/check_widgets.py --experiment-dir ."


def _setup(experiment: Path) -> str:
    """A sealed, bound, receipted toy pack opted-in with a check command. Returns rel."""
    pack_root = experiment / "packs" / "toy"
    (pack_root / "templates").mkdir(parents=True, exist_ok=True)
    (pack_root / "templates" / "audit.py").write_text("# %% audit\n", encoding="utf-8")
    recipe = {
        "name": "toy",
        "version": "1.0.0",
        "seams": {"audit_template": "templates/audit.py"},
        "fills_slots": [_SLOT],
        "pack_files": ["templates/audit.py"],
        "sweep": [],
    }
    (pack_root / "sweep.json").write_text(json.dumps(recipe, indent=2), encoding="utf-8")
    manifest_rel = "packs/toy/manifest.json"
    reseal_manifest(experiment / manifest_rel, pack_root / "sweep.json")
    pack_bind(experiment_dir=experiment, spec=PackBindSpec(manifest=manifest_rel, pack="toy"))
    pack_record_receipt(
        experiment_dir=experiment,
        spec=PackRecordReceiptSpec(pack="toy", slot=_SLOT, checked=[_TEMPLATE_REL], passed=True),
    )
    (experiment / "interview.json").write_text(
        json.dumps(
            {
                "goal": "toy",
                "packs": [
                    {
                        "pack": "toy",
                        "manifest": manifest_rel,
                        "receipt_bindings": [
                            {"slot": _SLOT, "pack": "toy", "check": _CHECK_CMD}
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest_rel


def test_clean_gate_passes(tmp_path: Path) -> None:
    _setup(tmp_path)
    assert_pack_receipts_current(tmp_path)  # current receipt → no raise, no rebind churn


def test_recipe_backed_drift_auto_remedies_then_refuses_with_check(tmp_path: Path) -> None:
    """A drift that WOULD be a loud SpecInvalid is re-sealed + re-bound mechanically;
    the refusal is now PackReceiptsMissing carrying the caller-side check command."""
    _setup(tmp_path)
    # Edit a sealed pack file → recipe-backed drift.
    (tmp_path / _TEMPLATE_REL).write_text("# %% audit v2\n", encoding="utf-8")

    with pytest.raises(errors.PackReceiptsMissing) as ei:
        assert_pack_receipts_current(tmp_path)

    err = ei.value
    # The auto-remedy converted the drift SpecInvalid into an uncleared-receipt refusal.
    assert _SLOT in str(err)
    assert "stale" in str(err)
    # The refusal carries the caller-side check command as the remedy (structured + text).
    assert err.remedy and err.remedy[0]["check"] == _CHECK_CMD
    assert _CHECK_CMD in str(err)
    assert "WITHOUT asking the human" in str(err)


def test_after_recheck_the_gate_clears(tmp_path: Path) -> None:
    """Re-earning the receipt (re-running the check) after the auto-remedy clears the gate."""
    _setup(tmp_path)
    (tmp_path / _TEMPLATE_REL).write_text("# %% audit v2\n", encoding="utf-8")
    with pytest.raises(errors.PackReceiptsMissing):
        assert_pack_receipts_current(tmp_path)  # this pass already re-sealed + re-bound

    # The caller-side check re-runs: record a fresh receipt at the NOW-current bind + bytes.
    pack_record_receipt(
        experiment_dir=tmp_path,
        spec=PackRecordReceiptSpec(pack="toy", slot=_SLOT, checked=[_TEMPLATE_REL], passed=True),
    )
    assert_pack_receipts_current(tmp_path)  # cleared — no raise
