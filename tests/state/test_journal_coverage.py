"""Behaviour-pinning coverage for :mod:`hpc_agent.state.journal`.

These tests exist because the 2026-07-17 mutation triage
(``docs/plans/mutation-triage-2026-07-17.md``, finding-2) found the journal +
index provenance substrate entirely outside mutation scope: covered-but-
UNASSERTED logic where a boundary/operator/default/return mutation would
survive the suite. The journal IS the provenance record a stranger reads to
re-derive the citable table, so a silent journal bug is a silent
reproducibility failure.

Each test below adds an assertion that KILLS a specific surviving mutant. The
paired ``test_index_coverage.py`` does the same for :mod:`hpc_agent.state.index`.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

import pytest

from hpc_agent._kernel.contract.vocabulary import TERMINAL_STATUSES, JournalStatus
from hpc_agent.state.journal import (
    _RESUBMITTABLE_TERMINAL_STATUSES,
    _refresh_index_entry,
    clear_pending_decision,
    is_awaiting_decision,
    is_resubmittable_terminal,
    load_run,
    mark_pending_decision,
    mark_run,
    read_pending_decision,
    update_run_record,
    update_run_status,
    upsert_run,
    upsert_run_compare_and_mint,
)
from hpc_agent.state.run_record import (
    SCHEMA_VERSION,
    RunRecord,
    _atomic_write_json,
    _run_path,
    journal_dir,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(
    run_id: str = "r1",
    *,
    status: str = "in_flight",
    job_ids: list[str] | None = None,
    **overrides: object,
) -> RunRecord:
    base: dict = {
        "run_id": run_id,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "u@h",
        "remote_path": "/remote",
        "job_name": "j",
        "job_ids": job_ids if job_ids is not None else (["1"] if status != "submitting" else []),
        "total_tasks": 4,
        "submitted_at": "2026-07-17T00:00:00+00:00",
        "experiment_dir": "/exp",
        "status": status,
    }
    base.update(overrides)
    return RunRecord(**base)  # type: ignore[arg-type]


def _index(experiment_dir: Path) -> dict:
    data = json.loads((journal_dir(experiment_dir) / "index.json").read_text())
    assert isinstance(data, dict)
    return data


# ── _RESUBMITTABLE_TERMINAL_STATUSES + is_resubmittable_terminal ──────────────


def test_resubmittable_set_is_terminal_minus_complete(tmp_path: Path) -> None:
    """Pins the exact ``TERMINAL_STATUSES - {COMPLETE}`` membership. A mutant that
    widens the subtraction (drops COMPLETE from the exclusion) or empties it would
    change which terminal statuses a fresh submit PROCEEDS past vs dedups."""
    expected = {JournalStatus.FAILED, JournalStatus.ABANDONED}
    assert expected == _RESUBMITTABLE_TERMINAL_STATUSES
    assert JournalStatus.COMPLETE not in _RESUBMITTABLE_TERMINAL_STATUSES


def test_is_resubmittable_terminal_excludes_submitting(tmp_path: Path) -> None:
    """A ``submitting`` (pre-dispatch) record is neither resubmittable-terminal nor
    live — reconcile-recovery owns it. The status-predicate must return False
    (submitting is absent from the resubmittable set)."""
    assert is_resubmittable_terminal(_record(status="submitting")) is False
    # And in_flight is excluded too (belt-and-suspenders against a widened set).
    assert is_resubmittable_terminal(_record(status="in_flight")) is False


# ── load_run: schema-version + structural-integrity guards ────────────────────


def test_load_run_missing_schema_version_is_skipped(tmp_path: Path) -> None:
    """A record with NO ``schema_version`` (``None`` → not an int) is unsupported;
    load_run must warn and return None, not construct a record. Kills the
    ``isinstance(found, int)`` guard mutation."""
    rid = "noschema"
    _atomic_write_json(
        _run_path(tmp_path, rid),
        {**_record(rid).to_dict(), "schema_version": None},
    )
    with pytest.warns(UserWarning, match="schema_version"):
        assert load_run(tmp_path, rid) is None


def test_load_run_noninteger_schema_version_is_skipped(tmp_path: Path) -> None:
    """A stringy ``schema_version`` ("1") is not an int → unsupported → None."""
    rid = "strschema"
    _atomic_write_json(
        _run_path(tmp_path, rid),
        {**_record(rid).to_dict(), "schema_version": "1"},
    )
    with pytest.warns(UserWarning, match="schema_version"):
        assert load_run(tmp_path, rid) is None


def test_load_run_structurally_incomplete_record_is_skipped(tmp_path: Path) -> None:
    """A truncated v1 record (compatible schema_version, but a required field
    missing → ``RunRecord.from_dict`` raises TypeError) must be SKIPPED with a
    warning, never let the TypeError escape into callers. Kills the
    ``except TypeError`` swallow being removed / re-raising."""
    rid = "truncated"
    _atomic_write_json(
        _run_path(tmp_path, rid),
        {"run_id": rid, "schema_version": SCHEMA_VERSION},  # no profile/cluster/...
    )
    with pytest.warns(UserWarning, match="structurally incomplete"):
        assert load_run(tmp_path, rid) is None


# ── update_run_status / update_run_record: missing-record + RMW freshness ─────


def test_update_run_status_raises_on_missing_record(tmp_path: Path) -> None:
    """No record for run_id → FileNotFoundError (fail loud, not a silent create)."""
    with pytest.raises(FileNotFoundError):
        update_run_status(tmp_path, "ghost", combined_waves=[1])


def test_update_run_record_applies_mutation_and_reads_fresh(tmp_path: Path) -> None:
    """The mutate callback receives the LIVE on-disk record and its changes persist;
    a second call reads the freshly-written value and accumulates onto it (the
    exact append-to-list use-case that motivated this over update_run_status)."""
    upsert_run(tmp_path, _record("r1"))

    def _append_seven(rec: RunRecord) -> None:
        rec.combined_waves = [*rec.combined_waves, 7]

    out1 = update_run_record(tmp_path, "r1", _append_seven)
    assert out1.combined_waves == [7]
    out2 = update_run_record(tmp_path, "r1", _append_seven)
    assert out2.combined_waves == [7, 7]  # read-fresh, not a stale snapshot
    reloaded = load_run(tmp_path, "r1")
    assert reloaded is not None and reloaded.combined_waves == [7, 7]


def test_update_run_record_raises_on_missing_record(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        update_run_record(tmp_path, "ghost", lambda _rec: None)


# ── upsert_run_compare_and_mint: the locked read-decide-write discipline ──────


def test_cam_mints_when_decide_returns_record(tmp_path: Path) -> None:
    """decide returns a record with NO existing → written atomically, index
    refreshed, returns (record, True)."""
    seen: list[RunRecord | None] = []

    def decide(existing: RunRecord | None) -> RunRecord | None:
        seen.append(existing)
        return _record("mint", status="submitting")

    rec, minted = upsert_run_compare_and_mint(tmp_path, "mint", decide)
    assert minted is True
    assert rec.status == "submitting"
    assert seen == [None]  # decide saw "no existing record"
    on_disk = load_run(tmp_path, "mint")
    assert on_disk is not None and on_disk.status == "submitting"
    assert _index(tmp_path)["mint"]["status"] == "submitting"


def test_cam_no_mint_when_decide_returns_none_with_existing(tmp_path: Path) -> None:
    """decide returns None while a record EXISTS → the existing record stands,
    returns (existing, False), and NOTHING is overwritten. decide is handed the
    loaded existing record (not None)."""
    upsert_run(tmp_path, _record("live", status="in_flight", job_ids=["1"]))
    seen: list[RunRecord | None] = []

    def decide(existing: RunRecord | None) -> RunRecord | None:
        seen.append(existing)
        return None

    rec, minted = upsert_run_compare_and_mint(tmp_path, "live", decide)
    assert minted is False
    assert rec.status == "in_flight"
    assert seen[0] is not None and seen[0].run_id == "live"


def test_cam_raises_when_decide_returns_none_without_existing(tmp_path: Path) -> None:
    """decide returns None with NO existing record → ValueError (nothing to mint,
    nothing to return). Kills the ``if existing is None: raise`` guard."""
    with pytest.raises(ValueError, match="nothing to mint"):
        upsert_run_compare_and_mint(tmp_path, "ghost", lambda _e: None)


def test_cam_propagates_decide_refusal_without_writing(tmp_path: Path) -> None:
    """decide may RAISE to refuse the mint; the exception propagates (lock
    released) and no record is written."""

    class _Refuse(Exception):
        pass

    def decide(_existing: RunRecord | None) -> RunRecord | None:
        raise _Refuse

    with pytest.raises(_Refuse):
        upsert_run_compare_and_mint(tmp_path, "refused", decide)
    assert load_run(tmp_path, "refused") is None


def test_cam_concurrent_double_mint_serializes(tmp_path: Path) -> None:
    """The load-decide-write critical section shares ONE per-run flock, so two
    genuinely concurrent same-run_id callers serialize: exactly ONE sees no
    existing record and mints; the other reads the minted record and dedups. If
    the read and the write did NOT share the lock, both could see None and mint."""
    journal_dir(tmp_path)  # prime the namespace so threads race only on the run lock
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def decide(existing: RunRecord | None) -> RunRecord | None:
        return _record("race", status="submitting") if existing is None else None

    def worker(name: str) -> None:
        barrier.wait()
        _rec, minted = upsert_run_compare_and_mint(tmp_path, "race", decide)
        outcomes[name] = "minted" if minted else "dedup"

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes.values()) == ["dedup", "minted"]
    assert load_run(tmp_path, "race") is not None


# ── mark_run: validation + transition + optional-stage ────────────────────────


def test_mark_run_rejects_invalid_status(tmp_path: Path) -> None:
    """A status outside the JournalStatus enum → ValueError. Kills the
    ``status not in set(JournalStatus)`` guard."""
    upsert_run(tmp_path, _record("r1"))
    with pytest.raises(ValueError, match="invalid status"):
        mark_run(tmp_path, "r1", status="bogus")


def test_mark_run_raises_on_missing_record(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        mark_run(tmp_path, "ghost", status="complete")


def test_mark_run_leaves_stage_untouched_when_none(tmp_path: Path) -> None:
    """``stage=None`` updates status only; the prior stage survives. Kills a mutant
    that always writes stage (or drops the ``if stage is not None`` guard)."""
    upsert_run(tmp_path, _record("r1", stage="monitor"))
    rec = mark_run(tmp_path, "r1", status="complete")
    assert rec.status == "complete"
    assert rec.stage == "monitor"  # unchanged


def test_mark_run_sets_stage_when_supplied(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("r1", stage="monitor"))
    rec = mark_run(tmp_path, "r1", status="complete", stage="done")
    assert rec.status == "complete"
    assert rec.stage == "done"


# ── pending-decision envelope round-trip ("parked ≠ stalled") ─────────────────


def test_pending_decision_envelope_round_trips_all_keys(tmp_path: Path) -> None:
    """mark → read returns the full ``{block, workflow, brief, resume_cursor,
    awaiting_since, cmd_sha}`` envelope; a dropped/renamed key would survive
    without pinning the whole shape. ``cmd_sha`` defaults to None."""
    upsert_run(tmp_path, _record("r1"))
    mark_pending_decision(
        "r1",
        block="submit-s2",
        workflow="submit",
        brief={"digest": "d"},
        resume_cursor={"run_id": "r1", "next_verb": None},
        awaiting_since="2026-07-17T05:00:00+00:00",
        experiment_dir=tmp_path,
    )
    marker = read_pending_decision("r1", experiment_dir=tmp_path)
    assert marker == {
        "block": "submit-s2",
        "workflow": "submit",
        "brief": {"digest": "d"},
        "resume_cursor": {"run_id": "r1", "next_verb": None},
        "awaiting_since": "2026-07-17T05:00:00+00:00",
        "cmd_sha": None,
    }
    assert is_awaiting_decision("r1", experiment_dir=tmp_path) is True


def test_pending_decision_clear_is_idempotent_and_read_returns_empty(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("r1"))
    mark_pending_decision(
        "r1",
        block="submit-s2",
        workflow="submit",
        brief={},
        resume_cursor={},
        awaiting_since="2026-07-17T05:00:00+00:00",
        cmd_sha="deadbeef",
        experiment_dir=tmp_path,
    )
    assert is_awaiting_decision("r1", experiment_dir=tmp_path) is True
    clear_pending_decision("r1", experiment_dir=tmp_path)
    clear_pending_decision("r1", experiment_dir=tmp_path)  # idempotent, must not raise
    assert is_awaiting_decision("r1", experiment_dir=tmp_path) is False
    assert read_pending_decision("r1", experiment_dir=tmp_path) == {}


def test_read_pending_decision_missing_record_returns_empty(tmp_path: Path) -> None:
    """A missing run is not parked → {} (not a crash). Kills the ``if record is
    None: return {}`` guard."""
    assert read_pending_decision("ghost", experiment_dir=tmp_path) == {}
    assert is_awaiting_decision("ghost", experiment_dir=tmp_path) is False


# ── _refresh_index_entry: fresh on-disk status wins over a stale caller arg ────


def test_refresh_index_entry_reads_fresh_status_not_caller_arg(tmp_path: Path) -> None:
    """The index refresh RE-READS the run file under the index lock and installs
    that fresh status, using the caller-supplied ``status`` only as a fallback. If
    the run file on disk is terminal but the caller passes a stale ``in_flight``,
    the index must record the FRESH terminal status — the lost-update guard that
    stops writer A's stale snapshot clobbering writer B's terminal transition."""
    upsert_run(tmp_path, _record("r1", status="in_flight"))
    assert _index(tmp_path)["r1"]["status"] == "in_flight"

    # Another writer landed a terminal transition on the run file (index not yet
    # refreshed for it).
    terminal = {**_record("r1").to_dict(), "status": "complete"}
    _atomic_write_json(_run_path(tmp_path, "r1"), terminal)

    # A stale refresh arrives carrying the OLD status.
    _refresh_index_entry(tmp_path, "r1", "in_flight")

    assert _index(tmp_path)["r1"]["status"] == "complete"  # fresh disk read won
    assert JournalStatus.COMPLETE in TERMINAL_STATUSES  # sanity on the vocabulary
