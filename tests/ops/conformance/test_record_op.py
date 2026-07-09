"""Tests for ``conformance-record`` (``ops/conformance/record_op.py``, T4).

Instrument-QC toy vocabulary ONLY (C6): a fake sensor (``sensor-7``) whose
registered calibration envelope is judged against live readings — never trading
vocabulary. Covers the recording posture by registration status (happy/current
append, absent refused, stale + revoked recorded-and-stamped never refused), the
asserted-sha IMPOSSIBILITY (no sha field on the spec), the one-append-only
side-effect probe, and the ``agent_facing=False`` pin.

The live-dossier recompute is stubbed (``record_op._recompute_dossier_sha``) so
the toy needs no real dossier substrate: returning the recorded sha reads
``current``; returning ``None``/a different sha reads ``stale``. The registration
journal is written straight to the state store (no gate) — the reduction only
needs the block + ``registration_id`` + ``dossier_sha``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import get_registry
from hpc_agent._wire.actions.conformance_record import ConformanceRecordSpec
from hpc_agent.ops.conformance import record_op
from hpc_agent.state import conformance_store
from hpc_agent.state.conformance import validate_observation
from hpc_agent.state.conformance_store import _canonical_observation_sha
from hpc_agent.state.decision_journal import append_decision as _state_append_decision

if TYPE_CHECKING:
    from pathlib import Path

_REG = "sensor-7-cal-2026"
_RUN = "sensor-7-cal-run"
_RECORDED_SHA = "cafef00ddeadbeef" * 4  # the registration's sealed dossier sha
_OBSERVED_AT = "2026-05-01T00:00:00Z"


def _register(experiment_dir: Path, *, dossier_sha: str = _RECORDED_SHA) -> None:
    """Write one ``registration`` record straight to the state journal (no gate)."""
    _state_append_decision(
        experiment_dir,
        scope_kind="registration",
        scope_id=_REG,
        block="registration",
        response=f"register {_REG} — sealed calibration {dossier_sha[:12]}",
        resolved={
            "registration_id": _REG,
            "run_id": _RUN,
            "dossier_sha": dossier_sha,
        },
    )


def _revoke(experiment_dir: Path) -> None:
    """Append a ``registration-revoke`` record (the newest family record wins)."""
    _state_append_decision(
        experiment_dir,
        scope_kind="registration",
        scope_id=_REG,
        block="registration-revoke",
        response=f"revoke {_REG} — sensor recalibrated off-spec",
        resolved={"registration_id": _REG, "reason": "sensor recalibrated off-spec"},
    )


def _spec(**overrides: object) -> ConformanceRecordSpec:
    base: dict[str, object] = {
        "registration_id": _REG,
        "payload": {"reading_mv": 0.947},
        "observed_at": _OBSERVED_AT,
        "labels": {"bench": "north"},
        "emitter": "cal-rig-1",
    }
    base.update(overrides)
    return ConformanceRecordSpec(**base)  # type: ignore[arg-type]


def _stub_live_sha(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Force the live dossier recompute to *value* (current when == recorded)."""
    monkeypatch.setattr(record_op, "_recompute_dossier_sha", lambda _exp, _winner: value)


# ── happy append (current) ──────────────────────────────────────────────────


def test_happy_append_records_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A standing registration → one ledger line, valid bind, current stamp, echo."""
    _register(tmp_path)
    _stub_live_sha(monkeypatch, _RECORDED_SHA)  # live matches recorded → current

    result = record_op.conformance_record(experiment_dir=tmp_path, spec=_spec())

    # Result echo: server-computed sha + stamped status + echoed observed_at.
    assert result.registration_id == _REG
    assert result.status_at_record == "current"
    assert result.observed_at == _OBSERVED_AT
    assert result.content_sha == _canonical_observation_sha(
        {"reading_mv": 0.947}, {"bench": "north"}, _OBSERVED_AT
    )
    assert result.ledger_path is not None and result.ledger_path.endswith(f"{_REG}.jsonl")

    # Exactly one ledger line, and it binds validly (the C-store shape holds).
    records, skipped = conformance_store.read_observations(tmp_path, _REG)
    assert skipped == 0
    assert len(records) == 1
    obs = validate_observation(records[0])
    assert obs.content_sha == result.content_sha
    assert obs.status_at_record == "current"
    assert obs.dossier_sha == _RECORDED_SHA
    assert obs.registration_id == _REG


# ── absent registration refused ─────────────────────────────────────────────


def test_absent_registration_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No registration record → loud refusal (no hypothesis to test)."""
    _stub_live_sha(monkeypatch, _RECORDED_SHA)
    with pytest.raises(errors.SpecInvalid, match="ABSENT"):
        record_op.conformance_record(experiment_dir=tmp_path, spec=_spec())
    # And nothing was appended.
    records, _ = conformance_store.read_observations(tmp_path, _REG)
    assert records == []


# ── stale recorded-and-stamped, never refused ───────────────────────────────


def test_stale_registration_recorded_and_stamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drifted (live sha ≠ recorded) registration is RECORDED, stamped stale."""
    _register(tmp_path)
    _stub_live_sha(monkeypatch, None)  # dossier moved/absent → stale

    result = record_op.conformance_record(experiment_dir=tmp_path, spec=_spec())

    assert result.status_at_record == "stale"
    records, skipped = conformance_store.read_observations(tmp_path, _REG)
    assert skipped == 0 and len(records) == 1
    assert validate_observation(records[0]).status_at_record == "stale"


# ── revoked recorded-and-stamped, never refused ─────────────────────────────


def test_revoked_registration_recorded_and_stamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overturned registration is RECORDED, stamped revoked (fail-open)."""
    _register(tmp_path)
    _revoke(tmp_path)
    # The revoke wins regardless of the live sha; the recorded dossier_sha is still
    # stamped (fallback to the newest registration record's sha).
    _stub_live_sha(monkeypatch, _RECORDED_SHA)

    result = record_op.conformance_record(experiment_dir=tmp_path, spec=_spec())

    assert result.status_at_record == "revoked"
    records, skipped = conformance_store.read_observations(tmp_path, _REG)
    assert skipped == 0 and len(records) == 1
    obs = validate_observation(records[0])
    assert obs.status_at_record == "revoked"
    assert obs.dossier_sha == _RECORDED_SHA  # the sealed hypothesis identity


# ── asserted-sha impossibility (no spec field) ──────────────────────────────


def test_spec_carries_no_sha_field() -> None:
    """The spec's field set is pinned; a caller CANNOT assert a content_sha."""
    assert set(ConformanceRecordSpec.model_fields) == {
        "registration_id",
        "payload",
        "observed_at",
        "labels",
        "emitter",
    }
    assert "content_sha" not in ConformanceRecordSpec.model_fields
    # extra="forbid": a sha on the wire is rejected outright, not ignored.
    with pytest.raises(Exception, match="content_sha|extra"):
        ConformanceRecordSpec(
            registration_id=_REG,
            payload={"reading_mv": 0.947},
            observed_at=_OBSERVED_AT,
            content_sha="deadbeef",  # type: ignore[call-arg]
        )


# ── one-append-only side-effect probe ───────────────────────────────────────


def _snapshot(root: Path) -> dict[str, bytes]:
    # The advisory flock sidecar (`.lock`) is part of the ONE append's mechanics
    # (the shared flock+fsync helper), not a distinct data write — ignore it.
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p.suffix != ".lock"
    }


def test_single_append_is_the_only_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording mutates NOTHING but the one ledger file (exactly one new line)."""
    _register(tmp_path)
    _stub_live_sha(monkeypatch, _RECORDED_SHA)

    before = _snapshot(tmp_path)
    record_op.conformance_record(experiment_dir=tmp_path, spec=_spec())
    after = _snapshot(tmp_path)

    ledger_rel = str(
        conformance_store.conformance_ledger_path(tmp_path, _REG).relative_to(tmp_path)
    )
    new_paths = set(after) - set(before)
    assert new_paths == {ledger_rel}, f"unexpected writes: {new_paths}"
    # No pre-existing file (e.g. the registration journal) was touched.
    for path, data in before.items():
        assert after[path] == data, f"unexpected mutation of {path}"
    # Exactly one line in the ledger.
    assert after[ledger_rel].decode("utf-8").strip().count("\n") == 0


# ── agent_facing=False pin ──────────────────────────────────────────────────


def test_conformance_record_is_not_agent_facing() -> None:
    """The record verb is caller/cron machinery, never an agent tool (C-verbs)."""
    meta = get_registry()["conformance-record"]
    assert meta.agent_facing is False
    assert meta.verb == "mutate"
