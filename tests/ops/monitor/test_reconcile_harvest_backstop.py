"""reconcile's terminal harvest is journal-evidence-backstopped, not transition-only.

The rank-2 / audit-U8 latent bug (``docs/plans/transport-robustness-2026-07-17``):
reconcile's settle arms do ``update_run_status`` → ``mark_run(terminal)`` →
transition-gated ``harvest_on_terminal``. A session-death BETWEEN ``mark_run``
and the harvest leaves the run **terminal-with-no-harvest**, and the next
reconcile sees no verdict TRANSITION (the journal already reads terminal) — so a
transition-only gate would drop the guaranteed harvest forever. Unlike
``monitor_flow`` (which harvests from a ``finally``), reconcile has no such
backstop.

The fix derives "harvest owed" from DURABLE JOURNAL EVIDENCE — the
``<run_id>.harvest.jsonl`` ledger (``harvest_guard.harvest_receipt_exists``) —
not from the in-process verdict transition: a terminal run with NO receipt is
owed a harvest, so the mark→harvest gap re-fires exactly ONCE on the next
reconcile, and a run whose receipt already landed does not (idempotent both
ways).

Cluster-free: the three SSH fan-out calls + the announce read are monkeypatched,
and ``harvest_on_terminal`` is replaced by a counting fake that mirrors the real
guard's durable receipt append (so the idempotency-both-ways leg is exercised
end-to-end through the real ledger).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.ops.monitor import reconcile as recon
from hpc_agent.ops.monitor.harvest_guard import (
    harvest_marker_path,
    harvest_receipt_exists,
)
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(run_id: str, *, status: str, total_tasks: int = 2) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["13548839"],
        total_tasks=total_tasks,
        submitted_at="2026-07-03T00:00:00Z",
        experiment_dir="/exp",
        status=status,
    )


def _stub_cluster_all_complete(monkeypatch: pytest.MonkeyPatch, *, total_tasks: int = 2) -> None:
    """Nothing alive, no waves, every task complete → settle yields ``complete``."""
    report = {
        "summary": {
            "complete": total_tasks,
            "running": 0,
            "pending": 0,
            "failed": 0,
            "unknown": 0,
        },
        "waves": {},
    }
    monkeypatch.setattr(recon, "_ssh_status_report", lambda **_kw: report)
    monkeypatch.setattr(recon, "_ssh_list_combined_waves", lambda **_kw: [])
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: set())
    # Skip the crash-only announce fast path — go straight to the probe/settle arm.
    monkeypatch.setattr(recon, "read_announcements", lambda **_kw: None)


def _count_harvests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace ``harvest_on_terminal`` with a counting fake that writes a REAL
    receipt (mirroring the guard) so the receipt-derived idempotency is exercised.
    """
    causes: list[str] = []

    def _fake(
        experiment_dir: Path,
        run_id: str,
        *,
        terminal_cause: str,
        record: Any | None = None,
    ) -> dict[str, Any]:
        causes.append(terminal_cause)
        # The real guard's LAST step: append a durable marker to the ledger.
        append_jsonl_line(
            harvest_marker_path(experiment_dir, run_id),
            {"run_id": run_id, "terminal_cause": terminal_cause, "harvest_ok": True},
        )
        return {"run_id": run_id, "terminal_cause": terminal_cause, "harvest_ok": True}

    monkeypatch.setattr(recon, "harvest_on_terminal", _fake)
    return causes


def test_terminal_with_no_receipt_refires_harvest_exactly_once(tmp_path, monkeypatch):
    """The gap: a run already ``complete`` (mark landed) but with NO harvest
    ledger (harvest died before it ran) MUST re-fire the harvest on the next
    reconcile, and a THIRD reconcile (receipt now present) must NOT re-fire.
    """
    # Simulate the post-mark, pre-harvest death: terminal status, empty ledger.
    upsert_run(tmp_path, _record("gap_run", status="complete"))
    assert not harvest_receipt_exists(tmp_path, "gap_run")
    _stub_cluster_all_complete(monkeypatch)
    causes = _count_harvests(monkeypatch)

    # First reconcile: NO verdict transition (already complete) but NO receipt →
    # the backstop must fire the guaranteed harvest exactly once.
    recon.reconcile(tmp_path, "gap_run", scheduler="sge")
    assert causes == ["complete"], "terminal-with-no-receipt run must re-fire the harvest"
    assert harvest_receipt_exists(tmp_path, "gap_run")

    # Second reconcile: still no transition, but the receipt now exists →
    # idempotent, no re-fire (each fire pays an rsync pull + reduce + append).
    recon.reconcile(tmp_path, "gap_run", scheduler="sge")
    assert causes == ["complete"], "a run with a harvest receipt must NOT re-harvest"


def test_terminal_with_receipt_does_not_fire(tmp_path, monkeypatch):
    """Idempotency the other way: an already-terminal run WITH a receipt is not
    re-harvested — the backstop only fires on a genuine gap, never on a normal
    idempotent re-reconcile.
    """
    upsert_run(tmp_path, _record("done_run", status="complete"))
    # A prior harvest already landed its receipt.
    append_jsonl_line(
        harvest_marker_path(tmp_path, "done_run"),
        {"run_id": "done_run", "terminal_cause": "complete", "harvest_ok": True},
    )
    assert harvest_receipt_exists(tmp_path, "done_run")
    _stub_cluster_all_complete(monkeypatch)
    causes = _count_harvests(monkeypatch)

    recon.reconcile(tmp_path, "done_run", scheduler="sge")
    assert causes == [], "an already-harvested terminal run must not re-fire"


def test_normal_transition_fires_once_then_idempotent(tmp_path, monkeypatch):
    """The normal path is unchanged: a real ``in_flight`` → ``complete``
    transition harvests once (writing its receipt), and a re-reconcile does not.
    """
    upsert_run(tmp_path, _record("fresh_run", status="in_flight"))
    _stub_cluster_all_complete(monkeypatch)
    causes = _count_harvests(monkeypatch)

    recon.reconcile(tmp_path, "fresh_run", scheduler="sge")
    assert causes == ["complete"], "a real terminal transition must harvest"
    assert harvest_receipt_exists(tmp_path, "fresh_run")

    recon.reconcile(tmp_path, "fresh_run", scheduler="sge")
    assert causes == ["complete"], "an idempotent re-reconcile must not re-harvest"


def test_harvest_receipt_exists_predicate(tmp_path):
    """The durable-evidence predicate: absent ledger → no receipt; a real marker
    → receipt; a lone ``run_not_terminal`` abnormal-exit skip is NOT a receipt.
    """
    assert not harvest_receipt_exists(tmp_path, "none")

    append_jsonl_line(
        harvest_marker_path(tmp_path, "skip_only"),
        {"run_id": "skip_only", "harvest_skipped_reason": "run_not_terminal"},
    )
    assert not harvest_receipt_exists(tmp_path, "skip_only"), (
        "an abnormal-exit run_not_terminal skip records a no-op, not a harvest"
    )

    append_jsonl_line(
        harvest_marker_path(tmp_path, "real"),
        {"run_id": "real", "terminal_cause": "complete", "harvest_ok": True},
    )
    assert harvest_receipt_exists(tmp_path, "real")

    # A skip followed by a real harvest still counts (scan sees the real marker).
    append_jsonl_line(
        harvest_marker_path(tmp_path, "skip_then_real"),
        {"run_id": "skip_then_real", "harvest_skipped_reason": "run_not_terminal"},
    )
    append_jsonl_line(
        harvest_marker_path(tmp_path, "skip_then_real"),
        {"run_id": "skip_then_real", "terminal_cause": "abandoned", "harvest_ok": False},
    )
    assert harvest_receipt_exists(tmp_path, "skip_then_real")
