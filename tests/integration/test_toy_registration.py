"""T10 — the toy registration, driven END TO END over REAL substrate.

The first consumer of the registration kernel (``docs/design/registration-kernel.md``
T10): a deliberately dumb widget-batch lineage exercising the whole round-trip —
register (the gate passes over a real dossier + a real prerequisite chain) → verify
``current`` → edit the audited source → verify ``stale`` naming the audit slot →
re-sign + re-register → verify ``current`` → revoke with reason → verify ``revoked``
— plus the ~10-line caller-side deploy refusal that consumes the status.

NOTHING is stubbed: ``compute_dossier_signature``, ``check_chain``, and the append
gate all run for real. Toy vocabulary only — never harxhar/quant.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from hpc_agent._wire.actions.verify_registration import (
    VerifyRegistrationResult,
    VerifyRegistrationSpec,
)
from hpc_agent.ops.registration.verify_op import verify_registration
from tests.fixtures import toy_registration as toy

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_deploy_script() -> object:
    """Load the shipped caller-side deploy script from examples/ (a real consumer)."""
    path = _REPO_ROOT / "examples" / "toy_registration" / "deploy.py"
    spec = importlib.util.spec_from_file_location("toy_deploy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verify(experiment_dir: Path) -> VerifyRegistrationResult:
    return verify_registration(
        experiment_dir=experiment_dir,
        spec=VerifyRegistrationSpec(registration_id=toy.REG_ID),
    )


def test_toy_registration_full_round_trip(tmp_path: Path) -> None:
    deploy = _load_deploy_script()

    # ── build the substrate + register (the gate passes over real evidence) ──
    toy.build_substrate(tmp_path)
    res = toy.register(tmp_path)
    assert res.record.block == "registration"
    assert res.record.resolved["registration_id"] == toy.REG_ID

    # ── verify CURRENT + the caller-side deploy clears ──
    current = _verify(tmp_path)
    assert current.status == "current"
    assert {leg.slot: leg.status for leg in current.prerequisites} == {
        toy.AUDIT_SLOT: "current",
        toy.REPRO_SLOT: "current",
    }
    cleared = deploy.deploy_or_refuse(tmp_path, toy.REG_ID)  # type: ignore[attr-defined]
    assert cleared.status == "current"

    # ── edit the audited source → verify STALE naming the audit slot ──
    (tmp_path / "source.py").write_text(toy.SOURCE_PY_EDITED, encoding="utf-8")
    stale = _verify(tmp_path)
    assert stale.status == "stale"
    audit_leg = next(leg for leg in stale.prerequisites if leg.slot == toy.AUDIT_SLOT)
    assert audit_leg.status == "stale"
    assert toy.AUDIT_ID in audit_leg.evidence_note
    # the reproduction leg is untouched by the source edit
    repro_leg = next(leg for leg in stale.prerequisites if leg.slot == toy.REPRO_SLOT)
    assert repro_leg.status == "current"

    # ── the deploy refusal fires on the stale clearance ──
    with pytest.raises(SystemExit):
        deploy.deploy_or_refuse(tmp_path, toy.REG_ID)  # type: ignore[attr-defined]

    # ── re-sign the audit at the new source + re-register → CURRENT again ──
    toy.resign_audit(tmp_path, source=toy.SOURCE_PY_EDITED)
    res2 = toy.register(tmp_path)
    assert res2.record.block == "registration"
    revived = _verify(tmp_path)
    assert revived.status == "current"
    # the older registration was superseded by the newer one (append-only, R7).
    assert res2.count == 2
    recleared = deploy.deploy_or_refuse(tmp_path, toy.REG_ID)  # type: ignore[attr-defined]
    assert recleared.status == "current"

    # ── revoke with a mandatory reason → verify REVOKED, deploy refuses ──
    toy.revoke(tmp_path, reason="widget batch recalled by vendor")
    revoked = _verify(tmp_path)
    assert revoked.status == "revoked"
    with pytest.raises(SystemExit):
        deploy.deploy_or_refuse(tmp_path, toy.REG_ID)  # type: ignore[attr-defined]


def test_verify_absent_before_any_registration(tmp_path: Path) -> None:
    """A verify with no record on file reports ``absent`` (a reporter, never a raise)."""
    result = _verify(tmp_path)
    assert result.status == "absent"
