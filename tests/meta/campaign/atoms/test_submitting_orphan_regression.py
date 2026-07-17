"""Regression: a ``submitting`` orphan (U3 live flip) must not fool the two
campaign-loop readers the U3-a reader-tolerance wave missed.

Both are reachable under ``HPC_SUBMIT_ONCE=1`` when a submit orphans in its
dispatch→id window (process death after the mint, before the promote), leaving
a durable ``submitting`` child tagged with the campaign_id. Found by the
2026-07-17 submit-once/state provenance review (F1/F2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.meta.campaign.atoms.circuit_breaker import consecutive_terminal_failures
from hpc_agent.meta.campaign.atoms.status import campaign_status
from hpc_agent.state.index import find_runs_by_campaign
from tests.meta.campaign.atoms.test_circuit_breaker import _seed_iteration

if TYPE_CHECKING:
    from pathlib import Path


def test_submitting_orphan_does_not_disarm_the_circuit_breaker(
    journal_home: Path, tmp_path: Path
) -> None:
    # 3 real consecutive failures, then a mid-dispatch orphan at the tail.
    # ``submitting`` is non-terminal (like ``in_flight``): it must be SKIPPED,
    # not read as a terminal non-failure that ends the streak → the breaker
    # must still see all 3 failures. (Pre-fix: submitting hit ``else: break``
    # and silently zeroed the count, disarming the halt on the exact orphan
    # state U3 exists to recover.)
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r2", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r3", campaign_id="A", status="submitting")

    out = consecutive_terminal_failures(find_runs_by_campaign(tmp_path, "A"))
    assert out["count"] == 3
    assert out["last_status"] == "failed"


def test_submitting_orphan_counts_as_outstanding_in_campaign_status(
    journal_home: Path, tmp_path: Path
) -> None:
    # A ``submitting`` child can name a LIVE array (dispatched, id-read severed),
    # so the campaign's wait/idle checks must treat it as outstanding — else the
    # campaign can stop and orphan the array unmonitored. (Pre-fix: the count
    # keyed on ``== "in_flight"`` only, so a lone submitting child read as 0.)
    _seed_iteration(tmp_path, run_id="s0", campaign_id="B", status="submitting")

    out = campaign_status(experiment_dir=tmp_path, campaign_id="B")
    assert out["in_flight"] >= 1
