"""Tests for the ``watching_refill`` terminator + its block-chain row (RFC #362).

Unit B wiring: ``campaign-watch`` gains a fourth terminator ``watching_refill``
(``needs_decision=False``) emitted when ``campaign-advance`` decides ``refill``
— split out of ``watching_healthy`` (which maps to a chain END) so it can chain
to the ``campaign-refill`` actor. These tests drive a REAL ``campaign-advance``
to each decision via a synthetic journal, and pin the ``block_chain`` successor
rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent._wire.workflows.campaign_blocks import CampaignWatchSpec
from hpc_agent.infra import block_chain
from hpc_agent.meta.campaign.blocks import campaign_watch
from hpc_agent.meta.campaign.manifest import write_manifest

if TYPE_CHECKING:
    from pathlib import Path


def _seed_iteration(experiment_dir: Path, *, run_id: str, campaign_id: str, status: str) -> None:
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="0" * 12,
        hpc_agent_version="0.0.0+test",
        submitted_at="2026-01-01T00:00:00Z",
        executor="hpc_user_tasks",
        result_dir_template="results/{run_id}/{task_id}",
        task_count=1,
        tasks_py_sha="0" * 12,
        campaign_id=campaign_id,
        profile="ml",
        cluster="hoffman2",
        remote_path="/u/scratch/exp",
    )
    upsert_run(
        experiment_dir,
        RunRecord(
            run_id=run_id,
            profile="ml",
            cluster="hoffman2",
            ssh_target="user@host",
            remote_path="/scratch/exp",
            job_name="ml",
            job_ids=["1"],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment_dir.resolve()),
            campaign_id=campaign_id,
            status=status,
        ),
    )


# ── watch terminator mapping ──────────────────────────────────────────────────


def test_watch_refill_hands_off_to_campaign_refill(journal_home: Path, tmp_path: Path) -> None:
    """An async campaign with a free pool slot (advance → refill) is a
    ``watching_refill`` terminator: no boundary, hand-off hint to campaign-refill."""
    write_manifest(tmp_path, campaign_id="A", goal="tune", async_refill=True, max_in_flight=3)
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="complete")

    res = campaign_watch(tmp_path, spec=CampaignWatchSpec(campaign_id="A"))

    assert res.block == "watch"
    assert res.stage_reached == "watching_refill"
    assert res.needs_decision is False
    assert res.brief["decision"] == "refill"
    assert res.next_block is not None
    assert res.next_block["verb"] == "campaign-refill"
    assert res.next_block["spec_hint"]["campaign_id"] == "A"


def test_watch_healthy_still_fires_for_continue(journal_home: Path, tmp_path: Path) -> None:
    """A SYNC campaign (no async opt-in) → advance continue → watching_healthy,
    unchanged (refill split did not disturb the healthy terminator)."""
    write_manifest(tmp_path, campaign_id="A", goal="tune")
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="complete")

    res = campaign_watch(tmp_path, spec=CampaignWatchSpec(campaign_id="A"))

    assert res.stage_reached == "watching_healthy"
    assert res.needs_decision is False
    assert res.brief["decision"] == "continue"
    assert res.next_block is None


def test_watch_healthy_for_full_async_pool(journal_home: Path, tmp_path: Path) -> None:
    """An async pool that is FULL (K=1, 1 in flight) → advance wait_in_flight →
    still watching_healthy (not refill): wait_in_flight stays a passive healthy
    tick, only ``refill`` routes to the actor."""
    write_manifest(tmp_path, campaign_id="A", goal="tune", async_refill=True, max_in_flight=1)
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="in_flight")

    res = campaign_watch(tmp_path, spec=CampaignWatchSpec(campaign_id="A"))

    assert res.stage_reached == "watching_healthy"
    assert res.needs_decision is False
    assert res.brief["decision"] == "wait_in_flight"
    assert res.next_block is None


# ── block_chain successor rows ────────────────────────────────────────────────


def test_block_chain_watching_refill_row() -> None:
    """SUCCESSORS: (campaign-watch, watching_refill) → campaign-refill; the
    watch→refill hand-off is the ONE deterministic new successor."""
    assert block_chain.successor_verb("campaign-watch", "watching_refill") == "campaign-refill"


def test_block_chain_campaign_refill_terminals_end_the_chain() -> None:
    """Every campaign-refill stage ends the chain (→ None) so the next tick
    re-enters via campaign-watch (one step per tick)."""
    for stage in ("refilled", "no_refill_needed", "refill_blocked"):
        assert ("campaign-refill", stage) in block_chain.SUCCESSORS
        assert block_chain.successor_verb("campaign-refill", stage) is None


def test_campaign_refill_is_a_known_ungated_block() -> None:
    """campaign-refill is in WORKFLOW_OF (its own single-member family) so the
    SUCCESSORS coverage assertion holds, and is NOT greenlight-gated (its own
    greenlight refusal is the consent check)."""
    assert block_chain.WORKFLOW_OF.get("campaign-refill") == "campaign-refill"
    assert block_chain.is_gated("campaign-refill") is False


def test_watching_refill_next_block_agrees_with_table(journal_home: Path, tmp_path: Path) -> None:
    """The block's emitted next_block verb == the SUCCESSORS table (SoT-drift
    guard: the block module and the chain table cannot disagree)."""
    write_manifest(tmp_path, campaign_id="A", goal="tune", async_refill=True, max_in_flight=3)
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="complete")

    res = campaign_watch(tmp_path, spec=CampaignWatchSpec(campaign_id="A"))
    assert res.next_block is not None
    assert res.next_block["verb"] == block_chain.successor_verb("campaign-watch", "watching_refill")
