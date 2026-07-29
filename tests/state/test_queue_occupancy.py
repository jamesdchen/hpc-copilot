"""The shared occupancy predicate's RELEASE rule (run-queue plan R9 / §10.S3).

``state/queue_occupancy`` is the ONE "occupies a pool slot" predicate, and D6
routes ``campaign-advance``'s pool arithmetic through it. Its union has two
halves and the halves have to retire a slot on the SAME evidence, because
intake has no terminal state (D8: ``{queued, placed}`` and nothing wider) and
nothing compacts the ledger — so an item lives forever and, if it were counted
forever, a healthy campaign would read as permanently full after exactly ``K``
completed iterations.

These are the release tests: the ones that fail when the ledger half keeps a
slot the journal half has already let go. The occupy-side behaviour (a queued
item shrinking pool_room — S3's actual bug) is pinned next to the decision it
feeds, in ``tests/meta/campaign/atoms/test_advance_refill.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent.state.queue_intake import (
    append_intake_item,
    append_intake_placement,
    placement_request_id,
)
from hpc_agent.state.queue_occupancy import occupancy_detail, occupied_slots

if TYPE_CHECKING:
    from pathlib import Path

_BASE = "tune"
_CLUSTER = "hoffman2"
_CID = f"{_BASE}_{_CLUSTER}"


def _place(experiment_dir: Path, item_id: str, *, run_id: str | None) -> None:
    """One item enqueued and then PLACED — the shape queue-dispatch leaves."""
    record: dict[str, Any] = {
        "spec": {"profile": "ml"},
        "run_name": "ml",
        "campaign_base": _BASE,
        "cluster_pin": _CLUSTER,
    }
    if run_id is not None:
        record["run_id"] = run_id
        record["cmd_sha"] = "0" * 8
    append_intake_item(experiment_dir, record=record, request_id=item_id)
    append_intake_placement(
        experiment_dir,
        item_id=item_id,
        request_id=placement_request_id(item_id),
        cluster=_CLUSTER,
        campaign_id=_CID,
        reason="the only candidate",
        run_id=run_id,
    )


def _seed_run(
    experiment_dir: Path,
    *,
    run_id: str,
    status: str = "in_flight",
    superseded_by: str = "",
    campaign_id: str = _CID,
) -> None:
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        experiment_dir,
        RunRecord(
            run_id=run_id,
            profile="ml",
            cluster=_CLUSTER,
            ssh_target="user@host",
            remote_path="/scratch/exp",
            job_name="ml",
            job_ids=["1"],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment_dir.resolve()),
            campaign_id=campaign_id,
            status=status,
            superseded_by=superseded_by,
        ),
    )


def test_an_item_whose_run_completed_stops_occupying(journal_home: Path, tmp_path: Path) -> None:
    """THE wedge test. K dispatched-and-completed items must not hold K slots.

    Both halves see the same runs: the journal half drops them (terminal), so
    without the ledger half dropping the ITEMS too, ``occupied`` sticks at K for
    the rest of the experiment's life and ``pool_room = K - occupied`` is 0
    forever — a campaign parked on ``wait_in_flight`` over an EMPTY cluster with
    its whole job budget unspent. Reproduced against the shipped code before the
    fix: four placed items + four ``complete`` records reported ``occupied=4``
    with an empty journal half.
    """
    for i in range(4):
        run_id = f"ml-cccc000{i}"
        _place(tmp_path, f"slot-{i}", run_id=run_id)
        _seed_run(tmp_path, run_id=run_id, status="complete")

    detail = occupancy_detail(tmp_path, _CID)
    assert detail["occupied"] == 0
    assert detail["runs"] == []
    assert detail["items"] == []
    # DISCLOSED, not merely skipped: "why is this campaign at 0 of 4?" has to be
    # answerable from the evidence the count was taken over.
    assert [row["item_id"] for row in detail["retired_items"]] == [f"slot-{i}" for i in range(4)]
    assert {row["retired_by"] for row in detail["retired_items"]} == {"complete"}


def test_an_item_whose_run_was_superseded_stops_occupying(
    journal_home: Path, tmp_path: Path
) -> None:
    """The ledger half applies the SAME two tests the journal half applies.

    ``superseded_by`` closes a record even when its status has not caught up, so
    a superseded run holds a slot no live job is using — and neither does the
    item that became it.
    """
    _place(tmp_path, "slot-a", run_id="ml-dead0001")
    _seed_run(tmp_path, run_id="ml-dead0001", status="in_flight", superseded_by="ml-live0001")

    detail = occupancy_detail(tmp_path, _CID)
    assert detail["occupied"] == 0
    assert detail["retired_items"][0]["retired_by"] == "ml-live0001"


def test_a_placed_item_with_no_run_still_occupies(journal_home: Path, tmp_path: Path) -> None:
    """The release rule must not eat the fact the ledger half exists FOR.

    A placed item with no RunRecord is the enqueue→dispatch (or the crashed
    dispatch) window: committed work no journal knows about. It keeps its slot —
    that is S3's bug closed, and the recovery leg in ``campaign-refill`` is what
    eventually turns it into a run.
    """
    _place(tmp_path, "slot-a", run_id="ml-aaaa0001")
    assert occupied_slots(tmp_path, _CID) == 1


def test_a_live_run_still_occupies_through_its_item(journal_home: Path, tmp_path: Path) -> None:
    """A dispatched, in-flight item is ONE slot — not zero, and not two."""
    _place(tmp_path, "slot-a", run_id="ml-aaaa0001")
    _seed_run(tmp_path, run_id="ml-aaaa0001", status="in_flight")

    detail = occupancy_detail(tmp_path, _CID)
    assert detail["occupied"] == 1
    assert detail["shared_slots"] == ["run:ml-aaaa0001"]
    assert detail["retired_items"] == []


def test_release_does_not_depend_on_the_runs_campaign_attribution(
    journal_home: Path, tmp_path: Path
) -> None:
    """The join is by ``run_id``, never by who the RunRecord says it belongs to.

    A run's ``campaign_id`` comes off its submit spec, not off the ledger item,
    so a record that landed with a blank or different id is still the run this
    item became. Keying the release on ``find_runs_by_campaign`` alone would
    reproduce the wedge for exactly the campaigns whose ids do not compose —
    the ones that already lose the composed placement id.
    """
    _place(tmp_path, "slot-a", run_id="ml-aaaa0001")
    _seed_run(tmp_path, run_id="ml-aaaa0001", status="complete", campaign_id="")
    assert occupied_slots(tmp_path, _CID) == 0


def test_the_join_mints_no_journal_namespace(tmp_path: Path, monkeypatch: Any) -> None:
    """F46: a pure read must not create ``~/.claude/hpc/<repo_hash>/``.

    ``load_run`` resolves through ``JournalLayout.run_record``, which mkdirs the
    journal home and writes ``repo.json`` on access — so the release join has to
    ask whether the namespace exists BEFORE it looks, the same guard
    ``queue-status`` opens with. Without it, ``queue-status`` (which calls this
    predicate) mints a ghost namespace from a verb declaring ``side_effects=[]``.
    """
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(home / "hpc"))
    exp = tmp_path / "exp"
    exp.mkdir()
    _place(exp, "slot-a", run_id="ml-aaaa0001")

    assert occupied_slots(exp, _CID) == 1
    assert list(home.rglob("*")) == []
