"""FIX-4: ``submit_and_record`` stamps an INITIAL ``next_tick_due`` at submit.

A run whose driver dies BEFORE its first monitor tick used to have no
``next_tick_due`` — and :func:`hpc_agent.state.index.find_stalled_runs`
permanently skips a never-ticked run, an undetectable stall. The fresh in_flight
record now carries ``last_tick_at`` + ``next_tick_due`` (INITIAL_GRACE = the
driver's fallback cadence) so the §5 watchdog can see it immediately.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent._wire.actions.submit import SubmitSpec
from hpc_agent.ops.submit.runner import submit_and_record
from hpc_agent.state.journal import load_run

if TYPE_CHECKING:
    from pathlib import Path


def _spec(run_id: str) -> SubmitSpec:
    return SubmitSpec(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["100"],
        total_tasks=4,
    )


def test_fresh_record_carries_initial_next_tick_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))

    record, deduped = submit_and_record(tmp_path, spec=_spec("r_init"))
    assert deduped is False

    # The stamp is a locked RMW on the journal file (not the in-memory record),
    # so read it back: the durable record carries both watchdog fields.
    reloaded = load_run(tmp_path, "r_init")
    assert reloaded is not None
    assert reloaded.next_tick_due, "never-ticked run must carry an initial next_tick_due"
    assert reloaded.last_tick_at
    # The initial deadline is the driver's fallback cadence AFTER last_tick_at.
    assert reloaded.next_tick_due > reloaded.last_tick_at
