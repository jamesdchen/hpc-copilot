"""live-conformance T8 — the two attention kinds (``ops/attention_queue.py`` C-queue).

``collect_conformance`` routes a registration's trailing-``min_window_n`` window
through the ONE comparator (``state/conformance.py::judge_window``) and surfaces a
``conformance-nonconforming`` (a FINDING) or ``conformance-needs-verdict`` item —
both class VERDICT, both fan-out 0. The item clears when a committed
``conformance-verdict`` post-dates the newest window receipt. Horizon lapse rides
the EXISTING ``registration-stale`` item with ``stale_cause == horizon-lapsed``.

TOY VOCABULARY ONLY: a fake ``sensor-7`` instrument-QC calibration. Never trading
vocabulary.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import hpc_agent.ops.export_dossier as export_dossier
from hpc_agent.ops import attention_queue as aq
from hpc_agent.state import conformance, conformance_store
from hpc_agent.state.decision_journal import append_decision as _state_append

_REG_ID = "reg-sensor-7"
_RUN_ID = "sensor-run-1"
_DOSSIER_SHA = "d" * 64
_BASELINE_PATH = "baseline.json"
_NOW = "2026-07-08T00:00:00Z"

# A sealed calibration baseline: readings in [20.0, 21.0], n=5 (well-evidenced).
_BASELINE_ROWS = [
    {"reading": 20.0},
    {"reading": 20.5},
    {"reading": 21.0},
    {"reading": 20.2},
    {"reading": 20.8},
]


@dataclass
class _FakeSig:
    bundle_sha256: str
    run_ids: list[str]
    entries: list[dict[str, Any]]


def _write_registration(
    experiment_dir: Path, *, review_horizon: str | None = "2027-01-01T00:00:00Z"
) -> None:
    """Write a registration record with a conformance declaration (bypassing the gate)."""
    (experiment_dir / _BASELINE_PATH).write_text(json.dumps(_BASELINE_ROWS), encoding="utf-8")
    block: dict[str, Any] = {
        "baseline": {"path": _BASELINE_PATH, "sha256": "b" * 64},
        "keys": ["reading"],
        "min_window_n": 3,
    }
    if review_horizon is not None:
        block["review_horizon"] = review_horizon
    _state_append(
        experiment_dir,
        scope_kind="registration",
        scope_id=_REG_ID,
        block="registration",
        response=f"register {_REG_ID}",
        resolved={
            "registration_id": _REG_ID,
            "run_id": _RUN_ID,
            "dossier_sha": _DOSSIER_SHA,
            "prerequisites": [],
            "conformance": block,
        },
    )


def _record(experiment_dir: Path, *, reading: float, ts: str) -> str:
    record = conformance.build_observation_record(
        registration_id=_REG_ID,
        dossier_sha=_DOSSIER_SHA,
        status_at_record="stale",
        payload={"reading": reading},
        observed_at=ts,
        labels={"emitter": "sensor-7"},
        emitter="sensor-7",
        ts=ts,
    )
    return str(conformance_store.append_observation(experiment_dir, record=record)["content_sha"])


def _write_verdict(experiment_dir: Path, *, cites: list[str], ts: str) -> None:
    """Write a conformance-verdict record straight to the journal (bypassing the gate)."""
    rec = {
        "schema_version": 1,
        "ts": ts,
        "scope_kind": "registration",
        "scope_id": _REG_ID,
        "block": "conformance-verdict",
        "response": f"verdict for {_REG_ID}",
        "resolved": {"registration_id": _REG_ID, "cites": cites, "note": "judged"},
    }
    path = experiment_dir / ".hpc" / "registrations" / f"{_REG_ID}.decisions.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


# ── the two item kinds ───────────────────────────────────────────────────────


def test_conforming_window_yields_nothing(tmp_path: Path) -> None:
    _write_registration(tmp_path)
    _record(tmp_path, reading=20.3, ts="2026-05-01T00:00:00Z")
    _record(tmp_path, reading=20.6, ts="2026-05-02T00:00:00Z")
    _record(tmp_path, reading=20.4, ts="2026-05-03T00:00:00Z")
    assert aq.collect_conformance(tmp_path, now=_NOW) == []


def test_nonconforming_window_is_a_finding(tmp_path: Path) -> None:
    _write_registration(tmp_path)
    _record(tmp_path, reading=25.0, ts="2026-05-01T00:00:00Z")
    _record(tmp_path, reading=25.1, ts="2026-05-02T00:00:00Z")
    _record(tmp_path, reading=24.9, ts="2026-05-03T00:00:00Z")
    items = aq.collect_conformance(tmp_path, now=_NOW)
    assert len(items) == 1
    item = items[0]
    assert item.kind == aq.CONFORMANCE_NONCONFORMING
    assert item.item_class == aq.VERDICT
    assert item.scope_kind == "registration"
    assert item.scope_id == _REG_ID
    assert item.evidence["overall"] == conformance.NONCONFORMING
    assert item.evidence["per_key"][0]["tier_reason"] == conformance.OUTSIDE_ENVELOPE
    assert item.since == "2026-05-03T00:00:00Z"  # newest receipt ts


def test_insufficient_window_routes_needs_verdict(tmp_path: Path) -> None:
    _write_registration(tmp_path)
    # Only 2 receipts < min_window_n=3 → insufficient_window → needs_verdict.
    _record(tmp_path, reading=25.0, ts="2026-05-01T00:00:00Z")
    _record(tmp_path, reading=25.1, ts="2026-05-02T00:00:00Z")
    items = aq.collect_conformance(tmp_path, now=_NOW)
    assert len(items) == 1
    assert items[0].kind == aq.CONFORMANCE_NEEDS_VERDICT
    assert items[0].evidence["per_key"][0]["tier_reason"] == conformance.INSUFFICIENT_WINDOW


def test_no_declaration_yields_nothing(tmp_path: Path) -> None:
    # A registration with NO conformance block opts out — no machinery runs.
    _state_append(
        tmp_path,
        scope_kind="registration",
        scope_id=_REG_ID,
        block="registration",
        response=f"register {_REG_ID}",
        resolved={"registration_id": _REG_ID, "run_id": _RUN_ID, "dossier_sha": _DOSSIER_SHA},
    )
    assert aq.collect_conformance(tmp_path, now=_NOW) == []


# ── mechanical clearing (C-verdict) ──────────────────────────────────────────


def test_verdict_post_dating_window_clears_the_item(tmp_path: Path) -> None:
    _write_registration(tmp_path)
    sha1 = _record(tmp_path, reading=25.0, ts="2026-05-01T00:00:00Z")
    _record(tmp_path, reading=25.1, ts="2026-05-02T00:00:00Z")
    _record(tmp_path, reading=24.9, ts="2026-05-03T00:00:00Z")
    # A verdict AFTER the newest receipt clears the finding.
    _write_verdict(tmp_path, cites=[sha1], ts="2026-05-04T00:00:00Z")
    assert aq.collect_conformance(tmp_path, now=_NOW) == []


def test_stale_verdict_does_not_clear(tmp_path: Path) -> None:
    _write_registration(tmp_path)
    sha1 = _record(tmp_path, reading=25.0, ts="2026-05-01T00:00:00Z")
    _record(tmp_path, reading=25.1, ts="2026-05-02T00:00:00Z")
    _record(tmp_path, reading=24.9, ts="2026-05-03T00:00:00Z")
    # A verdict BEFORE the newest receipt (fresh drift since) does NOT clear it.
    _write_verdict(tmp_path, cites=[sha1], ts="2026-05-02T12:00:00Z")
    items = aq.collect_conformance(tmp_path, now=_NOW)
    assert len(items) == 1
    assert items[0].kind == aq.CONFORMANCE_NONCONFORMING


# ── the D5 route-through + fan-out pins ──────────────────────────────────────


def test_route_through_judge_window() -> None:
    """The D5 pin: the collector routes through judge_window, never re-reduces an envelope."""
    src = inspect.getsource(aq.collect_conformance)
    assert "judge_window(" in src


def test_fanout_walk_gains_no_conformance_edge(tmp_path: Path) -> None:
    """Fan-out 0 by construction (the honest anti-capital-shaping answer, C-queue)."""
    for kind in (aq.CONFORMANCE_NONCONFORMING, aq.CONFORMANCE_NEEDS_VERDICT):
        item = aq.AttentionItem(
            kind=kind,
            item_class=aq.KIND_CLASS[kind],
            experiment_dir=str(tmp_path),
            scope_kind="registration",
            scope_id=_REG_ID,
            block="conformance-verdict",
            evidence={"content_sha": "d" * 64},
        )
        assert aq._fanout_for(item, tmp_path) == 0
    # The dispatch itself names no conformance kind (no encoded edge exists).
    assert "CONFORMANCE" not in inspect.getsource(aq._fanout_for)


# ── fail-open ────────────────────────────────────────────────────────────────


def test_fail_open_on_corrupt_journal(tmp_path: Path) -> None:
    reg_dir = tmp_path / ".hpc" / "registrations"
    reg_dir.mkdir(parents=True)
    (reg_dir / "reg-bad.decisions.jsonl").write_text("{not json\n", encoding="utf-8")
    assert aq.collect_conformance(tmp_path, now=_NOW) == []


# ── horizon lapse rides the EXISTING registration-stale item (no new kind) ────


def _stub_dossier_current(monkeypatch: pytest.MonkeyPatch) -> None:
    def _compute(_exp: Path, _run_id: str, include_lineage: bool = False) -> _FakeSig:
        return _FakeSig(bundle_sha256=_DOSSIER_SHA, run_ids=[_RUN_ID], entries=[])

    monkeypatch.setattr(export_dossier, "compute_dossier_signature", _compute)


def test_horizon_lapse_rides_registration_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Dossier is CURRENT (live == recorded), so the only staleness is the lapsed
    # horizon. It must ride the existing registration-stale item, cause 'horizon-lapsed'.
    _write_registration(tmp_path, review_horizon="2026-01-01T00:00:00Z")
    _stub_dossier_current(monkeypatch)
    items = aq.collect_registrations(tmp_path, now=_NOW)  # now is AFTER the horizon
    stale = [i for i in items if i.kind == aq.REGISTRATION_STALE]
    assert len(stale) == 1
    assert stale[0].evidence["stale_cause"] == "horizon-lapsed"


def test_unlapsed_horizon_is_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_registration(tmp_path, review_horizon="2027-01-01T00:00:00Z")
    _stub_dossier_current(monkeypatch)
    items = aq.collect_registrations(tmp_path, now=_NOW)  # now is BEFORE the horizon
    assert not [i for i in items if i.kind == aq.REGISTRATION_STALE]
