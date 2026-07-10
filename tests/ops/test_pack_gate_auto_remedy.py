"""The submit gate's AUTO-REMEDY path fires end-to-end (domain-packs, 2026-07-10).

"The pack gate MAY auto-remedy; latency is to be OBLITERATED." + the evening
CONVERSION 1 ruling ("prose cannot be load-bearing"): on a drift/missing-receipt
refusal the gate (1) re-seals + rebinds any STALE manifest from its sweep.json
recipe (journaled old→new — DP2 holds, no pack code runs), then (2) EXECUTES the
caller-authored check command ITSELF — a subprocess in the experiment dir (the
executor precedent), captured + journaled — and re-evaluates. The refusal survives
ONLY when no check is declared, the check fails/times out, or the slot is still
uncleared after it ran.

The guard-can-actually-fire test (``docs/internals/engineering-principles.md``):
a recipe-backed drift that WOULD have been a loud ``SpecInvalid`` is re-sealed, a
passing check clears the gate with ZERO refusals, and a failing check's OUTPUT
rides the surviving refusal. Drives the REAL verbs against the real journal.
Toy-domain vocabulary only.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.pack_bind import PackBindSpec
from hpc_agent._wire.actions.pack_record_receipt import PackRecordReceiptSpec
from hpc_agent.ops.pack.bind_op import pack_bind
from hpc_agent.ops.pack.record_receipt_op import pack_record_receipt
from hpc_agent.ops.pack.refresh_op import pack_checks_log_path
from hpc_agent.ops.pack_gate import assert_pack_receipts_current
from hpc_agent.state.pack_sweep import reseal_manifest

if TYPE_CHECKING:
    from pathlib import Path

_SLOT = "widget-audit"
_TEMPLATE_REL = "packs/toy/templates/audit.py"

#: A check that RECORDS the receipt for the slot via the real verb (cwd = the
#: experiment dir, so ``Path(".")`` is the experiment). ``sys.executable`` is
#: quoted so the POSIX shlex tokenizer keeps a Windows path with backslashes
#: intact — the exact exec form ``run_check_command`` documents.
_PASSING_CHECK = (
    f'"{sys.executable}" -c '
    '"from pathlib import Path; '
    "from hpc_agent.ops.pack.record_receipt_op import pack_record_receipt; "
    "from hpc_agent._wire.actions.pack_record_receipt import PackRecordReceiptSpec; "
    "pack_record_receipt(experiment_dir=Path('.'), "
    "spec=PackRecordReceiptSpec(pack='toy', slot='widget-audit', "
    "checked=['packs/toy/templates/audit.py'], passed=True))\""
)

#: A check that fails loudly (non-zero exit) — refusal must survive and name it.
_FAILING_CHECK = f'"{sys.executable}" -c "import sys; sys.exit(7)"'


def _setup(experiment: Path, *, check: str | None) -> str:
    """A sealed, bound, receipted toy pack opted-in with (or without) a check."""
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
    binding: dict[str, str] = {"slot": _SLOT, "pack": "toy"}
    if check is not None:
        binding["check"] = check
    (experiment / "interview.json").write_text(
        json.dumps(
            {
                "goal": "toy",
                "packs": [{"pack": "toy", "manifest": manifest_rel, "receipt_bindings": [binding]}],
            }
        ),
        encoding="utf-8",
    )
    return manifest_rel


def test_clean_gate_passes(tmp_path: Path) -> None:
    _setup(tmp_path, check=_PASSING_CHECK)
    assert_pack_receipts_current(tmp_path)  # current receipt → no raise, no rebind churn


def test_drift_auto_remedy_runs_check_and_gate_passes(tmp_path: Path) -> None:
    """The WHOLE loop: drift → re-seal + rebind → the gate RUNS the check → PASSES.

    Zero refusals, zero human turns: the auto-remedy's re-seal moves the manifest
    sha (staling the covered receipt), and the gate then executes the caller check
    itself, which records a fresh receipt at the now-current bind."""
    _setup(tmp_path, check=_PASSING_CHECK)
    # Edit a sealed pack file → recipe-backed drift (the receipt reads stale).
    (tmp_path / _TEMPLATE_REL).write_text("# %% audit v2\n", encoding="utf-8")

    assert_pack_receipts_current(tmp_path)  # no raise — the check auto-ran and cleared it

    # The check-run was journaled to the pack's checks ledger (the trail, not the
    # attestation journal).
    log = pack_checks_log_path(tmp_path, "toy")
    assert log.is_file()
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert lines and lines[-1]["slot"] == _SLOT and lines[-1]["ok"] is True


def test_failing_check_refusal_names_the_output(tmp_path: Path) -> None:
    """A declared check that FAILS: the auto-remedy ran it, the slot stays uncleared,
    and the surviving refusal NAMES the check's outcome (exit code + tail)."""
    _setup(tmp_path, check=_FAILING_CHECK)
    (tmp_path / _TEMPLATE_REL).write_text("# %% audit v2\n", encoding="utf-8")

    with pytest.raises(errors.PackReceiptsMissing) as ei:
        assert_pack_receipts_current(tmp_path)

    err = ei.value
    assert _SLOT in str(err)
    assert "stale" in str(err)
    # The refusal names that the check RAN and failed (CONVERSION 1: the gate ran it).
    assert "exited 7" in str(err)
    assert err.remedy and err.remedy[0]["check"] == _FAILING_CHECK
    assert err.remedy[0]["check_run"] is not None and "exited 7" in str(err.remedy[0]["check_run"])


def test_no_check_declared_refuses_today(tmp_path: Path) -> None:
    """No check command on the binding → the auto-remedy re-seals but cannot re-earn;
    today's refusal survives, pointing at pack-record-receipt."""
    _setup(tmp_path, check=None)
    (tmp_path / _TEMPLATE_REL).write_text("# %% audit v2\n", encoding="utf-8")

    with pytest.raises(errors.PackReceiptsMissing) as ei:
        assert_pack_receipts_current(tmp_path)

    err = ei.value
    assert _SLOT in str(err)
    assert "stale" in str(err)
    assert "pack-record-receipt" in str(err)
    assert err.remedy and err.remedy[0]["check"] is None
    assert err.remedy[0]["check_run"] is None
