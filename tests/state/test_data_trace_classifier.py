"""Tests for the digest classifier (data-trace T3).

Fires-tests per context row (the doc's ON conditions), the override winning
both ways with its exercise recorded, and the degradation path (a consumer
wanting digests and finding none → DISCLOSED, never fabricated).
"""

from __future__ import annotations

import pytest

from hpc_agent.state.data_trace_classifier import (
    DIGEST_OVERRIDE_VALUES,
    SMALL_ARRAY_DIGEST_THRESHOLD,
    DigestAvailability,
    DigestContext,
    classify_digests,
    digest_availability,
)

# --- the ON rows: canary | reproduces | local | small-array -----------------


def test_canary_run_digests_on() -> None:
    d = classify_digests(DigestContext(is_canary=True, task_count=1000))
    assert d.digests_on is True
    assert "canary" in d.triggers


def test_reproduces_digests_on() -> None:
    d = classify_digests(DigestContext(reproduces=True, task_count=1000))
    assert d.digests_on is True
    assert "reproduces" in d.triggers


def test_local_context_digests_on() -> None:
    d = classify_digests(DigestContext(is_local=True, task_count=1000))
    assert d.digests_on is True
    assert "local" in d.triggers


def test_small_array_digests_on_at_threshold() -> None:
    d = classify_digests(DigestContext(task_count=SMALL_ARRAY_DIGEST_THRESHOLD))
    assert d.digests_on is True
    assert any(t.startswith("task_count") for t in d.triggers)


def test_small_array_digests_on_below_threshold() -> None:
    assert classify_digests(DigestContext(task_count=1)).digests_on is True


# --- the OFF row: big array, no other signal --------------------------------


def test_big_array_no_signal_digests_off() -> None:
    d = classify_digests(DigestContext(task_count=SMALL_ARRAY_DIGEST_THRESHOLD + 1))
    assert d.digests_on is False
    assert d.triggers == ()
    assert d.override_exercised is False
    assert "OFF" in d.reason


# --- the override wins both ways + exercise recorded ------------------------


def test_force_on_wins_over_big_array_off() -> None:
    d = classify_digests(DigestContext(task_count=1000, override="force_on"))
    assert d.digests_on is True
    assert d.override == "force_on"
    assert d.override_exercised is True


def test_force_off_wins_over_small_array_on() -> None:
    d = classify_digests(DigestContext(task_count=1, override="force_off"))
    assert d.digests_on is False
    assert d.override == "force_off"
    assert d.override_exercised is True


def test_force_off_wins_over_canary() -> None:
    d = classify_digests(DigestContext(is_canary=True, override="force_off"))
    assert d.digests_on is False
    assert d.override_exercised is True


def test_no_override_not_exercised() -> None:
    d = classify_digests(DigestContext(task_count=1))
    assert d.override is None
    assert d.override_exercised is False


def test_override_value_set_is_closed() -> None:
    assert DIGEST_OVERRIDE_VALUES == ("force_on", "force_off")


# --- the degradation path (off-when-needed → DISCLOSED, never fabricated) ----


def _digest_record(with_digest: bool) -> dict:
    atoms: dict = {"row_count": {"rows": 10, "dropped": 0}}
    if with_digest:
        atoms["digest"] = "abc123"
    return {"stage": "s", "atoms": atoms}


def test_all_stages_digested_present_no_disclosure() -> None:
    av = digest_availability([_digest_record(True), _digest_record(True)])
    assert av == DigestAvailability(present=True, stages_total=2, stages_with_digest=2)
    assert av.disclosure() is None


def test_no_digests_discloses_unrecorded() -> None:
    av = digest_availability([_digest_record(False), _digest_record(False)])
    assert av.present is False
    msg = av.disclosure()
    assert msg is not None
    assert "unrecorded" in msg
    # never fabricates a match — the disclosure NAMES the degradation.
    assert "degrades to whole-run comparison" in msg


def test_partial_digests_disclosed_as_partial() -> None:
    av = digest_availability([_digest_record(True), _digest_record(False)])
    assert av.present is False
    assert av.stages_with_digest == 1
    assert "PARTIAL" in (av.disclosure() or "")


def test_empty_trace_discloses_no_stages() -> None:
    av = digest_availability([])
    assert av.present is False
    assert "no stages" in (av.disclosure() or "")


def test_malformed_record_does_not_count() -> None:
    av = digest_availability([{"stage": "s"}, "not-a-dict"])  # type: ignore[list-item]
    assert av.stages_with_digest == 0
    assert av.stages_total == 2


@pytest.mark.parametrize("tc", [0, 1, SMALL_ARRAY_DIGEST_THRESHOLD])
def test_threshold_boundary_on(tc: int) -> None:
    assert classify_digests(DigestContext(task_count=tc)).digests_on is True
