"""The TRUE scheduler-accept stamp (s2-readiness drift-log seam b).

``y_to_array_accepted_seconds`` is an SLO *of* the interval from the journaled
greenlight to the scheduler accepting the array. It shipped measuring to
``RunRecord.submitted_at``, which on the submit-once path is stamped at MINT —
before the dispatch — so every reading was short by the dispatch duration. On a
cold login node under load that is not a rounding error, and an SLO that
systematically under-reports the thing it exists to drive down is worse than no
SLO: it improves on paper whenever dispatch gets slower.

This module pins the closure end to end: ``promote_submitting_record`` (the one
site that runs with the parsed job ids in hand — which is what "accepted" means)
writes ``accepted_at`` in the SAME locked write as the ids, and the reducer
prefers it while falling back to ``submitted_at`` so nothing has to be backfilled.

Every instant is injected; nothing here reads a wall clock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent.ops.submit.runner import mint_submitting_record, promote_submitting_record
from hpc_agent.state import run_record, s2_slo
from hpc_agent.state.journal import load_run
from hpc_agent.state.run_record import RunRecord
from tests._no_network import no_network  # noqa: F401 — autouse zero-network tripwire

if TYPE_CHECKING:
    from pathlib import Path

MINTED_AT = "2026-07-30T20:00:00Z"
ACCEPTED_AT = "2026-07-30T20:04:00Z"
GREENLIT_AT = "2026-07-30T19:55:00Z"


def _greenlight(ts: str = GREENLIT_AT) -> dict:
    return {
        "block": "submit-s2",
        "response": "y",
        "ts": ts,
        "resolved": {"next_block": "submit-s2"},
    }


def _record(**kw: object) -> RunRecord:
    base = {
        "run_id": "run-accept",
        "profile": "p",
        "cluster": "c",
        "ssh_target": "u@h",
        "remote_path": "/scratch/x",
        "job_name": "j",
        "job_ids": ["1"],
        "total_tasks": 4,
        "submitted_at": MINTED_AT,
        "experiment_dir": "/exp",
    }
    base.update(kw)
    return RunRecord(**base)  # type: ignore[arg-type]


# ── the stamp is written where acceptance actually happens ───────────────────


def _mint(exp: Path, run_id: str) -> None:
    mint_submitting_record(
        exp,
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/scratch/x",
        job_name="j",
        total_tasks=4,
    )


def test_promote_stamps_accepted_at_with_the_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    exp = tmp_path / "exp"
    exp.mkdir()
    _mint(exp, "run-p")
    rec = promote_submitting_record(exp, "run-p", ["98765"], accepted_at=ACCEPTED_AT)
    assert rec.accepted_at == ACCEPTED_AT
    assert rec.job_ids == ["98765"]
    # Durable, not just on the returned object.
    reloaded = load_run(exp, "run-p")
    assert reloaded is not None and reloaded.accepted_at == ACCEPTED_AT


def test_the_stamp_and_the_ids_land_in_ONE_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They date each other, so they must not be separable by a crash. A record
    carrying ids with no accept instant would silently fall back to the
    pre-dispatch ``submitted_at`` and under-report the very latency the stamp
    exists to fix — a regression that would look like nothing at all.
    """
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    exp = tmp_path / "exp"
    exp.mkdir()
    _mint(exp, "run-w")

    seen: list[set[str]] = []
    from hpc_agent.state import journal

    real = journal.update_run_status

    def _spy(experiment_dir, run_id, **fields):  # type: ignore[no-untyped-def]
        seen.append(set(fields))
        return real(experiment_dir, run_id, **fields)

    monkeypatch.setattr(journal, "update_run_status", _spy)
    promote_submitting_record(exp, "run-w", ["42"], accepted_at=ACCEPTED_AT)
    assert seen == [{"job_ids", "accepted_at"}]


def test_accepted_at_defaults_to_now_when_not_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production passes nothing; the stamp must still be written, not skipped.

    And it must be written in the SAME format as ``submitted_at`` (the repo's
    ``utcnow_iso`` convention): the two are alternatives for one slot in the
    reducer, so a divergent shape would make the measurement depend on which
    branch of the fallback fired.
    """
    from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    exp = tmp_path / "exp"
    exp.mkdir()
    _mint(exp, "run-d")
    rec = promote_submitting_record(exp, "run-d", ["7"])
    assert rec.accepted_at
    assert parse_iso_utc_or_none(rec.accepted_at) is not None
    # Same offset spelling and same precision as the stamp it substitutes for.
    assert rec.accepted_at[-6:] == utcnow_iso()[-6:] == "+00:00"
    assert len(rec.accepted_at) == len(utcnow_iso())


def test_accepted_at_is_whitelisted_for_the_locked_write() -> None:
    """An un-whitelisted field raises in ``update_run_status`` — which would make
    every promote fail. Pinned so the field and its permission cannot drift apart.
    """
    assert "accepted_at" in run_record._UPDATABLE_FIELDS


# ── the reducer prefers it, and falls back exactly ───────────────────────────


def test_the_reducer_measures_to_the_accept_stamp_not_the_mint() -> None:
    """9 minutes to accept, not the 5 the pre-dispatch stamp would have shown."""
    slo = s2_slo.compute_slo(
        [_greenlight()], accepted_at=s2_slo.accept_stamp(_record(accepted_at=ACCEPTED_AT))
    )
    assert slo.y_to_array_accepted_seconds == 9 * 60


def test_the_under_estimate_is_what_the_stamp_removes() -> None:
    """The bug, stated as a number: reading ``submitted_at`` on the submit-once
    path loses the whole dispatch duration."""
    with_stamp = s2_slo.compute_slo(
        [_greenlight()], accepted_at=s2_slo.accept_stamp(_record(accepted_at=ACCEPTED_AT))
    )
    without = s2_slo.compute_slo([_greenlight()], accepted_at=MINTED_AT)
    assert without.y_to_array_accepted_seconds is not None
    assert with_stamp.y_to_array_accepted_seconds is not None
    assert with_stamp.y_to_array_accepted_seconds - without.y_to_array_accepted_seconds == 4 * 60


def test_a_record_without_the_stamp_falls_back_to_submitted_at() -> None:
    """Exact, not degraded, where it fires: on the ``submit_and_record`` path
    ``submitted_at`` is itself taken with the ids parsed, so it already MEANS
    accepted. Records written before the field existed keep reading unchanged —
    which is why this closure needed no backfill.
    """
    assert s2_slo.accept_stamp(_record()) == MINTED_AT
    slo = s2_slo.compute_slo([_greenlight()], accepted_at=s2_slo.accept_stamp(_record()))
    assert slo.y_to_array_accepted_seconds == 5 * 60


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_blank_stamp_falls_back_rather_than_erasing_the_measurement(blank: object) -> None:
    """A blank ``accepted_at`` is not a measurement. Preferring it would make the
    number vanish instead of degrading to what the reducer used before.
    """
    assert s2_slo.accept_stamp(_record(accepted_at=blank)) == MINTED_AT


def test_both_stamps_absent_yields_no_measurement_never_a_zero() -> None:
    assert s2_slo.accept_stamp(_record(submitted_at="", accepted_at=None)) is None
    slo = s2_slo.compute_slo([_greenlight()], accepted_at=None)
    assert slo.y_to_array_accepted_seconds is None
    # A missing field never poisons the others.
    assert slo.interventions_count == 1


def test_slo_for_run_routes_through_the_one_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second reader picking its own stamp would reintroduce the under-estimate
    under a different name, so the whole reducer has ONE accept definition."""
    captured: dict[str, object] = {}

    def _spy(records, *, accepted_at, host=""):  # type: ignore[no-untyped-def]
        captured["accepted_at"] = accepted_at
        return s2_slo.S2Slo()

    monkeypatch.setattr(s2_slo, "compute_slo", _spy)
    s2_slo.slo_for_run(tmp_path, "run-accept", _record(accepted_at=ACCEPTED_AT))
    assert captured["accepted_at"] == ACCEPTED_AT


def test_the_readiness_age_is_still_dated_against_the_FIRE_not_the_accept() -> None:
    """The two stamps answer different questions and must not be conflated: the
    accept instant ends the measured interval, while readiness age asks how stale
    the ledger was when S2 FIRED. Dating readiness against the accept would
    silently add the dispatch duration to the ledger's apparent age.
    """
    slo = s2_slo.compute_slo(
        [_greenlight()],
        accepted_at=s2_slo.accept_stamp(_record(accepted_at=ACCEPTED_AT)),
        host="",
    )
    # No host -> no ledger lookup at all; honest None, never zero.
    assert slo.readiness_age_at_fire_seconds is None
