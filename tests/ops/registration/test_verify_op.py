"""Tests for ``verify-registration`` (``ops/registration/verify_op.py``, T5).

The reporter seat of the registration kernel: given a ``registration_id`` (or a
``run_id`` naming a registration) it reduces the id's journal, recomputes the
four legs AT READ TIME, and renders a deterministic brief whose canonical-JSON
sha is the ``view_sha`` a sign-off must carry (R6/R8).

Seams stubbed here (Wave-B parallel work; both are code-against contracts):

* the T6 journal reader — ``verify_op._read_records`` is monkeypatched so these
  tests do not depend on the ``"registration"`` scope kind landing (T6). The
  ``run_id`` scan's ``_all_registration_ids`` is monkeypatched likewise.
* T4's ``check_chain`` — ``verify_op._check_chain`` is monkeypatched with a
  toy per-slot verdict list (``ops/registration/prereqs.py`` does not exist in
  this worktree; the op late-imports it, so the module imports cleanly).

TOY VOCABULARY ONLY (the plan's fixture rule): a widget-batch dossier, a
template with ``widget-owner`` / ``jam-threshold`` field slugs. Never
harxhar/quant words.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from hpc_agent._wire.actions.verify_registration import VerifyRegistrationSpec
from hpc_agent.ops.registration import verify_op

# --- toy fixtures -----------------------------------------------------------

_TEMPLATE = {"fields": ["widget-owner", "jam-threshold"], "prerequisites": []}
_DOSSIER_SHA = "a" * 64
_OLD_DOSSIER_SHA = "b" * 64


def _template_bytes() -> bytes:
    return json.dumps(_TEMPLATE).encode("utf-8")


def _template_sha() -> str:
    return hashlib.sha256(_template_bytes()).hexdigest()


def _write_template(experiment_dir: Path, name: str = "reg_template.json") -> str:
    (experiment_dir / name).write_bytes(_template_bytes())
    return name


def _registration_record(
    *,
    registration_id: str = "reg-widgets",
    run_id: str = "widget-run-1",
    dossier_sha: str = _DOSSIER_SHA,
    template: str = "reg_template.json",
    template_sha: str | None = None,
    fields: dict[str, Any] | None = None,
    prerequisites: list[dict[str, Any]] | None = None,
    ts: str = "2026-07-08T00:00:00Z",
    view_sha: str = "deadbeef",
) -> dict[str, Any]:
    return {
        "block": "registration",
        "ts": ts,
        "resolved": {
            "registration_id": registration_id,
            "run_id": run_id,
            "dossier_sha": dossier_sha,
            "template": template,
            "template_sha": template_sha if template_sha is not None else _template_sha(),
            "fields": fields
            if fields is not None
            else {"widget-owner": "wanda", "jam-threshold": "5"},
            "prerequisites": prerequisites if prerequisites is not None else [],
            "view_sha": view_sha,
        },
    }


def _revoke_record(
    *, registration_id: str = "reg-widgets", ts: str = "2026-07-08T01:00:00Z"
) -> dict[str, Any]:
    return {
        "block": "registration-revoke",
        "ts": ts,
        "resolved": {"registration_id": registration_id, "reason": "widgets recalled"},
    }


@dataclass
class _FakeSig:
    """Stands in for T3's ``DossierSignature`` (only the fields the op reads)."""

    bundle_sha256: str
    entries: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _FakeVerdict:
    """Stands in for T4's ``SlotVerdict`` (the attributes the op maps 1:1)."""

    slot: str
    kind: str
    status: str
    recorded_sha: str | None
    recomputed_sha: str | None
    evidence_note: str


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    records: list[dict[str, Any]],
    live_sha: str | None = _DOSSIER_SHA,
    verdicts: list[_FakeVerdict] | None = None,
    all_ids: list[str] | None = None,
    sig_raises: bool = False,
    chain_raises: bool = False,
) -> None:
    """Wire the T4/T6 seams + the T3 dossier recompute to toy stand-ins."""

    def _read_records(_experiment_dir: Path, _registration_id: str) -> list[dict[str, Any]]:
        return records

    def _all_registration_ids(_experiment_dir: Path) -> list[str]:
        return all_ids if all_ids is not None else ["reg-widgets"]

    def _compute(_experiment_dir: Path, _run_id: str, include_lineage: bool = False) -> _FakeSig:
        if sig_raises:
            raise RuntimeError("run moved/absent")
        return _FakeSig(bundle_sha256=live_sha or "")

    def _check_chain(_experiment_dir: Path, entries: list[Any]) -> list[_FakeVerdict]:
        if chain_raises:
            raise RuntimeError("checker unavailable")
        return verdicts if verdicts is not None else []

    monkeypatch.setattr(verify_op, "_read_records", _read_records)
    monkeypatch.setattr(verify_op, "_all_registration_ids", _all_registration_ids)
    monkeypatch.setattr(verify_op, "compute_dossier_signature", _compute)
    monkeypatch.setattr(verify_op, "_check_chain", _check_chain)


def _verify(experiment_dir: Path, **spec_kwargs: Any) -> Any:
    return verify_op.verify_registration(
        experiment_dir=experiment_dir, spec=VerifyRegistrationSpec(**spec_kwargs)
    )


# --- the tests --------------------------------------------------------------


def test_current_renders_brief_and_stable_view_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_template(tmp_path)
    _install(monkeypatch, records=[_registration_record()], live_sha=_DOSSIER_SHA)

    res = _verify(tmp_path, registration_id="reg-widgets")

    assert res.status == "current"
    assert res.registration_id == "reg-widgets"
    assert res.dossier is not None
    assert res.dossier.recorded_sha == _DOSSIER_SHA
    assert res.dossier.recomputed_sha == _DOSSIER_SHA
    assert res.template is not None and res.template.status == "current"
    assert res.fields.declared == ["widget-owner", "jam-threshold"]
    assert res.fields.missing == []
    assert res.brief.startswith("# Registration reg-widgets — current")
    assert res.view_sha  # non-empty witness

    # R6's fourth recompute leg: the view is byte-stable across repeated calls.
    res2 = _verify(tmp_path, registration_id="reg-widgets")
    assert res2.brief == res.brief
    assert res2.view_sha == res.view_sha


def test_stale_dossier_names_the_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    # Registration bound the OLD sha; the live re-gather returns a different one.
    _install(
        monkeypatch,
        records=[_registration_record(dossier_sha=_OLD_DOSSIER_SHA)],
        live_sha=_DOSSIER_SHA,
    )

    res = _verify(tmp_path, registration_id="reg-widgets")

    assert res.status == "stale"
    assert res.dossier is not None
    assert res.dossier.recorded_sha == _OLD_DOSSIER_SHA
    assert res.dossier.recomputed_sha == _DOSSIER_SHA
    assert res.dossier.recorded_sha != res.dossier.recomputed_sha


def test_stale_template_is_disclosed_not_a_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_template(tmp_path)
    # The registration recorded a DIFFERENT template sha than the file on disk.
    _install(
        monkeypatch,
        records=[_registration_record(template_sha="c" * 64)],
        live_sha=_DOSSIER_SHA,
    )

    res = _verify(tmp_path, registration_id="reg-widgets")

    # Template drift is disclosed; it does NOT flip the overall status (R5).
    assert res.status == "current"
    assert res.template is not None
    assert res.template.status == "stale"
    assert res.template.recorded_sha == "c" * 64
    assert res.template.recomputed_sha == _template_sha()


def test_missing_fields_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    _install(
        monkeypatch,
        records=[_registration_record(fields={"widget-owner": "wanda"})],  # jam-threshold empty
        live_sha=_DOSSIER_SHA,
    )

    res = _verify(tmp_path, registration_id="reg-widgets")

    assert res.fields.declared == ["widget-owner", "jam-threshold"]
    assert res.fields.present == ["widget-owner"]
    assert res.fields.missing == ["jam-threshold"]


def test_stale_prerequisite_flips_overall_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_template(tmp_path)
    prereq = {
        "slot": "audit",
        "kind": "attestation",
        "subject_id": "aud-1",
        "content_sha": "c1",
    }
    _install(
        monkeypatch,
        records=[_registration_record(prerequisites=[prereq])],
        live_sha=_DOSSIER_SHA,  # dossier itself is current
        verdicts=[
            _FakeVerdict("audit", "attestation", "stale", "c1", "c2", "block=x attestor=human")
        ],
    )

    res = _verify(tmp_path, registration_id="reg-widgets")

    # Dossier holds but a prerequisite drifted → overall STALE (R7).
    assert res.status == "stale"
    assert len(res.prerequisites) == 1
    assert res.prerequisites[0].slot == "audit"
    assert res.prerequisites[0].status == "stale"


def test_current_prerequisite_keeps_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_template(tmp_path)
    prereq = {"slot": "audit", "kind": "attestation", "subject_id": "aud-1", "content_sha": "c1"}
    _install(
        monkeypatch,
        records=[_registration_record(prerequisites=[prereq])],
        live_sha=_DOSSIER_SHA,
        verdicts=[
            _FakeVerdict("audit", "attestation", "current", "c1", "c1", "block=x attestor=human")
        ],
    )

    res = _verify(tmp_path, registration_id="reg-widgets")
    assert res.status == "current"
    assert res.prerequisites[0].status == "current"


def test_revoked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    _install(
        monkeypatch,
        records=[_registration_record(), _revoke_record()],
        live_sha=_DOSSIER_SHA,
    )

    res = _verify(tmp_path, registration_id="reg-widgets")

    assert res.status == "revoked"
    assert res.registration_id == "reg-widgets"
    # A revoke recomputes nothing — no legs.
    assert res.dossier is None
    assert res.template is None
    assert res.prerequisites == []
    assert "REVOKED" in res.brief


def test_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, records=[], live_sha=_DOSSIER_SHA)

    res = _verify(tmp_path, registration_id="reg-missing")

    assert res.status == "absent"
    assert res.registration_id is None
    assert res.dossier is None
    assert "ABSENT" in res.brief


def test_run_id_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    _install(
        monkeypatch,
        records=[_registration_record(run_id="widget-run-9")],
        live_sha=_DOSSIER_SHA,
        all_ids=["reg-widgets"],
    )

    res = _verify(tmp_path, run_id="widget-run-9")

    assert res.status == "current"
    assert res.registration_id == "reg-widgets"


def test_run_id_lookup_no_match_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    _install(
        monkeypatch,
        records=[_registration_record(run_id="widget-run-1")],
        live_sha=_DOSSIER_SHA,
        all_ids=["reg-widgets"],
    )

    res = _verify(tmp_path, run_id="some-other-run")
    assert res.status == "absent"
    assert res.registration_id is None


def test_reporter_never_raises_on_missing_dossier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_template(tmp_path)
    # The run moved/absent → compute_dossier_signature raises; the op must REPORT.
    _install(
        monkeypatch,
        records=[_registration_record()],
        sig_raises=True,
    )

    res = _verify(tmp_path, registration_id="reg-widgets")

    assert res.status == "stale"  # a None live sha cannot match any recorded sha
    assert res.dossier is not None
    assert res.dossier.recomputed_sha == ""
    assert "could not be recomputed" in res.brief


def test_reporter_never_raises_on_chain_check_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_template(tmp_path)
    prereq = {"slot": "audit", "kind": "attestation", "subject_id": "aud-1", "content_sha": "c1"}
    _install(
        monkeypatch,
        records=[_registration_record(prerequisites=[prereq])],
        live_sha=_DOSSIER_SHA,
        chain_raises=True,
    )

    res = _verify(tmp_path, registration_id="reg-widgets")

    # A checker that raises degrades to an absent leg — and flips overall stale.
    assert res.status == "stale"
    assert len(res.prerequisites) == 1
    assert res.prerequisites[0].status == "absent"
    assert "chain check unavailable" in res.prerequisites[0].evidence_note


def test_view_sha_byte_equal_across_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_template(tmp_path)
    prereq = {"slot": "audit", "kind": "attestation", "subject_id": "aud-1", "content_sha": "c1"}
    _install(
        monkeypatch,
        records=[_registration_record(prerequisites=[prereq])],
        live_sha=_DOSSIER_SHA,
        verdicts=[_FakeVerdict("audit", "attestation", "current", "c1", "c1", "note")],
    )

    shas = {_verify(tmp_path, registration_id="reg-widgets").view_sha for _ in range(3)}
    briefs = {_verify(tmp_path, registration_id="reg-widgets").brief for _ in range(3)}
    assert len(shas) == 1
    assert len(briefs) == 1


def test_spec_refuses_both_addresses() -> None:
    with pytest.raises(ValidationError):
        VerifyRegistrationSpec(registration_id="reg-widgets", run_id="widget-run-1")


def test_spec_refuses_neither_address() -> None:
    with pytest.raises(ValidationError):
        VerifyRegistrationSpec()
