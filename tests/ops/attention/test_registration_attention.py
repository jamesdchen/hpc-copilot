"""T8 — the registration attention edges (``ops/attention_queue.py``, R8).

New item kinds ``registration-blocked`` (a non-current prerequisite blocks the
registration → BLOCKED) and ``registration-stale`` (a drifted dossier signature →
VERDICT), plus the leverage fan-out: an ``audit-section-*`` item's count grows by
the registrations whose chains name that audit. The heavy substrates are stubbed
at the seams the collector routes through (``compute_dossier_signature`` for the
dossier drift, ``check_chain`` for prerequisite currency).

TOY VOCABULARY ONLY: a widget-batch registration. Never harxhar/quant words.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import hpc_agent.ops.export_dossier as export_dossier
import hpc_agent.ops.registration.prereqs as prereqs
from hpc_agent.ops import attention_queue as aq
from hpc_agent.state.decision_journal import append_decision as _state_append

_REG_ID = "reg-widgets"
_RUN_ID = "widget-run-1"
_DOSSIER_SHA = "d" * 64
_AUDIT_ID = "aud-1"


@dataclass
class _FakeSig:
    bundle_sha256: str
    run_ids: list[str]
    entries: list[dict[str, Any]]


@dataclass
class _FakeVerdict:
    slot: str
    status: str


def _write_registration(
    experiment_dir: Path,
    *,
    reg_id: str = _REG_ID,
    dossier_sha: str = _DOSSIER_SHA,
    prerequisites: list[dict[str, Any]] | None = None,
) -> None:
    """Write a registration record straight to the journal (bypassing the gate)."""
    _state_append(
        experiment_dir,
        scope_kind="registration",
        scope_id=reg_id,
        block="registration",
        response=f"register {reg_id}",
        resolved={
            "registration_id": reg_id,
            "run_id": _RUN_ID,
            "dossier_sha": dossier_sha,
            "prerequisites": prerequisites if prerequisites is not None else [],
        },
    )


def _stub_dossier(monkeypatch: pytest.MonkeyPatch, live_sha: str) -> None:
    def _compute(_exp: Path, _run_id: str, include_lineage: bool = False) -> _FakeSig:
        return _FakeSig(bundle_sha256=live_sha, run_ids=[_RUN_ID], entries=[])

    monkeypatch.setattr(export_dossier, "compute_dossier_signature", _compute)


def _stub_chain(monkeypatch: pytest.MonkeyPatch, verdicts: list[_FakeVerdict]) -> None:
    def _check_chain(_exp: Path, entries: list[Any], *, dossier_run_ids: Any = None) -> list[Any]:
        return verdicts

    monkeypatch.setattr(prereqs, "check_chain", _check_chain)


_AUDIT_PREREQ = [
    {
        "slot": "audit",
        "kind": "notebook-audit",
        "subject_id": _AUDIT_ID,
        "content_sha": "c" * 64,
    }
]


# ── the two item kinds ───────────────────────────────────────────────────────


def test_stale_registration_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Registration bound "d"*64; the live re-gather returns a different sha → stale.
    _write_registration(tmp_path, prerequisites=[])
    _stub_dossier(monkeypatch, live_sha="e" * 64)

    items = aq.collect_registrations(tmp_path, now="2026-07-08T00:00:00Z")

    stale = [i for i in items if i.kind == aq.REGISTRATION_STALE]
    assert len(stale) == 1
    assert stale[0].item_class == aq.VERDICT
    assert stale[0].scope_kind == "registration"
    assert stale[0].scope_id == _REG_ID
    assert stale[0].evidence["recomputed_sha"] == "e" * 64
    # A stale-only registration is not also blocked (no prerequisites).
    assert not [i for i in items if i.kind == aq.REGISTRATION_BLOCKED]


def test_blocked_registration_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_registration(tmp_path, prerequisites=_AUDIT_PREREQ)
    _stub_dossier(monkeypatch, live_sha=_DOSSIER_SHA)  # dossier current → not stale
    _stub_chain(monkeypatch, [_FakeVerdict("audit", "stale")])  # prerequisite drifted

    items = aq.collect_registrations(tmp_path, now="2026-07-08T00:00:00Z")

    blocked = [i for i in items if i.kind == aq.REGISTRATION_BLOCKED]
    assert len(blocked) == 1
    assert blocked[0].item_class == aq.BLOCKED
    assert blocked[0].evidence["pending"] == [{"slot": "audit", "status": "stale"}]
    # Dossier is current → no stale item.
    assert not [i for i in items if i.kind == aq.REGISTRATION_STALE]


def test_current_registration_yields_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_registration(tmp_path, prerequisites=_AUDIT_PREREQ)
    _stub_dossier(monkeypatch, live_sha=_DOSSIER_SHA)
    _stub_chain(monkeypatch, [_FakeVerdict("audit", "current")])
    assert aq.collect_registrations(tmp_path, now="2026-07-08T00:00:00Z") == []


def test_no_registrations_no_items(tmp_path: Path) -> None:
    assert aq.collect_registrations(tmp_path, now="2026-07-08T00:00:00Z") == []


# ── the audit → registration leverage fan-out ────────────────────────────────


def test_audit_fanout_counts_registrations(tmp_path: Path) -> None:
    _write_registration(tmp_path, prerequisites=_AUDIT_PREREQ)
    assert aq._count_registrations_naming_audit(tmp_path, _AUDIT_ID) == 1
    # A different audit id is not named by this registration's chain.
    assert aq._count_registrations_naming_audit(tmp_path, "other-audit") == 0


def test_audit_item_fanout_grows_by_registrations(tmp_path: Path) -> None:
    _write_registration(tmp_path, prerequisites=_AUDIT_PREREQ)
    item = aq.AttentionItem(
        kind=aq.AUDIT_SECTION_UNSIGNED,
        item_class=aq.KIND_CLASS[aq.AUDIT_SECTION_UNSIGNED],
        experiment_dir=str(tmp_path),
        scope_kind="notebook",
        scope_id=_AUDIT_ID,
        block="construction",
    )
    # No runs echo the audit here, so the fan-out is exactly the registration count.
    assert aq._fanout_for(item, tmp_path) == 1


def test_revoked_registration_not_counted_in_fanout(tmp_path: Path) -> None:
    _write_registration(tmp_path, prerequisites=_AUDIT_PREREQ)
    _state_append(
        tmp_path,
        scope_kind="registration",
        scope_id=_REG_ID,
        block="registration-revoke",
        response=f"revoke {_REG_ID}",
        resolved={"registration_id": _REG_ID, "reason": "recalled"},
    )
    # A revoked registration no longer depends on the audit → not counted.
    assert aq._count_registrations_naming_audit(tmp_path, _AUDIT_ID) == 0


# ── fail-open ────────────────────────────────────────────────────────────────


def test_fail_open_on_corrupt_journal(tmp_path: Path) -> None:
    reg_dir = tmp_path / ".hpc" / "registrations"
    reg_dir.mkdir(parents=True)
    (reg_dir / "reg-bad.decisions.jsonl").write_text("{not json\n", encoding="utf-8")
    # A torn journal is skipped, never crashing the read.
    assert aq.collect_registrations(tmp_path, now="2026-07-08T00:00:00Z") == []
    assert aq._count_registrations_naming_audit(tmp_path, _AUDIT_ID) == 0
