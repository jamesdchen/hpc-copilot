"""The janitor's invariants — what grooming may NEVER do, and what it must stop paying.

Companion to ``test_queue_drain_contract.py``: that file pins what Phase 3
publishes, this one pins the five properties an adversarial review found the
janitor and the projection breaking. Each section names the class of bug rather
than the symptom, because every one of them was a silent data or cost defect
that the shipped suite was green over.

1. **Compacting an item is not the same question as retiring its slot** (F1).
   ``run_occupies`` must release a ``failed`` run's slot immediately — a campaign
   wedges at ``K`` otherwise — but the ledger ROW over a corpse is live intent,
   because ``queue-dispatch``'s own decision table mints a fresh attempt over a
   resubmittable terminal instead of adopting it. Conflating the two deleted a
   ``queued`` retry in the same tick that reported it held, and deleted a
   ``placed`` row whose durable-first placement had been appended before a start
   that then refused.
2. **A status pass pays for the page it returns, not for the ledger's history**
   (F2). ``occupied_slots`` walks EVERY run file in the namespace per campaign;
   keying that on pre-hiding matches made a pass returning ZERO items pay
   O(history × campaigns), forever, for any ledger holding one placed item.
3. **R8 survives a compaction** (F7). Dropping an item's records drops its dedup
   entry, so past the journal prune a replayed relay turn re-enqueues and REALLY
   resubmits a completed run.
4. **The census is verified under the lock** (F10). It is computed outside the
   ledger flock, and the dispatch lock does not cover the gap — it is per-cid
   while the ledger is global. Both orderings of the resulting race are pinned.
5. **The retryable(n) budget bounds the spins it actually sees** (F4). Pinned
   behaviourally because the field's documentation now states it precisely: a
   park with no committed greenlight is counted once and then frozen out by the
   drivability formula's ¬parked clause, so the budget is about the
   greenlight-unadvanced spin and the skip spin, not about a wedged run.

Cluster-free: the journal home is redirected via ``HPC_JOURNAL_DIR`` and the one
submit seat is mocked; nothing here opens a socket.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent._kernel.contract.vocabulary import TERMINAL_STATUSES, JournalStatus
from hpc_agent._wire.actions.queue_run import QueueRunSpec
from hpc_agent._wire.queries.queue_status import QueueStatusSpec
from hpc_agent._wire.workflows.block_drive import BlockDriveResult, BlockDriveSpec
from hpc_agent._wire.workflows.campaign_run import CampaignRunResult
from hpc_agent._wire.workflows.queue_dispatch import QueueDispatchSpec
from hpc_agent.ops.block_drive_op import block_drive
from hpc_agent.ops.queue.dispatch import _ADOPTABLE_STATUSES, queue_dispatch
from hpc_agent.ops.queue.maintenance import groom_queue_stores
from hpc_agent.ops.queue.run import queue_run
from hpc_agent.ops.queue.status import queue_status
from hpc_agent.state import journal as journal_mod
from hpc_agent.state import queue_occupancy as occupancy_mod
from hpc_agent.state.index import prune_terminal_runs
from hpc_agent.state.journal import mark_pending_decision, upsert_run
from hpc_agent.state.queue_intake import (
    TOMBSTONE_CAP,
    append_intake_item,
    append_intake_placement,
    compact_intake_ledger,
    compaction_tombstones,
    intake_path_if_exists,
    is_compaction_tombstone,
    read_intake_items,
)
from hpc_agent.state.queue_occupancy import (
    item_is_history,
    occupied_slots,
    retired_item_census,
    retired_item_ids,
    run_occupies,
)
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-07-29T12:00:00+00:00"

#: The submit seat ``queue-dispatch`` composes, patched at its SOURCE module.
_RUN = "hpc_agent.ops.campaign_run.campaign_run"

_CLUSTERS = """
alpha:
  scheduler: slurm
  host: alpha.edu
  user: me
  max_walltime_sec: 86400
"""


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


@pytest.fixture(autouse=True)
def _clusters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "clusters.yaml"
    path.write_text(_CLUSTERS, encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(path))


def _exp(tmp_path: Path) -> Path:
    exp = tmp_path / "exp"
    exp.mkdir(exist_ok=True)
    return exp


def _submit_spec(run_id: str) -> dict[str, Any]:
    return {
        "profile": "ml",
        "cluster": "alpha",
        "ssh_target": "me@alpha.edu",
        "remote_path": "/scratch/ml",
        "job_name": "ml_array",
        "run_id": run_id,
        "total_tasks": 1,
        "backend": "slurm",
        "script": ".hpc/templates/cpu_array.sh",
        "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
    }


def _enqueue(exp: Path, item_id: str, run_id: str, **record: Any) -> None:
    record.setdefault("run_name", "ml")
    record.setdefault("cmd_sha", run_id.rsplit("-", 1)[-1])
    record.setdefault("cluster_pin", "alpha")
    record.setdefault("campaign_base", "study")
    record["run_id"] = run_id
    record.setdefault("spec", _submit_spec(run_id))
    append_intake_item(exp, record=record, request_id=item_id)


def _place(exp: Path, item_id: str, run_id: str, *, token: str = ".placed") -> None:
    append_intake_placement(
        exp,
        item_id=item_id,
        request_id=f"{item_id}{token}",
        cluster="alpha",
        campaign_id="study_alpha",
        reason="only candidate",
        run_id=run_id,
    )


def _seed_run(exp: Path, run_id: str, *, status: str, **kw: Any) -> RunRecord:
    record = RunRecord(
        run_id=run_id,
        profile="ml",
        cluster="alpha",
        ssh_target="me@alpha.edu",
        remote_path="/scratch/ml",
        job_name="ml_array",
        job_ids=["1"],
        total_tasks=1,
        submitted_at="2026-07-29T00:00:00+00:00",
        experiment_dir=str(exp),
        status=status,
        campaign_id="study_alpha",
        **kw,
    )
    upsert_run(exp, record)
    return record


def _detached(run_id: str) -> CampaignRunResult:
    return CampaignRunResult(
        stage_reached="detached",
        needs_decision=False,
        reason="detached",
        run_id=run_id,
        started=True,
        watch="journal",
        detached_pid=4242,
    )


def _status(exp: Path, **kw: Any) -> Any:
    kw.setdefault("now", _NOW)
    kw.setdefault("include_settled", True)
    return queue_status(experiment_dir=exp, spec=QueueStatusSpec(**kw))


def _by_id(result: Any) -> dict[str, Any]:
    return {item.item_id: item for item in result.items}


def _ids(exp: Path) -> list[str]:
    return [item["item_id"] for item in read_intake_items(exp)]


def _lines(exp: Path) -> list[dict[str, Any]]:
    path = intake_path_if_exists(exp)
    if not path.is_file():
        return []
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]


# ── 1. compacting an ITEM is not retiring its SLOT (F1) ──────────────────────


def test_a_queued_retry_over_a_failed_run_survives_the_tick_that_held_it(
    tmp_path: Path,
) -> None:
    """The blocker, end to end.

    A run fails; a human or a refill producer re-enqueues the same spec, so the
    retry computes the SAME run_id and sits ``queued``. The next dispatch tick
    holds it back (it is dispatching something else) — "never dropped, R4" — and
    then grooms with ``exclude_item_ids`` covering only the items it dispatched
    or refused. Before the fix that groom compacted the held retry off the
    ledger in the same result that promised it stayed queued, which for a refill
    producer is also an unbounded enqueue/delete cycle.
    """
    exp = _exp(tmp_path)
    _seed_run(exp, "ml-aaaa1111", status="failed")  # the corpse
    _enqueue(exp, "i-retry", "ml-aaaa1111")  # the retry: same computed run_id
    _enqueue(exp, "i-other", "ml-bbbb2222")  # what this tick actually dispatches

    with mock.patch(_RUN, return_value=_detached("ml-bbbb2222")):
        res = queue_dispatch(
            experiment_dir=exp,
            spec=QueueDispatchSpec(now=_NOW, item_ids=["i-other"]),
        )

    assert [row.item_id for row in res.dispatched] == ["i-other"]
    assert "i-retry" not in {row.item_id for row in res.dispatched}
    # The tick groomed (it wrote a placement) and left the held retry alone.
    assert res.maintenance["dropped_items"] == 0
    assert "i-retry" in _ids(exp)
    item = next(i for i in read_intake_items(exp) if i["item_id"] == "i-retry")
    assert item["state"] == "queued"


def test_the_surviving_retry_still_joins_its_corpse_and_protects_it_from_the_prune(
    tmp_path: Path,
) -> None:
    """Surviving compaction is only half of it: the row must still project truthfully.

    A ``queued`` retry over a corpse joins to that corpse, so the prune must not
    take the record out from under it — the projection would flip to
    ``dispatched=false`` and start lying about a run that really happened. The
    janitor protects the runs SURVIVING items reference, and the retry is now one
    of them.
    """
    exp = _exp(tmp_path)
    _seed_run(exp, "ml-aaaa1111", status="failed")
    _enqueue(exp, "i-retry", "ml-aaaa1111")

    report = groom_queue_stores(exp, keep_terminal_runs=0)

    assert (report["dropped_items"], report["pruned_runs"]) == (0, 0)
    assert report["protected_runs"] == 1
    item = _by_id(_status(exp))["i-retry"]
    assert (item.state, item.dispatched, item.run_status) == ("queued", True, "failed")


def test_a_re_dispatch_of_the_surviving_retry_mints_rather_than_adopts(
    tmp_path: Path,
) -> None:
    """The decision table the retirement rule now routes through.

    ``_ADOPTABLE_STATUSES`` excludes the resubmittable terminals on purpose — "a
    corpse is not a dispatch". So dispatching the retry starts a NEW lifecycle;
    the item was never history, which is exactly why compacting it was data loss.
    """
    exp = _exp(tmp_path)
    _seed_run(exp, "ml-aaaa1111", status="failed")
    _enqueue(exp, "i-retry", "ml-aaaa1111")

    with mock.patch(_RUN, return_value=_detached("ml-aaaa1111")) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=QueueDispatchSpec(now=_NOW))

    assert [(row.item_id, row.outcome) for row in res.dispatched] == [("i-retry", "started")]
    m_run.assert_called_once()


def test_a_placed_row_over_a_corpse_survives_because_its_start_may_never_have_happened(
    tmp_path: Path,
) -> None:
    """The second deleted row, and the window that makes it reachable.

    ``_dispatch_one`` appends the placement BEFORE starting (durable-first), so a
    ``claim_held`` / ``gate_refused`` / crash after that append leaves an item
    reading ``placed`` whose only RunRecord is the PREVIOUS attempt's corpse.
    ``exclude_item_ids`` covers it for the tick that refused it and no longer;
    the NEXT tick's groom used to erase it, and with it the only durable evidence
    ``queue-dispatch --item-ids`` needs to recover the dispatch.
    """
    exp = _exp(tmp_path)
    _seed_run(exp, "ml-cccc3333", status="failed")
    _enqueue(exp, "i-recover", "ml-cccc3333")
    _place(exp, "i-recover", "ml-cccc3333")  # placement landed; the start did not

    assert groom_queue_stores(exp)["dropped_items"] == 0
    assert _ids(exp) == ["i-recover"]
    # And the recovery path can still find it.
    with mock.patch(_RUN, return_value=_detached("ml-cccc3333")):
        res = queue_dispatch(
            experiment_dir=exp, spec=QueueDispatchSpec(now=_NOW, item_ids=["i-recover"])
        )
    assert [row.reason_code for row in res.refused] == []
    assert [row.item_id for row in res.dispatched] == ["i-recover"]


def test_a_placed_row_over_a_HELD_corpse_survives_because_a_human_owes_a_verdict(
    tmp_path: Path,
) -> None:
    """The gap in "not a resubmittable terminal", asked separately on purpose.

    ``is_resubmittable_terminal`` answers False for a HELD failed run — meaning
    "a plain submit must not proceed", the opposite of what a hold means to a
    janitor. Keying compaction on that predicate alone would therefore delete
    precisely the rows a human still owes a decision on, so the hold is asked as
    its own question.
    """
    exp = _exp(tmp_path)
    _seed_run(exp, "ml-dddd4444", status="failed", pending_verdict={"kind": "anomaly"})
    _enqueue(exp, "i-held", "ml-dddd4444")
    _place(exp, "i-held", "ml-dddd4444")

    assert retired_item_ids(exp) == set()
    assert groom_queue_stores(exp)["dropped_items"] == 0
    assert _ids(exp) == ["i-held"]


def test_a_placed_row_over_a_complete_run_is_still_compacted(tmp_path: Path) -> None:
    """The narrowing must not become a refusal to compact anything.

    A ``complete`` run is adoptable, so its ledger row genuinely answers nothing
    any reader still asks — this is the case §7's relaunch-cheapness invariant is
    written about, and it must still fire.
    """
    exp = _exp(tmp_path)
    _seed_run(exp, "ml-eeee5555", status="complete")
    _enqueue(exp, "i-done", "ml-eeee5555")
    _place(exp, "i-done", "ml-eeee5555")

    assert retired_item_ids(exp) == {"i-done"}
    assert groom_queue_stores(exp)["dropped_items"] == 1
    assert _ids(exp) == []


def test_a_superseded_run_still_retires_its_item(tmp_path: Path) -> None:
    """The other adoptable-and-settled case: a record the supersession organ closed.

    Its status has not caught up (``in_flight``), so only ``superseded_by``
    retires it — and it is not a resubmittable terminal, so the narrowing leaves
    it compactable exactly as before.
    """
    exp = _exp(tmp_path)
    _seed_run(exp, "ml-ffff6666", status="in_flight", superseded_by="ml-99990000")
    _enqueue(exp, "i-super", "ml-ffff6666")
    _place(exp, "i-super", "ml-ffff6666")

    assert retired_item_ids(exp) == {"i-super"}


@pytest.mark.parametrize("status", sorted(str(s) for s in JournalStatus))
def test_compaction_eligibility_is_the_dispatch_decision_tables_complement(
    tmp_path: Path, status: str
) -> None:
    """ONE definition of "a corpse is not a dispatch", asserted across the vocabulary.

    For every journal status: a placed row is history exactly when the run has
    stopped occupying AND ``queue-dispatch`` would ADOPT it rather than mint a
    fresh attempt. Derived from the shipped sets rather than from a literal, so a
    new status joins both sides by construction instead of by somebody
    remembering this file.
    """
    exp = _exp(tmp_path)
    record = _seed_run(exp, f"ml-{status}-0000", status=status)

    expected = not run_occupies(record) and status in _ADOPTABLE_STATUSES
    assert item_is_history(record) is expected
    # And the two sets say the same thing about the terminal half.
    if status in {str(s) for s in TERMINAL_STATUSES}:
        assert item_is_history(record) is (status in _ADOPTABLE_STATUSES)


def test_a_queued_item_is_never_history_whatever_its_run_did(tmp_path: Path) -> None:
    """The state conjunct, isolated: an item that was never placed cannot be the
    history of a dispatch, even when its computed run_id joins to a COMPLETE run
    (the collision §10.S2 names — identical params and run_name compute one id)."""
    exp = _exp(tmp_path)
    _seed_run(exp, "ml-77770000", status="complete")
    _enqueue(exp, "i-queued", "ml-77770000")

    assert retired_item_census(exp) == {}
    assert occupied_slots(exp, "study_alpha") == 0  # the SLOT is still released


# ── 2. a status pass pays for its page, not for history (F2) ─────────────────


def _counting_load_run(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count every ``load_run`` the status pass makes, at all three bindings.

    ``queue_occupancy`` and ``ops/queue/status`` bind it at import time;
    ``state/index.find_runs_by_campaign`` imports it lazily per call, so patching
    the source module catches the journal-walk leg this test is about.
    """
    seen: list[str] = []
    real = journal_mod.load_run

    def counting(experiment_dir: Path, run_id: str) -> Any:
        seen.append(run_id)
        return real(experiment_dir, run_id)

    import hpc_agent.ops.queue.status as status_mod

    monkeypatch.setattr(journal_mod, "load_run", counting)
    monkeypatch.setattr(occupancy_mod, "load_run", counting)
    monkeypatch.setattr(status_mod, "load_run", counting)
    return seen


def test_a_status_pass_returning_nothing_does_not_walk_terminal_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§7's invariant, as a load_run census rather than a stopwatch.

    ``occupied_slots`` -> ``find_runs_by_campaign`` ``load_run``s EVERY run file
    in the namespace, once per campaign on the page. Keying that on MATCHED items
    (pre-hiding) charged a pass returning ZERO items for the experiment's whole
    history — and the residue is permanent, because the final dispatched batch is
    never compacted (grooming excludes the items its own tick reports on).
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "i-settled", "ml-88880000")
    _place(exp, "i-settled", "ml-88880000")
    _seed_run(exp, "ml-88880000", status="complete")
    for n in range(40):
        _seed_run(exp, f"ml-hist{n:03d}", status="complete")

    seen = _counting_load_run(monkeypatch)
    result = queue_status(experiment_dir=exp, spec=QueueStatusSpec(now=_NOW))

    assert result.items == []  # the settled item is hidden by default
    assert result.total_items == 0
    assert [r for r in seen if r.startswith("ml-hist")] == []
    # Only the one run a ledger item names is read at all (twice: the projection
    # and its pending-decision probe). Nothing scaled with history, and the count
    # is bounded by ACTIVE items rather than by the 41 records on disk.
    assert set(seen) == {"ml-88880000"}
    assert len(seen) < 5
    assert result.occupancy == {}


def test_occupancy_still_answers_for_the_campaigns_the_page_returns(
    tmp_path: Path,
) -> None:
    """The narrowing is to the RETURNED items, not to nothing: a live item's
    campaign still carries the R9 number, from the one shared predicate."""
    exp = _exp(tmp_path)
    _enqueue(exp, "i-live", "ml-99990001")
    _place(exp, "i-live", "ml-99990001")
    _seed_run(exp, "ml-99990001", status="in_flight")

    result = queue_status(experiment_dir=exp, spec=QueueStatusSpec(now=_NOW))

    assert [item.item_id for item in result.items] == ["i-live"]
    assert result.occupancy == {"study_alpha": occupied_slots(exp, "study_alpha")}
    assert result.occupancy["study_alpha"] == 1


def test_hiding_a_settled_item_changes_occupancy_but_not_items(tmp_path: Path) -> None:
    """The behavioural difference the fix introduces, stated in one place.

    ``items`` is byte-identical either way; only the occupancy block narrows,
    because it is evidence ABOUT the page. ``include_settled=True`` brings the
    settled item back and its campaign with it.
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "i-settled", "ml-aaaa0009")
    _place(exp, "i-settled", "ml-aaaa0009")
    _seed_run(exp, "ml-aaaa0009", status="complete")

    hidden = queue_status(experiment_dir=exp, spec=QueueStatusSpec(now=_NOW))
    shown = _status(exp)

    assert hidden.items == []
    assert hidden.occupancy == {}
    assert [item.item_id for item in shown.items] == ["i-settled"]
    assert set(shown.occupancy) == {"study_alpha"}
    # Counting happens before hiding, so the numbers a brief quotes are unchanged.
    assert hidden.counts == shown.counts


def test_a_clipped_page_does_not_pay_for_the_campaigns_it_clipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``limit`` bounds the occupancy walk too — the clip is a bound on WORK, not
    only on output, or a limit=1 read over a wide ledger would still walk every
    campaign's journal."""
    exp = _exp(tmp_path)
    for n in range(3):
        _enqueue(exp, f"i-{n}", f"ml-cccc000{n}", campaign_base=f"study{n}")
        append_intake_placement(
            exp,
            item_id=f"i-{n}",
            request_id=f"i-{n}.placed",
            cluster="alpha",
            campaign_id=f"study{n}_alpha",
            reason="only candidate",
            run_id=f"ml-cccc000{n}",
        )
        _seed_run(exp, f"ml-cccc000{n}", status="in_flight")

    result = queue_status(experiment_dir=exp, spec=QueueStatusSpec(now=_NOW, limit=1))

    assert result.truncated is True
    assert result.total_items == 3
    assert len(result.items) == 1
    assert set(result.occupancy) == {result.items[0].campaign_id}


# ── 3. R8 survives a compaction (F7) ─────────────────────────────────────────


def test_a_replayed_request_id_for_a_compacted_item_writes_no_second_line(
    tmp_path: Path,
) -> None:
    """The reviewer's sequence: groom, prune past the record, replay.

    Compaction drops the item's records and with them its dedup entry; the prune
    then drops the RunRecord that was the shipped safety argument's last
    protection. Without a tombstone the replay dedup-MISSES, re-enqueues, and —
    since nothing survives to adopt — really resubmits a completed run.
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "req-42", "ml-dddd0001")
    _place(exp, "req-42", "ml-dddd0001")
    _seed_run(exp, "ml-dddd0001", status="complete")

    assert groom_queue_stores(exp)["dropped_items"] == 1
    assert prune_terminal_runs(exp, 0) == 1  # nothing references it any more
    assert compaction_tombstones(exp) == {"req-42", "req-42.placed"}

    lines_before = len(_lines(exp))
    replayed = append_intake_item(exp, record={"run_id": "ml-dddd0001"}, request_id="req-42")

    assert len(_lines(exp)) == lines_before  # NO new ledger line
    assert is_compaction_tombstone(replayed)
    assert "tombstoned" in (replayed or {})["reason"]
    assert _ids(exp) == []


def test_queue_run_discloses_the_compacted_replay_instead_of_failing_the_read_back(
    tmp_path: Path,
) -> None:
    """The verb's own contract on that path.

    ``queue-run`` echoes what the ledger holds by reading it back, and a compacted
    item is not there to read — so the tombstone answer has to be reported as the
    replay it is, not as a corrupt journal and not as a fresh enqueue.
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "req-77", "ml-dddd0002")
    _place(exp, "req-77", "ml-dddd0002")
    _seed_run(exp, "ml-dddd0002", status="complete")
    groom_queue_stores(exp)

    result = queue_run(
        experiment_dir=exp,
        spec=QueueRunSpec(request_id="req-77", spec=_submit_spec("ml-dddd0002")),
    )

    assert (result.replayed, result.compacted) == (True, True)
    assert result.item_id == "req-77"
    assert result.queued_count == 0
    assert _ids(exp) == []


def test_a_live_items_dedup_is_untouched_by_the_tombstones(tmp_path: Path) -> None:
    """The negative twin: tombstones must not answer for anything still on the
    ledger, or a live item's replay would stop returning its ORIGINAL record."""
    exp = _exp(tmp_path)
    _enqueue(exp, "i-settled", "ml-dddd0003")
    _place(exp, "i-settled", "ml-dddd0003")
    _seed_run(exp, "ml-dddd0003", status="complete")
    _enqueue(exp, "i-live", "ml-dddd0004")
    _place(exp, "i-live", "ml-dddd0004")
    _seed_run(exp, "ml-dddd0004", status="in_flight")

    groom_queue_stores(exp)

    replayed = append_intake_item(exp, record={"run_id": "ml-dddd0004"}, request_id="i-live")
    assert not is_compaction_tombstone(replayed)
    assert (replayed or {})["item_id"] == "i-live"


def test_the_tombstone_set_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A watermark key that grew without bound would be the history-shaped cost
    compaction exists to remove, one file over. The cap keeps the NEWEST entries,
    because those are the ones whose replay window may still be open."""
    exp = _exp(tmp_path)
    monkeypatch.setattr("hpc_agent.state.queue_intake.TOMBSTONE_CAP", 3)
    for n in range(5):
        _enqueue(exp, f"r-{n}", f"ml-eeee000{n}")
        _seed_run(exp, f"ml-eeee000{n}", status="complete")
        _place(exp, f"r-{n}", f"ml-eeee000{n}")
        compact_intake_ledger(exp, drop_item_ids={f"r-{n}"})

    tombstones = compaction_tombstones(exp)
    assert len(tombstones) == 3
    assert "r-4.placed" in tombstones and "r-4" in tombstones
    assert "r-0" not in tombstones
    assert TOMBSTONE_CAP > 3  # the shipped value is the argued one, not the test's


# ── 4. the census is verified under the ledger lock (F10) ────────────────────


def test_a_placement_appended_between_census_and_lock_is_not_compacted(
    tmp_path: Path,
) -> None:
    """Ordering one of the race: the concurrent append lands BEFORE the lock.

    ``groom_queue_stores`` computes the census outside the ledger flock, and the
    dispatch lock does not cover the gap — it is per-cid while the ledger is
    global. A ``queue-dispatch`` tick for another campaign therefore appends a
    placement for an item this census already condemned, and the rewrite deleted
    a row that other process was at that moment reporting as ``placed``. The
    under-lock re-count declines instead, and says so.
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "i-race", "ml-ffff0001")
    _place(exp, "i-race", "ml-ffff0001")
    _seed_run(exp, "ml-ffff0001", status="complete")

    census = retired_item_census(exp)  # what the janitor computes, outside the lock
    assert census == {"i-race": 2}
    _place(exp, "i-race", "ml-ffff0001", token=".replaced")  # the concurrent tick

    report = compact_intake_ledger(exp, drop_item_ids=set(census), witnessed_records=census)

    assert report["raced_items"] == ["i-race"]
    assert (report["dropped_items"], report["dropped_records"]) == (0, 0)
    assert _ids(exp) == ["i-race"]
    assert _status(exp).skipped_records == 0


def test_an_unwitnessed_drop_set_keeps_the_old_unverified_behaviour(
    tmp_path: Path,
) -> None:
    """The guard is shown FIRING: the same interleaving without the census counts
    still destroys the row, which is what the shipped janitor did on every pass."""
    exp = _exp(tmp_path)
    _enqueue(exp, "i-race", "ml-ffff0002")
    _place(exp, "i-race", "ml-ffff0002")
    _seed_run(exp, "ml-ffff0002", status="complete")

    census = retired_item_census(exp)
    _place(exp, "i-race", "ml-ffff0002", token=".replaced")

    report = compact_intake_ledger(exp, drop_item_ids=set(census))  # no witness

    assert report["raced_items"] == []
    assert report["dropped_items"] == 1
    assert _ids(exp) == []


def test_the_janitor_hands_its_own_census_to_the_verification(tmp_path: Path) -> None:
    """The wiring, not just the mechanism: ``groom_queue_stores`` must pass the
    counts it witnessed, or the under-lock check is dead code."""
    exp = _exp(tmp_path)
    _enqueue(exp, "i-race", "ml-ffff0003")
    _place(exp, "i-race", "ml-ffff0003")
    _seed_run(exp, "ml-ffff0003", status="complete")

    real = occupancy_mod.retired_item_census

    def racing_census(experiment_dir: Path) -> dict[str, int]:
        census = real(experiment_dir)
        # A concurrent dispatch tick, in the gap the census cannot hold a lock over.
        _place(experiment_dir, "i-race", "ml-ffff0003", token=".replaced")
        return census

    with mock.patch(
        "hpc_agent.ops.queue.maintenance.retired_item_census", side_effect=racing_census
    ):
        report = groom_queue_stores(exp)

    assert report["raced_items"] == ["i-race"]
    assert report["dropped_items"] == 0
    assert _ids(exp) == ["i-race"]


def test_an_orphan_left_by_the_other_ordering_is_reaped_and_stops_costing_a_note(
    tmp_path: Path,
) -> None:
    """Ordering two: the concurrent append lands AFTER the rewrite.

    Fold rule 2 (only an enqueue opens an item) makes that line permanently
    unfoldable, and because every drop set is derived from FOLDED items it is
    permanently uncompactable too — an immortal ledger line and a permanent
    "inspect the ledger" note on every ``queue-status``. Re-reading cannot see
    this ordering, so the orphan is reaped instead — but only because a TOMBSTONE
    proves this ledger authored the removal it is the tail of.
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "i-orphan", "ml-ffff0004")
    _place(exp, "i-orphan", "ml-ffff0004")
    _seed_run(exp, "ml-ffff0004", status="complete")

    assert groom_queue_stores(exp)["dropped_items"] == 1
    _place(exp, "i-orphan", "ml-ffff0004", token=".late")  # the concurrent tick, too late

    stranded = _status(exp)
    assert stranded.skipped_records == 1
    assert read_intake_items(exp) == []  # unfoldable: no enqueue record opens it

    report = groom_queue_stores(exp)

    assert report["reaped_orphans"] == 1
    assert _lines(exp) == []
    assert _status(exp).skipped_records == 0


def test_an_orphan_with_no_tombstone_behind_it_is_kept(tmp_path: Path) -> None:
    """The reaping is bounded by evidence, not by shape. An unexplained orphan —
    a hand-edited line, a foreign writer — is the only record that something went
    wrong, and compaction removes ANSWERED questions, never unanswerable ones."""
    exp = _exp(tmp_path)
    _enqueue(exp, "i-real", "ml-ffff0005")
    _place(exp, "i-real", "ml-ffff0005")
    _seed_run(exp, "ml-ffff0005", status="complete")
    _place(exp, "i-stranger", "ml-00001111")  # never enqueued: unfoldable, unexplained

    groom_queue_stores(exp)

    remaining = [line["item_id"] for line in _lines(exp)]
    assert remaining == ["i-stranger"]
    assert _status(exp).skipped_records == 1


def test_grooming_still_never_raises_a_failure_into_the_dispatch(tmp_path: Path) -> None:
    """Unchanged by the rewiring: hygiene must never turn a successful dispatch
    into an error envelope. Re-pinned here because the census is now the seat the
    first leg fails at."""
    exp = _exp(tmp_path)
    with mock.patch(
        "hpc_agent.ops.queue.maintenance.retired_item_census", side_effect=OSError("disk on fire")
    ):
        report = groom_queue_stores(exp)

    assert report["error"] == "OSError: disk on fire"
    assert report["dropped_items"] == 0
    assert report["raced_items"] == []


# ── 5. what the retryable(n) budget actually bounds (F4) ─────────────────────


def _tick(exp: Path, run_id: str, action: str) -> None:
    result = BlockDriveResult(action=action, run_id=run_id, reason="test")  # type: ignore[arg-type]
    with mock.patch("hpc_agent.ops.block_drive_op.run_tick", return_value=(result, 0)):
        block_drive(exp, spec=BlockDriveSpec(run_id=run_id))


def _drivable(item: Any) -> bool:
    """The drain plan's formula, spelled exactly as §5 states it."""
    return item.dispatched and not item.terminal and (not item.parked or item.greenlight_unadvanced)


def test_a_park_with_no_greenlight_costs_exactly_one_attempt_and_then_freezes(
    tmp_path: Path,
) -> None:
    """The traced reality the field's documentation now states.

    The budget is NOT a bound on "a genuinely-wedged run": the plan's ¬parked
    clause removes an unanswered park from the drivable set, so the tick that
    parked it is the only one charged and the counter sits at 1 until a human
    answers. What the budget really bounds is the greenlight-unadvanced spin and
    the skip spin — the two loops with no human in them.
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "i-park", "ml-11112222")
    _place(exp, "i-park", "ml-11112222")
    _seed_run(exp, "ml-11112222", status="in_flight")

    _tick(exp, "ml-11112222", "awaiting_decision")
    mark_pending_decision(
        "ml-11112222",
        block="submit-s2",
        workflow="submit",
        brief={"summary": "canary done"},
        resume_cursor={"next_verb": "submit-s3"},
        awaiting_since=_NOW,
        experiment_dir=exp,
    )

    item = _by_id(_status(exp))["i-park"]
    assert (item.parked, item.greenlight_unadvanced) == (True, False)
    assert item.drive_attempts == 1
    # The plan will not tick it again, so the counter cannot climb toward the
    # ceiling while the human is the one holding it up.
    assert _drivable(item) is False


def test_the_skip_spin_is_what_the_budget_actually_stops(tmp_path: Path) -> None:
    """The other half of the same statement: an item the plan KEEPS driving, whose
    tick keeps answering ``skip``, is the loop the ceiling exists to break."""
    exp = _exp(tmp_path)
    _enqueue(exp, "i-spin", "ml-33334444")
    _place(exp, "i-spin", "ml-33334444")
    _seed_run(exp, "ml-33334444", status="in_flight")

    for _ in range(3):
        _tick(exp, "ml-33334444", "skip")

    item = _by_id(_status(exp))["i-spin"]
    assert _drivable(item) is True  # nothing removes it from the drivable set
    assert item.drive_attempts == 3  # so only the budget can
