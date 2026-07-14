"""Multi-cluster isolation proof (plan Phase 2: N campaign_ids, one repo).

The multi-cluster model is **N campaign_ids in one repo**, never N repos. The
``campaign_id`` slug is the isolation primitive: every cross-run query partitions
on it, so two clusters driven from the same tree never see each other's runs.
These tests pin that partition over synthetic journal evidence, and demonstrate
the reporting-only "one logical campaign" merge as a plain Python aggregation —
**not** a new persisted primitive.

Design: ``docs/design/campaign-multi-cluster.md``.

Seeding mirrors ``tests/meta/campaign/atoms/test_campaign_atoms.py``
(``_seed_run_with_status``): each iteration gets BOTH a v2 sidecar (so
``campaign-status`` counts the iteration via ``find_sidecars_by_campaign``) and a
journal ``RunRecord`` carrying the lifecycle ``status`` that
``find_runs_by_campaign`` / ``campaign-status.in_flight`` partition on.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.meta.campaign.atoms.status import campaign_status
from hpc_agent.state.index import find_runs_by_campaign
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

# Per the ``<base>_<clusterkey>`` naming convention (one base study, one
# campaign_id per cluster). These two cids are the disjoint isolation slugs.
_BASE = "ebm_all_buckets"
_CID_CARC = f"{_BASE}_carc"
_CID_HOFFMAN2 = f"{_BASE}_hoffman2"


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    """A throwaway experiment dir on disk (the ONE repo both cids share)."""
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_campaign_run(
    experiment: Path,
    *,
    run_id: str,
    campaign_id: str,
    cluster: str,
    status: str = "in_flight",
) -> None:
    """Seed BOTH a v2 sidecar and a journal ``RunRecord`` for one iteration.

    The sidecar makes ``campaign-status`` count the iteration (it walks
    ``find_sidecars_by_campaign``); the journal record carries the ``cluster``
    target identity and the lifecycle ``status`` (default ``in_flight``) that
    ``find_runs_by_campaign`` and ``campaign-status.in_flight`` partition on.
    """
    write_run_sidecar(
        experiment,
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
        cluster=cluster,
        remote_path=f"/scratch/{cluster}/exp",
    )
    upsert_run(
        experiment,
        RunRecord(
            run_id=run_id,
            profile="ml",
            cluster=cluster,
            ssh_target=f"user@{cluster}.example.edu",
            remote_path=f"/scratch/{cluster}/exp",
            job_name="ml",
            job_ids=["12345"],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment.resolve()),
            campaign_id=campaign_id,
            status=status,
        ),
    )


def _seed_two_cluster_campaigns(experiment: Path) -> None:
    """Two campaigns sharing one repo, each its own cluster's disjoint slug.

    carc: 3 iterations, 2 still in flight + 1 complete (so in_flight is
    journal-status-derived, INDEPENDENT of the iteration count).
    hoffman2: 1 iteration, 1 in flight.
    """
    _seed_campaign_run(experiment, run_id="carc_0", campaign_id=_CID_CARC, cluster="carc")
    _seed_campaign_run(experiment, run_id="carc_1", campaign_id=_CID_CARC, cluster="carc")
    _seed_campaign_run(
        experiment, run_id="carc_2", campaign_id=_CID_CARC, cluster="carc", status="complete"
    )
    _seed_campaign_run(experiment, run_id="hoff_0", campaign_id=_CID_HOFFMAN2, cluster="hoffman2")


def test_find_runs_by_campaign_partitions_cleanly(journal_home: Path, experiment: Path) -> None:
    """``find_runs_by_campaign`` returns ONLY that cid's runs — a clean partition.

    Two cluster campaigns live in one repo; each cid's query sees its own runs
    and never the other's. This is the isolation primitive the whole
    multi-cluster model rests on.
    """
    _seed_two_cluster_campaigns(experiment)

    carc_runs = find_runs_by_campaign(experiment, _CID_CARC)
    hoff_runs = find_runs_by_campaign(experiment, _CID_HOFFMAN2)

    carc_ids = {r.run_id for r in carc_runs}
    hoff_ids = {r.run_id for r in hoff_runs}

    assert carc_ids == {"carc_0", "carc_1", "carc_2"}
    assert hoff_ids == {"hoff_0"}
    # Disjoint by construction — neither query leaks the other's runs.
    assert carc_ids.isdisjoint(hoff_ids)
    # Each partition carries exactly one cluster target (single-cluster-per-cid).
    assert {r.cluster for r in carc_runs} == {"carc"}
    assert {r.cluster for r in hoff_runs} == {"hoffman2"}


def test_campaign_status_in_flight_counts_are_independent(
    journal_home: Path, experiment: Path
) -> None:
    """Each cid's ``in_flight`` count is its own, derived from journal status —
    independent of the other cid AND of its own iteration count."""
    _seed_two_cluster_campaigns(experiment)

    carc = campaign_status(experiment_dir=experiment, campaign_id=_CID_CARC)
    hoff = campaign_status(experiment_dir=experiment, campaign_id=_CID_HOFFMAN2)

    # carc: 3 iterations (sidecars), but only 2 in flight (1 is complete) —
    # in_flight is journal-status-derived, not the iteration count.
    assert carc["iterations"] == 3
    assert carc["in_flight"] == 2
    # hoffman2's counts are wholly its own.
    assert hoff["iterations"] == 1
    assert hoff["in_flight"] == 1
    # The run-id sets reported per cid are disjoint.
    assert set(carc["run_ids"]).isdisjoint(set(hoff["run_ids"]))


def _merge_campaign_statuses(statuses: list[dict[str, Any]]) -> dict[str, Any]:
    """The reporting-only "one logical campaign" view: a thin merge over per-cid
    ``campaign-status`` results. Sum iterations / in_flight, union run_ids.

    This is a PLAIN Python aggregation — no new persisted state, no new
    primitive. The merged dict is derived on demand from the per-cid sources of
    truth and thrown away; nothing is written back to disk.
    """
    return {
        "iterations": sum(int(s["iterations"]) for s in statuses),
        "in_flight": sum(int(s["in_flight"]) for s in statuses),
        "run_ids": sorted({rid for s in statuses for rid in s["run_ids"]}),
    }


def test_one_logical_campaign_merge_view(journal_home: Path, experiment: Path) -> None:
    """The "one logical campaign" view is a reporting-only merge over the two
    per-cid ``campaign-status`` results — summed counts + unioned run_ids — with
    NO new persisted state."""
    _seed_two_cluster_campaigns(experiment)

    statuses = [
        campaign_status(experiment_dir=experiment, campaign_id=_CID_CARC),
        campaign_status(experiment_dir=experiment, campaign_id=_CID_HOFFMAN2),
    ]
    merged = _merge_campaign_statuses(statuses)

    # 3 carc iterations + 1 hoffman2 iteration.
    assert merged["iterations"] == 4
    # 2 carc in flight + 1 hoffman2 in flight.
    assert merged["in_flight"] == 3
    # Union of both cids' run-ids, all distinct (clean partition → no overlap).
    assert merged["run_ids"] == ["carc_0", "carc_1", "carc_2", "hoff_0"]
    assert len(merged["run_ids"]) == sum(len(s["run_ids"]) for s in statuses)

    # The merge is purely derived: nothing under .hpc/campaigns/ was created for
    # a "merged" campaign — only the two real per-cid dirs exist.
    campaign_root = experiment / ".hpc" / "campaigns"
    if campaign_root.exists():
        persisted = {p.name for p in campaign_root.iterdir() if p.is_dir()}
        assert _BASE not in persisted  # no synthesized "one logical campaign" dir


@pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="msvcrt byte-range locking is the win32-only branch; POSIX uses fcntl",
)
def test_concurrent_deploy_serialized_by_win32_advisory_lock(tmp_path: Path) -> None:
    """Concurrent cross-cluster deploys are serialized by the Windows lock.

    Safe round-robin / N-driver concurrent deploys rely on Phase 0 making
    ``advisory_flock`` a REAL ``msvcrt`` byte-range lock on win32 (before, the
    per-repo ``.submit_lock`` serializing concurrent deploys was a no-op on
    Windows, leaving the ``prune_orphan_sidecars(min_age_seconds=0)`` race
    unguarded). Cross-PROCESS serialization is already proven by Phase 0's own
    test —
    ``tests/infra/test_atomic_locked_update.py::test_advisory_flock_serializes_cross_process_win32``
    — so we do NOT duplicate it here. This pins the exclusion contract that story
    rests on: while one acquirer holds the lock, a second non-blocking acquire is
    refused; once released, it succeeds again.
    """
    from hpc_agent.infra.io import advisory_flock

    lock = tmp_path / ".submit_lock"
    with advisory_flock(lock, blocking=True) as outer:
        assert outer is True
        # A second, separate handle cannot acquire the held byte-range lock.
        with advisory_flock(lock, blocking=False) as inner:
            assert inner is False
    # Released → acquirable again.
    with advisory_flock(lock, blocking=False) as again:
        assert again is True
