"""T7 — the registration sign-off gate (``ops/decision/journal.py``, R6/R7).

Fires each lock of ``_assert_registration_authorship`` on a synthetic violation
and drives the happy register + revoke round-trip. The heavy substrates are
stubbed at the module seam: ``compute_dossier_signature`` (T3) and
``check_chain`` (T4) are monkeypatched to toy stand-ins — the gate reaches them
through ``ops/export_dossier`` and the ``ops/registration_view`` facade, so
patching the module attribute the gate calls is the whole stub. ``build_view``
(the pure R6 fourth-leg renderer) is used FOR REAL so the recomputed view_sha is
exercised end to end.

TOY VOCABULARY ONLY (the plan's fixture rule): a widget-batch dossier, a template
with ``widget-owner`` / ``jam-threshold`` field slugs. Never harxhar/quant words.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent._wire.actions.verify_registration import (
    DossierLeg,
    FieldsReport,
    PrerequisiteLeg,
    TemplateLeg,
)
from hpc_agent.ops import export_dossier, registration_view
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.ops.registration.verify_op import build_view

# ── toy fixtures ────────────────────────────────────────────────────────────

_REG_ID = "reg-widgets"
_RUN_ID = "widget-run-1"
_DOSSIER_SHA = "d" * 64
_CONTENT_SHA = "abcdef0123456789" + "0" * 48  # 64 hex; prefix "abcdef01"
_TEMPLATE = {
    "fields": ["widget-owner", "jam-threshold"],
    "prerequisites": [{"slot": "audit", "kind": "attestation"}],
}
_FIELDS = {"widget-owner": "wanda", "jam-threshold": "5"}
_CHAIN = [
    {
        "slot": "audit",
        "kind": "attestation",
        "subject_id": "notebook:aud-1",
        "content_sha": _CONTENT_SHA,
    }
]


@dataclass
class _FakeSig:
    bundle_sha256: str
    run_ids: list[str] = field(default_factory=lambda: [_RUN_ID])
    entries: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _FakeVerdict:
    slot: str
    kind: str
    status: str
    recorded_sha: str
    recomputed_sha: str | None
    evidence_note: str


def _template_bytes() -> bytes:
    return json.dumps(_TEMPLATE).encode("utf-8")


def _template_sha() -> str:
    return hashlib.sha256(_template_bytes()).hexdigest()


def _write_template(experiment_dir: Path, name: str = "reg_template.json") -> str:
    (experiment_dir / name).write_bytes(_template_bytes())
    return name


def _current_verdict() -> _FakeVerdict:
    return _FakeVerdict(
        slot="audit",
        kind="attestation",
        status="current",
        recorded_sha=_CONTENT_SHA,
        recomputed_sha=_CONTENT_SHA,
        evidence_note="attestation block='x' attestor='human' in 'notebook'/'aud-1'",
    )


def _expected_view_sha(*, template_sha: str, verdicts: list[_FakeVerdict]) -> str:
    """Recompute the R6 fourth-leg view_sha exactly as the gate does."""
    dossier_leg = DossierLeg(
        recorded_sha=_DOSSIER_SHA, recomputed_sha=_DOSSIER_SHA, drifted_stores=[]
    )
    template_leg = TemplateLeg(
        status="current", recorded_sha=template_sha, recomputed_sha=template_sha
    )
    prereq_legs = [
        PrerequisiteLeg(
            slot=v.slot,
            kind=v.kind,  # type: ignore[arg-type]
            status=v.status,  # type: ignore[arg-type]
            recorded_sha=v.recorded_sha,
            recomputed_sha=v.recomputed_sha,
            evidence_note=v.evidence_note,
        )
        for v in verdicts
    ]
    fields_report = FieldsReport(
        declared=["widget-owner", "jam-threshold"],
        present=["widget-owner", "jam-threshold"],
        missing=[],
    )
    _, view_sha = build_view(
        status="current",
        registration_id=_REG_ID,
        registered_at=None,
        dossier=dossier_leg,
        template=template_leg,
        prerequisites=prereq_legs,
        fields=fields_report,
    )
    return view_sha


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    live_sha: str = _DOSSIER_SHA,
    verdicts: list[_FakeVerdict] | None = None,
) -> None:
    """Stub the dossier re-gather + chain checker at the seams the gate calls."""
    the_verdicts = verdicts if verdicts is not None else [_current_verdict()]

    def _compute(_exp: Path, _run_id: str, include_lineage: bool = False) -> _FakeSig:
        return _FakeSig(bundle_sha256=live_sha)

    def _check_chain(_exp: Path, entries: list[Any], *, dossier_run_ids: Any = None) -> list[Any]:
        return the_verdicts

    monkeypatch.setattr(export_dossier, "compute_dossier_signature", _compute)
    monkeypatch.setattr(registration_view, "check_chain", _check_chain)


def _spec(
    *,
    block: str = "registration",
    scope_kind: str = "registration",
    scope_id: str = _REG_ID,
    response: str,
    resolved: dict[str, Any],
) -> AppendDecisionInput:
    return AppendDecisionInput(
        scope_kind=scope_kind,  # type: ignore[arg-type]
        scope_id=scope_id,
        block=block,
        response=response,
        resolved=resolved,
    )


def _registration_resolved(
    experiment_dir: Path,
    *,
    template_name: str = "reg_template.json",
    dossier_sha: str = _DOSSIER_SHA,
    template_sha: str | None = None,
    fields: dict[str, Any] | None = None,
    prerequisites: list[dict[str, Any]] | None = None,
    view_sha: str | None = None,
    verdicts: list[_FakeVerdict] | None = None,
) -> dict[str, Any]:
    tsha = template_sha if template_sha is not None else _template_sha()
    the_verdicts = verdicts if verdicts is not None else [_current_verdict()]
    return {
        "registration_id": _REG_ID,
        "run_id": _RUN_ID,
        "dossier_sha": dossier_sha,
        "template": template_name,
        "template_sha": tsha,
        "fields": fields if fields is not None else dict(_FIELDS),
        "prerequisites": prerequisites if prerequisites is not None else [dict(c) for c in _CHAIN],
        "view_sha": view_sha
        if view_sha is not None
        else _expected_view_sha(template_sha=tsha, verdicts=the_verdicts),
    }


_GOOD_RESPONSE = "register reg-widgets — reviewed the audit prerequisite abcdef01"


# ── the happy paths ─────────────────────────────────────────────────────────


def test_happy_registration_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    _install(monkeypatch)
    res = append_decision(
        experiment_dir=tmp_path,
        spec=_spec(response=_GOOD_RESPONSE, resolved=_registration_resolved(tmp_path)),
    )
    assert res.count == 1
    assert res.record.resolved["registration_id"] == _REG_ID
    # NO auto-clear / redundant marking exists at this gate.
    assert "redundant" not in res.record.resolved


def test_revoke_round_trip_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    _install(monkeypatch)
    append_decision(
        experiment_dir=tmp_path,
        spec=_spec(response=_GOOD_RESPONSE, resolved=_registration_resolved(tmp_path)),
    )
    res = append_decision(
        experiment_dir=tmp_path,
        spec=_spec(
            block="registration-revoke",
            response="revoke reg-widgets — the widget batch was recalled",
            resolved={"registration_id": _REG_ID, "reason": "widget batch recalled by vendor"},
        ),
    )
    assert res.count == 2
    assert res.record.block == "registration-revoke"


# ── Lock 2 (recompute) fire tests — plain SpecInvalid, UNMARKED ──────────────


def test_fabricated_dossier_sha_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    _install(monkeypatch, live_sha=_DOSSIER_SHA)
    # The record asserts a dossier_sha the live re-gather does not produce.
    resolved = _registration_resolved(tmp_path, dossier_sha="f" * 64)
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path, spec=_spec(response=_GOOD_RESPONSE, resolved=resolved)
        )
    assert not hasattr(exc.value, "failure_features")  # sha refusal is UNMARKED (E2 scoping)


def test_drifted_store_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    # A sealed store moved: the live re-gather differs from the recorded dossier_sha.
    _install(monkeypatch, live_sha="e" * 64)
    with pytest.raises(errors.SpecInvalid, match="does not match the recomputed"):
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(response=_GOOD_RESPONSE, resolved=_registration_resolved(tmp_path)),
        )


def test_template_sha_drift_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    _install(monkeypatch)
    resolved = _registration_resolved(tmp_path, template_sha="c" * 64)
    with pytest.raises(errors.SpecInvalid, match="template sha mismatch"):
        append_decision(
            experiment_dir=tmp_path, spec=_spec(response=_GOOD_RESPONSE, resolved=resolved)
        )


def test_stale_prerequisite_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    stale = _FakeVerdict("audit", "attestation", "stale", _CONTENT_SHA, "other", "moved")
    _install(monkeypatch, verdicts=[stale])
    resolved = _registration_resolved(tmp_path, verdicts=[stale])
    with pytest.raises(errors.SpecInvalid, match="partial registration REFUSED"):
        append_decision(
            experiment_dir=tmp_path, spec=_spec(response=_GOOD_RESPONSE, resolved=resolved)
        )


def test_missing_field_slug_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    _install(monkeypatch)
    resolved = _registration_resolved(
        tmp_path, fields={"widget-owner": "wanda"}
    )  # jam-threshold empty
    with pytest.raises(errors.SpecInvalid, match="fields incomplete"):
        append_decision(
            experiment_dir=tmp_path, spec=_spec(response=_GOOD_RESPONSE, resolved=resolved)
        )


def test_missing_declared_prerequisite_slot_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_template(tmp_path)
    _install(monkeypatch)
    # The chain fills a DIFFERENT slot than the declared "audit" (a non-empty
    # chain that nonetheless leaves the declared prerequisite unfilled — the
    # empty-list case is caught earlier by the required-non-empty shape check).
    resolved = _registration_resolved(
        tmp_path,
        prerequisites=[
            {
                "slot": "other",
                "kind": "attestation",
                "subject_id": "notebook:x",
                "content_sha": _CONTENT_SHA,
            }
        ],
    )
    with pytest.raises(errors.SpecInvalid, match="not present in the chain"):
        append_decision(
            experiment_dir=tmp_path, spec=_spec(response=_GOOD_RESPONSE, resolved=resolved)
        )


def test_view_sha_mismatch_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    _install(monkeypatch)
    resolved = _registration_resolved(tmp_path, view_sha="0" * 64)  # a fabricated witness
    with pytest.raises(errors.SpecInvalid, match="view_sha"):
        append_decision(
            experiment_dir=tmp_path, spec=_spec(response=_GOOD_RESPONSE, resolved=resolved)
        )


# ── Lock 3 (authorship) fire tests — MARKED with the E2 marker ───────────────


def test_bare_ack_refused_and_marked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    _install(monkeypatch)
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(response="y", resolved=_registration_resolved(tmp_path)),
        )
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}


def test_response_lacking_id_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    _install(monkeypatch)
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                response="reviewed the audit prerequisite abcdef01",  # sha prefix, no id
                resolved=_registration_resolved(tmp_path),
            ),
        )
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}


def test_response_lacking_sha_prefix_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_template(tmp_path)
    _install(monkeypatch)
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                response="register reg-widgets — looks good to me",  # id, no sha prefix
                resolved=_registration_resolved(tmp_path),
            ),
        )
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}


# ── the revoke floor ─────────────────────────────────────────────────────────


def test_revoke_without_reason_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    _install(monkeypatch)
    with pytest.raises(errors.SpecInvalid, match="free-text resolved\\['reason'\\]"):
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                block="registration-revoke",
                response="revoke reg-widgets",
                resolved={"registration_id": _REG_ID},  # no reason
            ),
        )


def test_revoke_bare_ack_refused_and_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_template(tmp_path)
    _install(monkeypatch)
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                block="registration-revoke",
                response="y",
                resolved={"registration_id": _REG_ID, "reason": "recalled"},
            ),
        )
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}


# ── block convention, BOTH directions ────────────────────────────────────────


def test_registration_block_refused_on_non_registration_scope(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="only valid for scope_kind='registration'"):
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(scope_kind="run", scope_id="widget-run-1", response="y", resolved={}),
        )


def test_registration_scope_refuses_foreign_block(tmp_path: Path) -> None:
    # ``registration-review`` is a PLANNED-but-not-yet-added family member — the
    # family exists to gate exactly this: an unreviewed block on the scope.
    with pytest.raises(errors.SpecInvalid, match="accepts only its block family"):
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(block="registration-review", response="whatever", resolved={}),
        )
