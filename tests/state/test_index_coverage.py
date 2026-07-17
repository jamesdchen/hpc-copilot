"""Behaviour-pinning coverage for :mod:`hpc_agent.state.index`.

Paired with ``test_journal_coverage.py`` — see that module's header for the
mutation-triage rationale (``docs/plans/mutation-triage-2026-07-17.md``,
finding-2: the journal/index provenance substrate ran DARK, with covered-but-
unasserted logic where a boundary/operator/default mutation would survive).

The index cache + its cross-run queries decide what a stranger's ``status`` /
``doctor`` read reports as live, stalled, parked, or prunable — a silent bug
here is a silent provenance failure. Each test kills a specific surviving
mutant (noted in its docstring).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.state.index import (
    _all_run_files,
    _index_is_stale,
    _read_index,
    _rebuild_index,
    _safe_mtime,
    find_held_runs,
    find_in_flight_runs,
    find_parked_runs,
    find_stalled_runs,
    find_submitting_runs,
    prune_terminal_runs,
)
from hpc_agent.state.journal import (
    mark_pending_decision,
    mark_pending_verdict,
    stamp_tick,
    upsert_run,
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
    run_id: str,
    *,
    status: str = "in_flight",
    job_ids: list[str] | None = None,
    campaign_id: str = "",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=job_ids if job_ids is not None else (["1"] if status != "submitting" else []),
        total_tasks=4,
        submitted_at="2026-07-17T00:00:00+00:00",
        experiment_dir="/exp",
        status=status,
        campaign_id=campaign_id,
    )


def _touch(experiment_dir: Path, run_id: str, mtime: float) -> None:
    import os

    os.utime(_run_path(experiment_dir, run_id), (mtime, mtime))


def _index(experiment_dir: Path) -> dict:
    data = json.loads((journal_dir(experiment_dir) / "index.json").read_text())
    assert isinstance(data, dict)
    return data


# ── _safe_mtime / _read_index / _all_run_files: the small guards ──────────────


def test_safe_mtime_of_vanished_file_is_zero(tmp_path: Path) -> None:
    """A file that vanished between glob and stat() must read as 0.0 (oldest), not
    crash a routine find_in_flight_runs. Kills the ``except OSError: return 0.0``
    guard."""
    assert _safe_mtime(tmp_path / "gone.json") == 0.0


def test_read_index_coerces_non_dict_to_empty(tmp_path: Path) -> None:
    """An index.json that is valid JSON but NOT a dict (e.g. a list) is coerced to
    {} — never handed to callers as a list. Kills the ``isinstance(payload, dict)``
    guard."""
    upsert_run(tmp_path, _record("r1"))
    idx_path = journal_dir(tmp_path) / "index.json"
    idx_path.write_text(json.dumps([1, 2, 3]))  # non-empty list, exercises isinstance
    assert _read_index(tmp_path) == {}


def test_all_run_files_empty_when_runs_dir_absent(tmp_path: Path) -> None:
    """No runs/ directory → [] (not a crash). Kills the ``if not rdir.exists()``
    guard."""
    assert _all_run_files(tmp_path / "nowhere") == []


# ── _index_is_stale: the strict-greater mtime boundary ────────────────────────


def test_index_not_stale_when_run_mtime_equals_index_mtime(tmp_path: Path) -> None:
    """A run file whose mtime EQUALS the index mtime is NOT newer, so the index is
    fresh. Kills the ``mtime > idx_mtime`` → ``>=`` mutation (which would force a
    needless rebuild every call)."""
    upsert_run(tmp_path, _record("r1"))
    _rebuild_index(tmp_path)
    # Pin both to the same exact value (identical os.utime rounding) → equal.
    idx_path = journal_dir(tmp_path) / "index.json"
    import os

    os.utime(idx_path, (1_000.0, 1_000.0))
    _touch(tmp_path, "r1", 1_000.0)
    assert _index_is_stale(tmp_path) is False


def test_index_stale_when_run_mtime_exceeds_index_mtime(tmp_path: Path) -> None:
    """A run file strictly newer than the index IS stale (a write landed after the
    last rebuild). Kills removal/negation of the staleness comparison."""
    upsert_run(tmp_path, _record("r1"))
    _rebuild_index(tmp_path)
    idx_path = journal_dir(tmp_path) / "index.json"
    import os

    os.utime(idx_path, (1_000.0, 1_000.0))
    _touch(tmp_path, "r1", 2_000.0)  # strictly newer
    assert _index_is_stale(tmp_path) is True


def test_index_stale_when_missing(tmp_path: Path) -> None:
    """A missing index (mtime 0.0) counts as stale so the caller rebuilds. Kills
    the ``if not idx_mtime: return True`` guard."""
    upsert_run(tmp_path, _record("r1"))
    (journal_dir(tmp_path) / "index.json").unlink()
    assert _index_is_stale(tmp_path) is True


# ── _rebuild_index: schema filter + default status ────────────────────────────


def test_rebuild_index_excludes_incompatible_schema(tmp_path: Path) -> None:
    """A run file with an unsupported schema_version is left OUT of the rebuilt
    index — the same compatibility gate load_run applies. Kills the
    ``is_compatible`` guard in the rebuild scan."""
    journal_dir(tmp_path)  # scaffold runs/ + namespace
    _atomic_write_json(
        _run_path(tmp_path, "good"),
        {**_record("good", status="complete").to_dict(), "schema_version": SCHEMA_VERSION},
    )
    _atomic_write_json(
        _run_path(tmp_path, "badver"),
        {**_record("badver").to_dict(), "schema_version": 999},
    )
    entries = _rebuild_index(tmp_path)
    assert "good" in entries
    assert entries["good"]["status"] == "complete"
    assert "updated_at" in entries["good"]
    assert "badver" not in entries  # incompatible schema excluded


def test_rebuild_index_defaults_missing_status_to_in_flight(tmp_path: Path) -> None:
    """A record payload with no ``status`` key rebuilds as ``in_flight``. Kills a
    mutation of the ``payload.get("status", "in_flight")`` default."""
    journal_dir(tmp_path)
    payload = {"run_id": "nostatus", "schema_version": SCHEMA_VERSION}
    _atomic_write_json(_run_path(tmp_path, "nostatus"), payload)
    entries = _rebuild_index(tmp_path)
    assert entries["nostatus"]["status"] == "in_flight"


# ── find_in_flight_runs: F42 trust-the-record + newest-first ──────────────────


def test_find_in_flight_excludes_run_terminal_on_disk_despite_stale_index(
    tmp_path: Path,
) -> None:
    """F42: a crash between a terminal run-write and its index refresh leaves the
    index claiming ``in_flight`` for a run that is terminal on disk. find_in_flight
    must trust the freshly-loaded record, not the stale index tag, or a finished
    run is reported live forever (doctor re-arms it; the campaign counts a
    phantom). Kills the ``if record.status != "in_flight": continue`` filter."""
    upsert_run(tmp_path, _record("r1", status="in_flight"))
    # A terminal transition landed on the run file; the index was NOT refreshed.
    _atomic_write_json(
        _run_path(tmp_path, "r1"),
        {**_record("r1").to_dict(), "status": "complete"},
    )
    # Force the index to look FRESH (run file older than index) so no rebuild
    # fires and the stale ``in_flight`` tag survives to be trusted-or-filtered.
    import os

    _touch(tmp_path, "r1", 1_000.0)
    os.utime(journal_dir(tmp_path) / "index.json", (2_000.0, 2_000.0))
    assert _index_is_stale(tmp_path) is False  # precondition: stale tag survives
    assert _index(tmp_path)["r1"]["status"] == "in_flight"  # index still lies

    assert find_in_flight_runs(tmp_path) == []  # record-truth wins


def test_find_in_flight_orders_newest_first(tmp_path: Path) -> None:
    """Records are returned newest-first by mtime. Kills a ``reverse=True`` → False
    sort mutation."""
    upsert_run(tmp_path, _record("older"))
    upsert_run(tmp_path, _record("newer"))
    _touch(tmp_path, "older", 1_000_000.0)
    _touch(tmp_path, "newer", 2_000_000.0)
    assert [r.run_id for r in find_in_flight_runs(tmp_path)] == ["newer", "older"]


# ── find_submitting_runs: F42 trust-the-record (independent implementation) ───


def test_find_submitting_excludes_run_promoted_on_disk_despite_stale_index(
    tmp_path: Path,
) -> None:
    """The submitting-set analogue of F42: a promote submitting→in_flight that
    landed on disk before its index refresh must NOT be reported as still
    submitting. Kills the ``if record.status != "submitting": continue`` filter."""
    upsert_run(tmp_path, _record("s1", status="submitting"))
    _atomic_write_json(
        _run_path(tmp_path, "s1"),
        {**_record("s1", status="submitting").to_dict(), "status": "in_flight", "job_ids": ["9"]},
    )
    import os

    _touch(tmp_path, "s1", 1_000.0)
    os.utime(journal_dir(tmp_path) / "index.json", (2_000.0, 2_000.0))
    assert _index_is_stale(tmp_path) is False
    assert find_submitting_runs(tmp_path) == []


# ── find_stalled_runs: deadline boundary + parked ≠ stalled ───────────────────


def test_find_stalled_excludes_deadline_equal_to_now(tmp_path: Path) -> None:
    """A run whose ``next_tick_due`` EQUALS now is not yet a miss (the deadline is
    the instant by which the next tick must run). Kills the ``due_dt < now_dt`` →
    ``<=`` mutation, which would false-flag a tick landing exactly on time."""
    now = "2026-07-17T01:00:00+00:00"
    upsert_run(tmp_path, _record("edge"))
    stamp_tick(
        "edge",
        last_tick_at="2026-07-17T00:55:00+00:00",
        next_tick_due=now,  # exactly now
        experiment_dir=tmp_path,
    )
    assert find_stalled_runs(now, experiment_dir=tmp_path) == []


def test_find_stalled_excludes_parked_on_decision(tmp_path: Path) -> None:
    """A run past its deadline BUT parked on a human decision is awaiting the
    human, not stalled (block-drive §5 "parked ≠ stalled"): it must be excluded so
    doctor does not false-alarm "re-arm?". A control run, past-deadline and NOT
    parked, still fires — proving the deadline itself is live. Kills the
    ``if is_awaiting_decision(...): continue`` skip."""
    now = "2026-07-17T02:00:00+00:00"
    past = "2026-07-17T01:00:00+00:00"
    for rid in ("parked", "control"):
        upsert_run(tmp_path, _record(rid))
        stamp_tick(
            rid,
            last_tick_at="2026-07-17T00:55:00+00:00",
            next_tick_due=past,
            experiment_dir=tmp_path,
        )
    mark_pending_decision(
        "parked",
        block="submit-s2",
        workflow="submit",
        brief={},
        resume_cursor={},
        awaiting_since=past,
        experiment_dir=tmp_path,
    )
    hits = {s["run_id"] for s in find_stalled_runs(now, experiment_dir=tmp_path)}
    assert hits == {"control"}


# ── find_parked_runs: fields + non-parked exclusion + malformed now ───────────


def test_find_parked_surfaces_parked_with_fields(tmp_path: Path) -> None:
    """A parked in_flight run surfaces with {run_id, status, block, workflow,
    awaiting_since}; a non-parked in_flight run does not. Kills the ``if not
    marker: continue`` skip and a wrong field-key projection."""
    now = "2026-07-17T02:00:00+00:00"
    upsert_run(tmp_path, _record("parked"))
    upsert_run(tmp_path, _record("busy"))  # in_flight but not parked
    mark_pending_decision(
        "parked",
        block="submit-s3",
        workflow="submit",
        brief={},
        resume_cursor={},
        awaiting_since="2026-07-17T01:30:00+00:00",
        experiment_dir=tmp_path,
    )
    parked = find_parked_runs(now, experiment_dir=tmp_path)
    assert len(parked) == 1
    entry = parked[0]
    assert entry["run_id"] == "parked"
    assert entry["status"] == "in_flight"
    assert entry["block"] == "submit-s3"
    assert entry["workflow"] == "submit"
    assert entry["awaiting_since"] == "2026-07-17T01:30:00+00:00"


def test_find_parked_rejects_malformed_now(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ISO-8601"):
        find_parked_runs("not-a-timestamp", experiment_dir=tmp_path)


# ── find_held_runs: newest-first ordering ─────────────────────────────────────


def test_find_held_orders_newest_first(tmp_path: Path) -> None:
    """Held runs are returned newest-first by mtime. Kills a ``reverse=True`` →
    False sort mutation."""
    escalation = {"decided_by": "code", "reason": "x"}
    for rid in ("old_held", "new_held"):
        upsert_run(tmp_path, _record(rid, status="failed"))
        mark_pending_verdict(tmp_path, rid, escalation=escalation)
    _touch(tmp_path, "old_held", 1_000_000.0)
    _touch(tmp_path, "new_held", 2_000_000.0)
    assert [r.run_id for r in find_held_runs(tmp_path)] == ["new_held", "old_held"]


# ── prune_terminal_runs: keep boundary + newest-kept + negative-keep guard ────


def test_prune_rejects_negative_keep(tmp_path: Path) -> None:
    """``keep < 0`` is a spec error, not a silent no-op / everything-pruned. Kills
    the ``if keep < 0: raise`` guard and its boundary."""
    with pytest.raises(errors.SpecInvalid):
        prune_terminal_runs(tmp_path, keep=-1)


def test_prune_returns_zero_when_terminal_count_equals_keep(tmp_path: Path) -> None:
    """With exactly ``keep`` terminal records, nothing is evicted. Kills the
    ``len(terminal) <= keep`` boundary → ``<`` (which would wrongly evict one)."""
    for i in range(2):
        upsert_run(tmp_path, _record(f"t{i}", status="complete"))
    assert prune_terminal_runs(tmp_path, keep=2) == 0
    assert {p.stem for p in _all_run_files(tmp_path)} == {"t0", "t1"}


def test_prune_keeps_newest_evicts_oldest(tmp_path: Path) -> None:
    """Prune keeps the NEWEST ``keep`` terminal records and evicts the oldest — the
    opposite ordering would garbage-collect the most recent provenance. Kills a
    ``reverse=True`` → False mutation on the eviction sort."""
    for rid, mtime in (("oldest", 1_000.0), ("middle", 2_000.0), ("newest", 3_000.0)):
        upsert_run(tmp_path, _record(rid, status="complete"))
        _touch(tmp_path, rid, mtime)
    removed = prune_terminal_runs(tmp_path, keep=1)
    assert removed == 2
    survivors = {p.stem for p in _all_run_files(tmp_path)}
    assert survivors == {"newest"}  # newest kept, two oldest evicted
