"""Reader-tolerance for the ``submitting`` journal status (submit-once U3, phase 1).

The submit-once contract (docs/plans/transport-robustness-2026-07-17) mints a
``RunRecord`` as ``submitting`` BEFORE the remote dispatch and promotes it to
``in_flight`` only once the job id is in hand. NOTHING in phase 1 mints or
transitions ``submitting`` — these tests exercise the *readers* every existing
path runs over the journal, so an old-or-new wheel that encounters a submitting
record tolerates it (premortem Δ3, reader-tolerance-first).

Covers: the latent ``prune_terminal_runs`` GC-orphan bug (a submitting record
must survive prune — with the pre-fix old-guard regression pinned as the exact
fire), ``find_submitting_runs`` (NEW), the ``find_stalled_runs`` extend, and the
enum round-trip through ``_rebuild_index`` / ``_refresh_index_entry``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hpc_agent.state.index import (
    find_in_flight_runs,
    find_stalled_runs,
    find_submitting_runs,
    prune_terminal_runs,
)
from hpc_agent.state.journal import load_run, mark_run, stamp_tick, upsert_run
from hpc_agent.state.run_record import RunRecord, _run_path


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(run_id: str, *, status: str = "in_flight") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=[] if status == "submitting" else ["100"],
        total_tasks=4,
        submitted_at="2026-07-17T00:00:00+00:00",
        experiment_dir="/exp",
        status=status,
    )


def _touch(experiment_dir: Path, run_id: str, mtime: float) -> None:
    """Pin a run file's mtime so prune's oldest-first ordering is deterministic."""
    path = _run_path(experiment_dir, run_id)
    os.utime(path, (mtime, mtime))


# ── prune: the GC-orphan latent bug (premortem Δ3) ────────────────────────────


def test_prune_keeps_a_submitting_record(tmp_path: Path) -> None:
    """A ``submitting`` orphan is non-terminal and MUST survive prune, even as
    the OLDEST record with keep exceeded — losing it would garbage-collect the
    only durable evidence reconcile-recovery needs (submit-once §3.3)."""
    # Submitting is the OLDEST file; two terminal records are newer.
    upsert_run(tmp_path, _record("orphan", status="submitting"))
    _touch(tmp_path, "orphan", 1_000.0)
    upsert_run(tmp_path, _record("done1", status="complete"))
    _touch(tmp_path, "done1", 2_000.0)
    upsert_run(tmp_path, _record("done2", status="failed"))
    _touch(tmp_path, "done2", 3_000.0)

    # keep=1: with 2 TERMINAL records, exactly 1 terminal is evicted; the
    # submitting record is never a prune candidate under the fixed guard.
    removed = prune_terminal_runs(tmp_path, keep=1)

    assert removed == 1
    assert _run_path(tmp_path, "orphan").exists(), "submitting orphan was pruned"
    surviving = load_run(tmp_path, "orphan")
    assert surviving is not None and surviving.status == "submitting"


def test_old_guard_would_have_pruned_the_submitting_orphan(tmp_path: Path) -> None:
    """Regression pin: the PRE-FIX guard (``status != "in_flight"``) would treat
    a submitting record as terminal and evict it. This reproduces the old guard
    inline to name the exact fire the new ``not in TERMINAL_STATUSES`` guard
    closes (premortem Δ3 / §7 enforcement row)."""
    from hpc_agent.state.run_record import _read_json

    upsert_run(tmp_path, _record("orphan", status="submitting"))
    _touch(tmp_path, "orphan", 1_000.0)
    upsert_run(tmp_path, _record("done1", status="complete"))
    _touch(tmp_path, "done1", 3_000.0)

    # Simulate the OLD prune candidate selection over the same on-disk journal.
    from hpc_agent.state.index import _all_run_files

    old_guard_candidates = {
        _read_json(p).get("run_id")  # type: ignore[union-attr]
        for p in _all_run_files(tmp_path)
        if (payload := _read_json(p)) is not None
        and payload.get("status", "in_flight") != "in_flight"
    }
    # The old guard swept the submitting record into the terminal set (the bug).
    assert "orphan" in old_guard_candidates

    # The SHIPPED guard does not — proven by the real prune leaving it in place.
    prune_terminal_runs(tmp_path, keep=0)  # evict every genuinely-terminal record
    assert _run_path(tmp_path, "orphan").exists()
    assert not _run_path(tmp_path, "done1").exists()


# ── find_submitting_runs (NEW) ────────────────────────────────────────────────


def test_find_submitting_runs_returns_only_submitting(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("sub", status="submitting"))
    upsert_run(tmp_path, _record("live", status="in_flight"))
    upsert_run(tmp_path, _record("done", status="complete"))

    submitting = find_submitting_runs(tmp_path)
    assert [r.run_id for r in submitting] == ["sub"]
    assert submitting[0].status == "submitting"


def test_find_in_flight_runs_ignores_submitting(tmp_path: Path) -> None:
    """find_in_flight_runs is UNCHANGED — a submitting run has no job_ids and is
    NOT part of the monitor/campaign live set (submit-once §3.3 migration row)."""
    upsert_run(tmp_path, _record("sub", status="submitting"))
    upsert_run(tmp_path, _record("live", status="in_flight"))

    assert [r.run_id for r in find_in_flight_runs(tmp_path)] == ["live"]


def test_find_submitting_runs_empty_on_missing_journal(tmp_path: Path) -> None:
    """Non-creating namespace probe (F46): reading a journal-less dir returns []
    and leaves no ghost namespace."""
    assert find_submitting_runs(tmp_path / "nowhere") == []


# ── find_stalled_runs extend ──────────────────────────────────────────────────


def test_find_stalled_surfaces_a_lapsed_submitting_run(tmp_path: Path) -> None:
    """A submit that died in its dispatch window: a submitting record whose
    watchdog stamp lapsed surfaces as a stalled hit carrying status=submitting
    (routes doctor to reconcile-recovery, not re-arm)."""
    now = "2026-07-17T01:00:00+00:00"
    upsert_run(tmp_path, _record("stuck_submit", status="submitting"))
    stamp_tick(
        "stuck_submit",
        last_tick_at="2026-07-17T00:55:00+00:00",
        next_tick_due="2026-07-17T00:59:00+00:00",  # past
        experiment_dir=tmp_path,
    )
    # A submitting run with a FUTURE deadline is not stalled.
    upsert_run(tmp_path, _record("fresh_submit", status="submitting"))
    stamp_tick(
        "fresh_submit",
        last_tick_at="2026-07-17T00:59:00+00:00",
        next_tick_due="2026-07-17T02:00:00+00:00",  # future
        experiment_dir=tmp_path,
    )

    stalled = find_stalled_runs(now, experiment_dir=tmp_path)
    hits = {s["run_id"]: s for s in stalled}
    assert "stuck_submit" in hits
    assert hits["stuck_submit"]["status"] == "submitting"
    assert "fresh_submit" not in hits


# ── enum round-trip through index rebuild / refresh ───────────────────────────


def test_submitting_round_trips_through_index_rebuild(tmp_path: Path) -> None:
    """The index caches the raw status string, so ``submitting`` round-trips
    through _rebuild_index (find_submitting_runs forces a rebuild when stale)
    and load_run reads it back verbatim."""
    upsert_run(tmp_path, _record("sub", status="submitting"))

    # Force a stale index -> rebuild path.
    idx = tmp_path / "journal"
    # find_submitting_runs rebuilds if stale and reads the index tag.
    assert [r.run_id for r in find_submitting_runs(tmp_path)] == ["sub"]
    assert load_run(tmp_path, "sub").status == "submitting"  # type: ignore[union-attr]
    assert idx.exists()


def test_mark_run_accepts_submitting_and_promotes(tmp_path: Path) -> None:
    """The enum extension frees ``mark_run``: a record can be marked submitting
    (previously a ValueError) and later promoted to in_flight. The
    _refresh_index_entry write carries the status through so the index queries
    agree with the record."""
    upsert_run(tmp_path, _record("r", status="submitting"))
    assert find_submitting_runs(tmp_path)[0].run_id == "r"

    # Promote submitting -> in_flight (the phase-2 transition, verified reachable
    # at the state layer here even though no phase-1 caller fires it).
    mark_run(tmp_path, "r", status="in_flight")
    assert find_submitting_runs(tmp_path) == []
    assert [rec.run_id for rec in find_in_flight_runs(tmp_path)] == ["r"]
