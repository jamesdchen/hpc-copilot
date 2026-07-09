"""Tests for the live-conformance ledger (``state/conformance_store.py``, T3).

Instrument-QC toy vocabulary ONLY (C6): a fake sensor (``sensor-7``) whose
registered calibration envelope is judged against live readings — never trading
vocabulary. Covers the append round-trip (bind recompute matches; a doctored
payload sha refused), path derivation + slug refusal, the tolerant read with a
torn line, the window-selection boundaries + exclusivity guard, and the
single-write property.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent.state import conformance_store as cs
from hpc_agent.state.conformance_store import _canonical_observation_sha

if TYPE_CHECKING:
    from pathlib import Path

_REG = "sensor-7-cal-2026"


def _observation(
    *,
    registration_id: str = _REG,
    payload: dict[str, Any] | None = None,
    labels: dict[str, Any] | None = None,
    observed_at: str = "2026-05-01T00:00:00Z",
    status_at_record: str = "current",
    content_sha: str | None = None,
) -> dict[str, Any]:
    """A well-formed observation with a correctly-recomputed content_sha."""
    payload = {"reading_mv": 0.947} if payload is None else payload
    labels = {"bench": "north"} if labels is None else labels
    sha = (
        content_sha
        if content_sha is not None
        else _canonical_observation_sha(payload, labels, observed_at)
    )
    return {
        "schema_version": cs.SCHEMA_VERSION,
        "attestor": cs.ATTESTOR,
        "subject_kind": cs.SUBJECT_KIND,
        "subject_id": registration_id,
        "content_sha": sha,
        "registration": {
            "registration_id": registration_id,
            "dossier_sha": "deadbeefcafef00d",
            "status_at_record": status_at_record,
        },
        "observed_at": observed_at,
        "labels": labels,
        "payload": payload,
        "emitter": "cal-rig-1",
    }


# ── append round-trip + bind lock ────────────────────────────────────────────


def test_append_round_trip_binds_and_reads_back(tmp_path: Path) -> None:
    rec = _observation()
    written = cs.append_observation(tmp_path, record=rec)
    assert written["subject_id"] == _REG
    assert written["ts"]  # stamped server-side

    records, skipped = cs.read_observations(tmp_path, _REG)
    assert skipped == 0
    assert len(records) == 1
    assert records[0]["payload"] == {"reading_mv": 0.947}
    assert records[0]["registration"]["status_at_record"] == "current"


def test_doctored_payload_sha_refused(tmp_path: Path) -> None:
    rec = _observation(content_sha="0" * 64)
    with pytest.raises(errors.SpecInvalid):
        cs.append_observation(tmp_path, record=rec)
    # Nothing was written.
    records, _ = cs.read_observations(tmp_path, _REG)
    assert records == []


def test_recompute_matches_when_payload_edited(tmp_path: Path) -> None:
    # A different payload yields a different sha; the correctly-recomputed record
    # binds, the stale sha does not.
    rec_good = _observation(payload={"reading_mv": 0.912})
    cs.append_observation(tmp_path, record=rec_good)
    rec_bad = _observation(payload={"reading_mv": 0.912})
    rec_bad["content_sha"] = _canonical_observation_sha(
        {"reading_mv": 0.999}, rec_bad["labels"], rec_bad["observed_at"]
    )
    with pytest.raises(errors.SpecInvalid):
        cs.append_observation(tmp_path, record=rec_bad)


def test_fail_open_records_against_revoked_registration(tmp_path: Path) -> None:
    # Recording is fail-open for evidence: a revoked status is stamped, never refused.
    rec = _observation(status_at_record="revoked")
    cs.append_observation(tmp_path, record=rec)
    records, _ = cs.read_observations(tmp_path, _REG)
    assert records[0]["registration"]["status_at_record"] == "revoked"


def test_missing_status_at_record_refused(tmp_path: Path) -> None:
    rec = _observation()
    del rec["registration"]["status_at_record"]
    with pytest.raises(errors.SpecInvalid):
        cs.append_observation(tmp_path, record=rec)


def test_registration_id_mismatch_refused(tmp_path: Path) -> None:
    rec = _observation()
    rec["registration"]["registration_id"] = "sensor-8-cal-2026"
    with pytest.raises(errors.SpecInvalid):
        cs.append_observation(tmp_path, record=rec)


def test_container_payload_value_refused(tmp_path: Path) -> None:
    rec = _observation(payload={"reading_mv": [1, 2, 3]})
    with pytest.raises(errors.SpecInvalid):
        cs.append_observation(tmp_path, record=rec)


# ── path derivation + slug refusal ───────────────────────────────────────────


def test_path_derivation(tmp_path: Path) -> None:
    path = cs.conformance_ledger_path(tmp_path, _REG)
    assert path.parent.name == "_conformance"
    assert path.parent.parent.name == "_aggregated"
    assert path.name == f"{_REG}.jsonl"


def test_slug_refusal_on_path_traversal(tmp_path: Path) -> None:
    for bad in ("../escape", "a/b", "has space", ""):
        with pytest.raises(errors.SpecInvalid):
            cs.conformance_ledger_path(tmp_path, bad)


# ── tolerant read ────────────────────────────────────────────────────────────


def test_missing_ledger_reads_empty(tmp_path: Path) -> None:
    records, skipped = cs.read_observations(tmp_path, _REG)
    assert records == []
    assert skipped == 0


def test_torn_line_counted_as_skipped(tmp_path: Path) -> None:
    cs.append_observation(tmp_path, record=_observation(observed_at="2026-05-01T00:00:00Z"))
    path = cs.conformance_ledger_path(tmp_path, _REG)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
        fh.write("\n")  # blank line — ignored, NOT skipped
    cs.append_observation(tmp_path, record=_observation(observed_at="2026-05-02T00:00:00Z"))

    records, skipped = cs.read_observations(tmp_path, _REG)
    assert len(records) == 2  # the two good lines survive the torn one
    assert skipped == 1  # only the torn line, not the blank


# ── window selection ─────────────────────────────────────────────────────────


def _windowable() -> list[dict[str, Any]]:
    return [
        {"observed_at": "2026-05-01T00:00:00Z", "payload": {"reading_mv": 1}},
        {"observed_at": "2026-05-02T00:00:00Z", "payload": {"reading_mv": 2}},
        {"observed_at": "2026-05-03T00:00:00Z", "payload": {"reading_mv": 3}},
    ]


def test_window_since_inclusive_until_exclusive() -> None:
    recs = _windowable()
    # [since, until): since inclusive, until exclusive.
    got = cs.select_window(recs, since="2026-05-01T00:00:00Z", until="2026-05-03T00:00:00Z")
    stamps = [r["observed_at"] for r in got]
    assert stamps == ["2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z"]  # 05-03 excluded


def test_window_since_only_is_inclusive() -> None:
    recs = _windowable()
    got = cs.select_window(recs, since="2026-05-02T00:00:00Z")
    stamps = [r["observed_at"] for r in got]
    assert stamps == ["2026-05-02T00:00:00Z", "2026-05-03T00:00:00Z"]


def test_window_last_n() -> None:
    recs = _windowable()
    got = cs.select_window(recs, last_n=2)
    stamps = [r["observed_at"] for r in got]
    assert stamps == ["2026-05-02T00:00:00Z", "2026-05-03T00:00:00Z"]


def test_window_last_n_over_length_returns_all() -> None:
    recs = _windowable()
    assert len(cs.select_window(recs, last_n=99)) == 3


def test_window_exclusivity_guard() -> None:
    recs = _windowable()
    with pytest.raises(errors.SpecInvalid):
        cs.select_window(recs, since="2026-05-01T00:00:00Z", last_n=2)


def test_window_no_selector_refused() -> None:
    with pytest.raises(errors.SpecInvalid):
        cs.select_window(_windowable())


def test_window_last_n_must_be_positive() -> None:
    for bad in (0, -1, True):
        with pytest.raises(errors.SpecInvalid):
            cs.select_window(_windowable(), last_n=bad)


def test_window_skips_records_missing_observed_at() -> None:
    recs = [*_windowable(), {"payload": {"reading_mv": 9}}]  # no observed_at
    got = cs.select_window(recs, since="2026-05-01T00:00:00Z")
    assert len(got) == 3  # the observed_at-less record is skipped


# ── append is the ONLY write ─────────────────────────────────────────────────


def test_append_is_the_only_file_written(tmp_path: Path) -> None:
    cs.append_observation(tmp_path, record=_observation())
    ledger = cs.conformance_ledger_path(tmp_path, _REG)
    # Only the ledger (and its sibling advisory lock) may exist under _aggregated.
    created = {p.name for p in ledger.parent.iterdir() if p.is_file()}
    assert created <= {ledger.name, ledger.name + ".lock"}
    assert ledger.name in created
