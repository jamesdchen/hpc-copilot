"""live-conformance T7 — the ``ops/decision/journal.py`` gates:

* ``_assert_conformance_verdict_authorship`` (block ``conformance-verdict``) —
  the drift-verdict gate: non-empty ledger-resolved ``cites``, a dated ``note``,
  and the R6 authorship bar (name the id + a cited receipt sha by 8+ hex);
* ``_assert_registration_review_floor`` (block ``registration-review``) — the
  C-horizon re-affirmation: recompute the live dossier signature so a DRIFTED
  registration cannot be re-affirmed; and
* the ``conformance`` declaration's baseline-membership recompute leg at the
  registration append (``_assert_conformance_baseline_membership``).

The heavy dossier/chain substrates are stubbed at the module seams the gates call
(``ops/export_dossier.compute_dossier_signature`` + ``ops/registration_view.check_chain``);
``build_view`` runs FOR REAL so the R6 fourth leg is exercised end to end.

TOY VOCABULARY ONLY (the plan's fixture rule): a widget-batch dossier and a fake
``sensor-7`` emitter recording ``reading`` values. Never trading vocabulary.
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
    FieldsBlock,
    PrerequisiteLeg,
    TemplateLeg,
)
from hpc_agent.ops import export_dossier, registration_view
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.ops.registration.verify_op import build_view
from hpc_agent.state import conformance, conformance_store

# ── toy fixtures ────────────────────────────────────────────────────────────

_REG_ID = "reg-widgets"
_RUN_ID = "widget-run-1"
_DOSSIER_SHA = "d" * 64
_CONTENT_SHA = "abcdef0123456789" + "0" * 48
_BASELINE_PATH = "aggregated/widget-run-1/calibration.json"
_BASELINE_SHA = "b" * 64
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
_CONFORMANCE_BLOCK = {
    "baseline": {"path": _BASELINE_PATH, "sha256": _BASELINE_SHA},
    "keys": ["reading"],
    "min_window_n": 3,
    "review_horizon": "2027-01-01T00:00:00Z",
}
_BASELINE_ENTRY = {
    "source": "aggregated",
    "path": _BASELINE_PATH,
    "sha256": _BASELINE_SHA,
    "bytes": 10,
}


@dataclass
class _FakeSig:
    bundle_sha256: str
    run_ids: list[str] = field(default_factory=lambda: [_RUN_ID])
    entries: list[dict[str, Any]] = field(default_factory=lambda: [dict(_BASELINE_ENTRY)])


@dataclass
class _FakeVerdict:
    slot: str
    kind: str
    status: str
    recorded_sha: str
    recomputed_sha: str | None
    evidence_note: str


def _current_verdict() -> _FakeVerdict:
    return _FakeVerdict(
        slot="audit",
        kind="attestation",
        status="current",
        recorded_sha=_CONTENT_SHA,
        recomputed_sha=_CONTENT_SHA,
        evidence_note="attestation block='x' attestor='human' in 'notebook'/'aud-1'",
    )


def _template_bytes() -> bytes:
    return json.dumps(_TEMPLATE).encode("utf-8")


def _template_sha() -> str:
    return hashlib.sha256(_template_bytes()).hexdigest()


def _expected_view_sha(*, template_sha: str, entries: list[dict[str, Any]]) -> str:
    dossier_leg = DossierLeg(
        recorded_sha=_DOSSIER_SHA, recomputed_sha=_DOSSIER_SHA, drifted_stores=[]
    )
    template_leg = TemplateLeg(
        status="current", recorded_sha=template_sha, recomputed_sha=template_sha
    )
    v = _current_verdict()
    prereq_legs = [
        PrerequisiteLeg(
            slot=v.slot,
            kind=v.kind,  # type: ignore[arg-type]
            status=v.status,  # type: ignore[arg-type]
            recorded_sha=v.recorded_sha,
            recomputed_sha=v.recomputed_sha,
            evidence_note=v.evidence_note,
        )
    ]
    fields_report = FieldsBlock(
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


def _install(monkeypatch: pytest.MonkeyPatch, sig_state: dict[str, Any]) -> None:
    """Stub the dossier re-gather (live sha + entries from *sig_state*) and chain checker."""

    def _compute(_exp: Path, _run_id: str, include_lineage: bool = False) -> _FakeSig:
        return _FakeSig(bundle_sha256=sig_state["live"], entries=sig_state["entries"])

    def _check_chain(_exp: Path, entries: list[Any], *, dossier_run_ids: Any = None) -> list[Any]:
        return [_current_verdict()]

    monkeypatch.setattr(export_dossier, "compute_dossier_signature", _compute)
    monkeypatch.setattr(registration_view, "check_chain", _check_chain)


def _spec(
    *,
    block: str = "conformance-verdict",
    scope_id: str = _REG_ID,
    response: str,
    resolved: dict[str, Any],
) -> AppendDecisionInput:
    return AppendDecisionInput(
        scope_kind="registration",  # type: ignore[arg-type]
        scope_id=scope_id,
        block=block,
        response=response,
        resolved=resolved,
    )


def _register(
    experiment_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    conformance_block: dict[str, Any] | None,
) -> dict[str, Any]:
    """Append a real registration record (with an optional conformance block)."""
    (experiment_dir / "reg_template.json").write_bytes(_template_bytes())
    sig_state = {"live": _DOSSIER_SHA, "entries": [dict(_BASELINE_ENTRY)]}
    _install(monkeypatch, sig_state)
    tsha = _template_sha()
    resolved: dict[str, Any] = {
        "registration_id": _REG_ID,
        "run_id": _RUN_ID,
        "dossier_sha": _DOSSIER_SHA,
        "template": "reg_template.json",
        "template_sha": tsha,
        "fields": dict(_FIELDS),
        "prerequisites": [dict(c) for c in _CHAIN],
        "view_sha": _expected_view_sha(template_sha=tsha, entries=[dict(_BASELINE_ENTRY)]),
    }
    if conformance_block is not None:
        resolved["conformance"] = conformance_block
    append_decision(
        experiment_dir=experiment_dir,
        spec=_spec(
            block="registration",
            response="register reg-widgets — reviewed the audit prerequisite abcdef01",
            resolved=resolved,
        ),
    )
    return sig_state


def _record_receipt(experiment_dir: Path, *, reading: float, observed_at: str) -> str:
    """Append one live receipt to the conformance ledger; return its content_sha."""
    record = conformance.build_observation_record(
        registration_id=_REG_ID,
        dossier_sha=_DOSSIER_SHA,
        status_at_record="current",
        payload={"reading": reading},
        observed_at=observed_at,
        labels={"emitter": "sensor-7"},
        emitter="sensor-7",
        ts=observed_at,
    )
    appended = conformance_store.append_observation(experiment_dir, record=record)
    return str(appended["content_sha"])


# ── baseline-membership leg at the registration append ───────────────────────


def test_baseline_in_manifest_registers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The conformance baseline {path, sha256} IS a dossier manifest entry → registers.
    _register(tmp_path, monkeypatch, conformance_block=dict(_CONFORMANCE_BLOCK))
    # (no raise = pass; the registration is on record)


def test_baseline_not_in_manifest_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "reg_template.json").write_bytes(_template_bytes())
    sig_state = {"live": _DOSSIER_SHA, "entries": [dict(_BASELINE_ENTRY)]}
    _install(monkeypatch, sig_state)
    tsha = _template_sha()
    bad_block = {**_CONFORMANCE_BLOCK, "baseline": {"path": _BASELINE_PATH, "sha256": "f" * 64}}
    resolved = {
        "registration_id": _REG_ID,
        "run_id": _RUN_ID,
        "dossier_sha": _DOSSIER_SHA,
        "template": "reg_template.json",
        "template_sha": tsha,
        "fields": dict(_FIELDS),
        "prerequisites": [dict(c) for c in _CHAIN],
        "view_sha": _expected_view_sha(template_sha=tsha, entries=[dict(_BASELINE_ENTRY)]),
        "conformance": bad_block,
    }
    with pytest.raises(errors.SpecInvalid, match="NOT a member of the sealed dossier"):
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                block="registration",
                response="register reg-widgets — reviewed the audit prerequisite abcdef01",
                resolved=resolved,
            ),
        )
    # A structural refusal is UNMARKED (a re-elicit cannot fix a non-member artifact).


# ── conformance-verdict gate ─────────────────────────────────────────────────


def test_conformance_verdict_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _register(tmp_path, monkeypatch, conformance_block=dict(_CONFORMANCE_BLOCK))
    sha = _record_receipt(tmp_path, reading=99.0, observed_at="2026-05-01T00:00:00Z")
    res = append_decision(
        experiment_dir=tmp_path,
        spec=_spec(
            response=f"conformance-verdict for reg-widgets — receipt {sha[:8]} drifted; real shift",
            resolved={
                "registration_id": _REG_ID,
                "cites": [sha],
                "note": "the sensor drifted out of calibration; recalibrate before re-registering",
            },
        ),
    )
    assert res.record.block == "conformance-verdict"


def test_verdict_fabricated_receipt_sha_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(tmp_path, monkeypatch, conformance_block=dict(_CONFORMANCE_BLOCK))
    _record_receipt(tmp_path, reading=99.0, observed_at="2026-05-01T00:00:00Z")
    fabricated = "c" * 64
    with pytest.raises(errors.SpecInvalid, match="NOT carried by registration") as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                response=f"conformance-verdict for reg-widgets — receipt {fabricated[:8]} drifted",
                resolved={"registration_id": _REG_ID, "cites": [fabricated], "note": "drift"},
            ),
        )
    assert not hasattr(exc.value, "failure_features")  # citation refusal is UNMARKED


def test_verdict_empty_cites_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _register(tmp_path, monkeypatch, conformance_block=dict(_CONFORMANCE_BLOCK))
    with pytest.raises(errors.SpecInvalid, match="NON-EMPTY list"):
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                response="conformance-verdict for reg-widgets — nothing to cite",
                resolved={"registration_id": _REG_ID, "cites": [], "note": "n/a"},
            ),
        )


def test_verdict_bare_ack_refused_and_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(tmp_path, monkeypatch, conformance_block=dict(_CONFORMANCE_BLOCK))
    sha = _record_receipt(tmp_path, reading=99.0, observed_at="2026-05-01T00:00:00Z")
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                response="y",
                resolved={"registration_id": _REG_ID, "cites": [sha], "note": "drift"},
            ),
        )
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}


def test_verdict_missing_sha_prefix_refused_and_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(tmp_path, monkeypatch, conformance_block=dict(_CONFORMANCE_BLOCK))
    sha = _record_receipt(tmp_path, reading=99.0, observed_at="2026-05-01T00:00:00Z")
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                response="conformance-verdict for reg-widgets — the sensor looks off to me",
                resolved={"registration_id": _REG_ID, "cites": [sha], "note": "drift"},
            ),
        )
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}


# ── registration-review floor ────────────────────────────────────────────────


def test_registration_review_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _register(tmp_path, monkeypatch, conformance_block=dict(_CONFORMANCE_BLOCK))
    res = append_decision(
        experiment_dir=tmp_path,
        spec=_spec(
            block="registration-review",
            response="re-affirming reg-widgets — dossier dddddddd still holds, horizon extended",
            resolved={
                "registration_id": _REG_ID,
                "dossier_sha": _DOSSIER_SHA,
                "review_horizon": "2028-01-01T00:00:00Z",
            },
        ),
    )
    assert res.record.block == "registration-review"


def test_review_of_drifted_dossier_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sig_state = _register(tmp_path, monkeypatch, conformance_block=dict(_CONFORMANCE_BLOCK))
    # A sealed store moved AFTER registration: the live re-gather now differs.
    sig_state["live"] = "e" * 64
    with pytest.raises(errors.SpecInvalid, match="sealed stores have DRIFTED") as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                block="registration-review",
                response="re-affirming reg-widgets — dossier dddddddd still holds",
                resolved={
                    "registration_id": _REG_ID,
                    "dossier_sha": _DOSSIER_SHA,
                    "review_horizon": "2028-01-01T00:00:00Z",
                },
            ),
        )
    assert not hasattr(exc.value, "failure_features")  # drift refusal is UNMARKED


def test_review_bare_ack_refused_and_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(tmp_path, monkeypatch, conformance_block=dict(_CONFORMANCE_BLOCK))
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                block="registration-review",
                response="y",
                resolved={
                    "registration_id": _REG_ID,
                    "dossier_sha": _DOSSIER_SHA,
                    "review_horizon": "2028-01-01T00:00:00Z",
                },
            ),
        )
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}


def test_review_missing_dossier_sha_prefix_refused_and_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(tmp_path, monkeypatch, conformance_block=dict(_CONFORMANCE_BLOCK))
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                block="registration-review",
                response="re-affirming reg-widgets — still stand behind it",  # id, no sha prefix
                resolved={
                    "registration_id": _REG_ID,
                    "dossier_sha": _DOSSIER_SHA,
                    "review_horizon": "2028-01-01T00:00:00Z",
                },
            ),
        )
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}


def test_review_malformed_horizon_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _register(tmp_path, monkeypatch, conformance_block=dict(_CONFORMANCE_BLOCK))
    with pytest.raises(errors.SpecInvalid, match="ISO-8601"):
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                block="registration-review",
                response="re-affirming reg-widgets — dossier dddddddd holds",
                resolved={
                    "registration_id": _REG_ID,
                    "dossier_sha": _DOSSIER_SHA,
                    "review_horizon": "every 90 days",
                },
            ),
        )
