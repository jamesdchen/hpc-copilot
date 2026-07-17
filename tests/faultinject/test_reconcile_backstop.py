"""Reconcile settle-arm harvest backstop — AUDIT rank-2 / U8.

The AUDIT (rank 2, §7 'Session-death between mark_run(terminal) and
harvest_on_terminal') flagged a latent correctness bug: a transition-ONLY harvest
gate drops the guaranteed harvest forever when a session dies between
``mark_run(terminal)`` and ``harvest_on_terminal`` (the re-reconcile sees no
transition). Since the audit, ``reconcile._harvest_if_owed`` gained a
JOURNAL-EVIDENCE backstop: a terminal run with NO harvest receipt re-fires the
harvest exactly once. These drills pin that fix — the injected fault is the
absent receipt (the durable evidence a session-death left behind).

If this fix regresses, the ``test_no_transition_no_receipt`` drill flips red,
which is the intended fix-signal for U8.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hpc_agent.ops.monitor import reconcile


def test_transition_fires_harvest(monkeypatch, tmp_path) -> None:
    """The normal path: a verdict TRANSITION (running → failed) fires the harvest."""
    fired = MagicMock()
    monkeypatch.setattr(reconcile, "harvest_on_terminal", fired)
    monkeypatch.setattr(reconcile, "harvest_receipt_exists", lambda *a, **k: True)

    reconcile._harvest_if_owed(
        tmp_path,
        "run-1",
        terminal_cause="failed",
        record=MagicMock(),
        pre_reconcile_status="running",  # transition
    )
    assert fired.called


def test_no_transition_no_receipt_refires_harvest(monkeypatch, tmp_path) -> None:
    """AUDIT rank-2 repair: NO transition (journal already terminal) but NO harvest
    receipt — a session-death dropped the guaranteed harvest. The journal-evidence
    backstop RE-FIRES it exactly once, so the harvest is never silently lost.
    """
    fired = MagicMock()
    monkeypatch.setattr(reconcile, "harvest_on_terminal", fired)
    # The injected fault: the durable receipt is ABSENT (session died before harvest).
    monkeypatch.setattr(reconcile, "harvest_receipt_exists", lambda *a, **k: False)

    reconcile._harvest_if_owed(
        tmp_path,
        "run-1",
        terminal_cause="failed",
        record=MagicMock(),
        pre_reconcile_status="failed",  # NO transition — the rank-2 window
    )
    assert fired.called  # DOCTRINE: terminal-with-no-receipt re-harvests (U8 backstop)


def test_no_transition_with_receipt_is_idempotent(monkeypatch, tmp_path) -> None:
    """The backstop is idempotent: a terminal run whose receipt ALREADY landed does
    NOT re-pay the pull + reduce + ledger append on an idempotent re-reconcile.
    """
    fired = MagicMock()
    monkeypatch.setattr(reconcile, "harvest_on_terminal", fired)
    monkeypatch.setattr(reconcile, "harvest_receipt_exists", lambda *a, **k: True)

    reconcile._harvest_if_owed(
        tmp_path,
        "run-1",
        terminal_cause="failed",
        record=MagicMock(),
        pre_reconcile_status="failed",  # no transition, receipt present
    )
    assert not fired.called  # DOCTRINE: no double-harvest when the receipt exists
