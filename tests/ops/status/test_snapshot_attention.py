"""Status-snapshot v2: the additive ``attention`` brief field (attention-queue D4).

The snapshot embeds THE SAME ordered projection the ``attention-queue`` verb
renders — one ordering definition (``ops/attention_queue.py::collect_queue``)
serves both surfaces, so the in-flow morning read and the standalone digest can
never disagree. This pins: (1) the field is present and equals ``collect_queue``
byte-for-byte in ordering; (2) the snapshot does not re-sort/re-collect (it calls
the shared seat); (3) additive-only — an empty queue yields ``[]`` and every other
brief field is unchanged.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from hpc_agent._wire.workflows.status_blocks import StatusSnapshotSpec
from hpc_agent.ops.attention_queue import collect_queue
from hpc_agent.ops.status_blocks import status_snapshot
from hpc_agent.state.journal import stamp_tick, upsert_run
from hpc_agent.state.run_record import RunRecord

_NOW = "2026-07-06T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _mk(exp: Path, run_id: str, *, status: str = "in_flight", **kw: object) -> RunRecord:
    rec = RunRecord(
        run_id=run_id,
        profile="prof",
        cluster="hoffman2",
        ssh_target="user@hoffman2",
        remote_path="/scratch/run",
        job_name="job",
        job_ids=["1"],
        total_tasks=10,
        submitted_at="2026-07-06T00:00:00+00:00",
        experiment_dir=str(exp),
        status=status,
        **kw,  # type: ignore[arg-type]
    )
    upsert_run(exp, rec)
    return rec


def test_snapshot_brief_carries_attention_ordered_by_the_one_seat(tmp_path: Path) -> None:
    _mk(tmp_path, "run-stalled")
    stamp_tick(
        "run-stalled",
        last_tick_at="2026-07-06T05:00:00+00:00",
        next_tick_due="2026-07-06T06:00:00+00:00",
        experiment_dir=tmp_path,
    )
    _mk(tmp_path, "run-failed", status="failed", last_tick_at="2026-07-06T06:00:00+00:00")

    result = status_snapshot(tmp_path, spec=StatusSnapshotSpec(now_iso=_NOW, mark_seen=False))
    attention = result.brief["attention"]

    # Ordering matches the ONE seat byte-for-byte (the snapshot never re-sorts).
    expected = collect_queue(tmp_path, now=_NOW)
    assert [(d["kind"], d["subject"]["scope_id"]) for d in attention] == [
        (i.kind, i.scope_id) for i in expected
    ]
    # The D1 wire shape (with the D2-revision fan-out key) rides each item.
    assert all("unblocks" in d and "class" in d for d in attention)


def test_snapshot_attention_is_empty_and_additive_when_nothing_pending(tmp_path: Path) -> None:
    _mk(tmp_path, "run-live")  # in_flight, not stalled → nothing needs attention
    result = status_snapshot(tmp_path, spec=StatusSnapshotSpec(now_iso=_NOW, mark_seen=False))
    assert result.brief["attention"] == []
    # Additive only: the pre-existing brief keys are all still present.
    for key in (
        "now",
        "running_where",
        "changed_since_seen",
        "stalled_runs",
        "anomalies",
        "alerts",
        "open_ssh_circuits",
    ):
        assert key in result.brief


def test_snapshot_calls_the_shared_collect_queue_seat_not_a_local_sort() -> None:
    """The one-ordering seat: status_snapshot routes through collect_queue and never
    re-implements the D2 sort inline."""
    src = inspect.getsource(status_snapshot)
    assert "collect_queue(" in src
    assert "order_items(" not in src  # no local re-sort — the seat owns ordering
