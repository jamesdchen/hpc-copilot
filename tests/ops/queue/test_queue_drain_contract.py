"""Phase 3 — the substrate that makes an always-draining loop cheap and decidable.

``docs/plans/run-queue-placement-2026-07-28.md`` §5 (relay), §7
(relaunch-cheapness), §8 S12 (O(history) at pass startup). The drain loop itself
is plan-side composition of shipped verbs; what the kernel owes it is exactly
four things, one section below each:

1. **Every field the plan's drivability formula needs is PUBLISHED.** The plan
   computes ``drivable := dispatched ∧ ¬terminal ∧ (¬parked ∨
   greenlight_unadvanced)`` per item from ``queue-status`` output. If any input
   were missing the plan would have to infer it, and an inferred kernel fact is
   a second definition of it. Pinned positively (the fields exist, with the
   values the stores actually hold) AND at the two edges the four-field form
   cannot see — an escalation ``held`` and a ``superseded_by`` whose status has
   not caught up.
2. **Compaction fires at the WRITE authority, never on a read**, and a compacted
   ledger still answers ``queue-status`` and the R8 request_id dedup correctly
   for every STILL-LIVE item.
3. **A status pass over a ledger full of terminal runs does O(active) work.**
   Behavioural proxy, not a stopwatch: after grooming, neither the ledger nor
   the journal still holds the settled entries a pass would otherwise walk.
4. **The retryable(n) counter increments at exactly one write point and
   survives a relaunch** — it is read back off disk, because a budget that lives
   in a plan variable dies with the pass that spent it.

Cluster-free: the journal home is redirected via ``HPC_JOURNAL_DIR`` and the one
submit seat is mocked; nothing here opens a socket.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent._kernel.contract.layout import JournalLayout
from hpc_agent._wire.queries.queue_status import QueueStatusItem, QueueStatusSpec
from hpc_agent._wire.workflows.block_drive import BlockDriveResult, BlockDriveSpec
from hpc_agent._wire.workflows.campaign_run import CampaignRunResult
from hpc_agent._wire.workflows.queue_dispatch import QueueDispatchSpec
from hpc_agent.ops.block_drive_op import block_drive
from hpc_agent.ops.queue.dispatch import queue_dispatch
from hpc_agent.ops.queue.maintenance import groom_queue_stores
from hpc_agent.ops.queue.status import queue_status
from hpc_agent.state.index import prune_terminal_runs
from hpc_agent.state.journal import load_run, mark_pending_decision, upsert_run
from hpc_agent.state.queue_intake import (
    append_intake_item,
    append_intake_placement,
    compaction_watermark,
    intake_path_if_exists,
    read_intake_items,
)
from hpc_agent.state.queue_occupancy import retired_item_ids
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

#: The four projected fields the plan's drivability formula consumes. Named here
#: so the pin below fails LOUDLY on a rename rather than quietly returning
#: ``None`` for a field the plan then treats as falsey.
DRIVABLE_FORMULA_FIELDS = ("dispatched", "terminal", "parked", "greenlight_unadvanced")

#: The two further stop conditions the four-field form cannot express, plus the
#: retryable(n) budget. Published for the same reason: the plan must not infer
#: them.
DRIVABLE_EDGE_FIELDS = ("held", "superseded_by", "drive_attempts")


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


def _place(exp: Path, item_id: str, run_id: str) -> None:
    append_intake_placement(
        exp,
        item_id=item_id,
        request_id=f"{item_id}.placed",
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


def _by_id(result: Any) -> dict[str, QueueStatusItem]:
    return {item.item_id: item for item in result.items}


def _drivable(item: QueueStatusItem) -> bool:
    """The plan's formula, spelled exactly as §5 states it — nothing inferred."""
    return item.dispatched and not item.terminal and (not item.parked or item.greenlight_unadvanced)


def _lines(exp: Path) -> list[dict[str, Any]]:
    path = intake_path_if_exists(exp)
    if not path.is_file():
        return []
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]


# ── 1. every field the drivability formula needs is published ────────────────


def test_every_drivable_formula_field_is_a_declared_wire_field() -> None:
    """A positive pin on the WIRE shape, so a rename cannot silently degrade the
    plan's formula into ``getattr(item, 'parked', False)`` — which would read
    False forever and call every parked item drivable."""
    declared = set(QueueStatusItem.model_fields)
    missing = [f for f in (*DRIVABLE_FORMULA_FIELDS, *DRIVABLE_EDGE_FIELDS) if f not in declared]
    assert missing == []


def test_the_formula_fields_carry_the_stores_answers_for_each_class(tmp_path: Path) -> None:
    """One item per drivability class, all four fields read off the real stores.

    The negative twin matters as much as the positive: a parked item with NO
    committed greenlight must come back NOT drivable, or the loop would relay a
    tick at a human who has not answered.
    """
    exp = _exp(tmp_path)
    for item_id, run_id in (
        ("i-live", "ml-11111111"),
        ("i-parked", "ml-22222222"),
        ("i-done", "ml-33333333"),
        ("i-unsent", "ml-44444444"),
    ):
        _enqueue(exp, item_id, run_id)
        _place(exp, item_id, run_id)
    _seed_run(exp, "ml-11111111", status="in_flight")
    _seed_run(exp, "ml-22222222", status="in_flight")
    _seed_run(exp, "ml-33333333", status="complete")
    mark_pending_decision(
        "ml-22222222",
        block="submit-s2",
        workflow="submit",
        brief={"summary": "canary done"},
        resume_cursor={"next_verb": "submit-s3"},
        awaiting_since=_NOW,
        experiment_dir=exp,
    )

    items = _by_id(_status(exp))

    live, parked, done, unsent = (items[k] for k in ("i-live", "i-parked", "i-done", "i-unsent"))
    assert (live.dispatched, live.terminal, live.parked) == (True, False, False)
    assert _drivable(live) is True
    assert (parked.dispatched, parked.terminal, parked.parked) == (True, False, True)
    assert parked.greenlight_unadvanced is False
    assert _drivable(parked) is False  # no y committed: driving it would spin
    assert (done.dispatched, done.terminal) == (True, True)
    assert _drivable(done) is False
    assert unsent.dispatched is False  # no RunRecord: the enqueue→dispatch window
    assert _drivable(unsent) is False


def test_an_escalation_hold_is_published_because_the_four_field_form_cannot_see_it(
    tmp_path: Path,
) -> None:
    """``held`` is the second human-wait axis. Without it the formula calls a run
    holding on an escalation verdict drivable — a tick relayed at it forever."""
    exp = _exp(tmp_path)
    _enqueue(exp, "i-held", "ml-55555555")
    _place(exp, "i-held", "ml-55555555")
    _seed_run(exp, "ml-55555555", status="in_flight", pending_verdict={"kind": "anomaly"})

    result = _status(exp)
    item = _by_id(result)["i-held"]

    assert item.held is True
    assert item.parked is False
    assert _drivable(item) is True  # the four-field form's blind spot, demonstrated
    assert result.counts["held"] == 1
    assert any("HELD on an escalation verdict" in note for note in result.notes)


def test_a_superseded_run_whose_status_has_not_caught_up_is_published(tmp_path: Path) -> None:
    """``superseded_by`` is the field ``queue_occupancy.run_occupies`` retires a
    slot on. In the window before the status write lands, ``terminal`` is still
    False — so occupancy and a terminal-only drivability test disagree unless the
    field is published."""
    exp = _exp(tmp_path)
    _enqueue(exp, "i-super", "ml-66666666")
    _place(exp, "i-super", "ml-66666666")
    _seed_run(exp, "ml-66666666", status="in_flight", superseded_by="ml-77777777")

    result = _status(exp)
    item = _by_id(result)["i-super"]

    assert item.superseded_by == "ml-77777777"
    assert item.terminal is False
    assert retired_item_ids(exp) == {"i-super"}  # occupancy already retired it
    assert any("superseded by ml-77777777" in note for note in result.notes)


# ── 2. compaction fires at the WRITE authority, never on a read ──────────────


def test_queue_status_never_compacts_even_with_settled_items(tmp_path: Path) -> None:
    """R1/F46: a query must not groom the store it reports on. Two identical reads
    over a ledger FULL of settled items must leave the bytes untouched."""
    exp = _exp(tmp_path)
    _enqueue(exp, "i-old", "ml-88888888")
    _place(exp, "i-old", "ml-88888888")
    _seed_run(exp, "ml-88888888", status="complete")
    before = intake_path_if_exists(exp).read_bytes()

    _status(exp)
    _status(exp)

    assert intake_path_if_exists(exp).read_bytes() == before
    assert retired_item_ids(exp) == {"i-old"}  # it WAS compactable; the read declined


def test_a_dispatching_tick_compacts_settled_items_and_keeps_live_ones(tmp_path: Path) -> None:
    """The authority point: ``queue-dispatch`` grooms after it writes a placement.

    Three items — one settled, one mid-flight, one being dispatched now. Only the
    settled one leaves the ledger, and the item this very call reports on is
    exempt (never destroy the thing you're operating on).
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "i-settled", "ml-aaaa0001")
    _place(exp, "i-settled", "ml-aaaa0001")
    _seed_run(exp, "ml-aaaa0001", status="complete")
    _enqueue(exp, "i-live", "ml-aaaa0002")
    _place(exp, "i-live", "ml-aaaa0002")
    _seed_run(exp, "ml-aaaa0002", status="in_flight")
    _enqueue(exp, "i-new", "ml-aaaa0003")

    with mock.patch(_RUN, return_value=_detached("ml-aaaa0003")):
        res = queue_dispatch(experiment_dir=exp, spec=QueueDispatchSpec(now=_NOW))

    assert [row.item_id for row in res.dispatched] == ["i-new"]
    assert res.maintenance["dropped_items"] == 1
    assert res.maintenance["dropped_records"] == 2  # enqueue + placement
    assert {item["item_id"] for item in read_intake_items(exp)} == {"i-live", "i-new"}
    assert compaction_watermark(exp)["items_compacted"] == 1
    assert "compacted 1 settled item(s)" in res.brief


def test_a_tick_that_dispatches_nothing_grooms_nothing(tmp_path: Path) -> None:
    """§7's invariant is about exactly this pass. A drain tick that finds nothing
    to dispatch must not be charged an O(history) sweep."""
    exp = _exp(tmp_path)
    _enqueue(exp, "i-settled", "ml-bbbb0001")
    _place(exp, "i-settled", "ml-bbbb0001")
    _seed_run(exp, "ml-bbbb0001", status="complete")
    before = intake_path_if_exists(exp).read_bytes()

    with mock.patch(_RUN) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=QueueDispatchSpec(now=_NOW))

    assert res.stage_reached == "nothing_to_dispatch"
    assert res.maintenance == {}
    assert intake_path_if_exists(exp).read_bytes() == before
    m_run.assert_not_called()


def test_a_compacted_ledger_still_answers_status_and_r8_dedup_for_live_items(
    tmp_path: Path,
) -> None:
    """The safety property compaction must not trade away.

    After the settled item's records are gone, the LIVE item must still project
    the same run facts, and a replayed enqueue of its ``request_id`` must still
    dedup (``append_intake_item`` returns the PRE-EXISTING record and writes no
    second line) — R8 is what stops a replayed relay double-enqueuing.
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "i-settled", "ml-cccc0001")
    _place(exp, "i-settled", "ml-cccc0001")
    _seed_run(exp, "ml-cccc0001", status="failed")
    _enqueue(exp, "i-live", "ml-cccc0002")
    _place(exp, "i-live", "ml-cccc0002")
    _seed_run(exp, "ml-cccc0002", status="in_flight")

    report = groom_queue_stores(exp)
    assert report["dropped_items"] == 1

    item = _by_id(_status(exp))["i-live"]
    assert (item.dispatched, item.run_status, item.in_flight) == (True, "in_flight", True)
    assert item.run_id == "ml-cccc0002"

    lines_before = len(_lines(exp))
    replayed = append_intake_item(exp, record={"run_id": "ml-cccc0002"}, request_id="i-live")
    assert replayed is not None and replayed["item_id"] == "i-live"
    assert len(_lines(exp)) == lines_before  # no second line: dedup survived


def test_compaction_keeps_lines_it_could_not_parse(tmp_path: Path) -> None:
    """Compaction removes ANSWERED questions, never unanswerable ones: a torn or
    foreign line is the only evidence something went wrong, and dropping it would
    silently shrink ``skipped_records``."""
    exp = _exp(tmp_path)
    _enqueue(exp, "i-settled", "ml-dddd0001")
    _place(exp, "i-settled", "ml-dddd0001")
    _seed_run(exp, "ml-dddd0001", status="complete")
    path = intake_path_if_exists(exp)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"item_id": "torn", "sta\n')

    groom_queue_stores(exp)

    text = path.read_text(encoding="utf-8")
    assert '{"item_id": "torn", "sta' in text
    assert "ml-dddd0001" not in text
    assert _status(exp).skipped_records == 1


def test_grooming_never_raises_a_failure_into_the_dispatch(tmp_path: Path) -> None:
    """Hygiene must never turn a successful dispatch into an error envelope: the
    items already started, and the janitor's trouble is DATA."""
    exp = _exp(tmp_path)
    with mock.patch(
        "hpc_agent.ops.queue.maintenance.retired_item_ids", side_effect=OSError("disk on fire")
    ):
        report = groom_queue_stores(exp)

    assert report["error"] == "OSError: disk on fire"
    assert report["dropped_items"] == 0


# ── 3. O(active), shown behaviourally ────────────────────────────────────────


def test_grooming_leaves_neither_ledger_nor_journal_holding_settled_history(
    tmp_path: Path,
) -> None:
    """The O(active) proxy, on both stores a pass startup walks.

    ``queue-status`` folds every ledger line and — through ``occupied_slots`` →
    ``find_runs_by_campaign`` — ``load_run``s every run file in the namespace. So
    the cost is bounded only when BOTH stores stop holding settled entries. Six
    settled items + one live one, ``keep_terminal_runs=0``.
    """
    exp = _exp(tmp_path)
    for n in range(6):
        item_id, run_id = f"i-old{n}", f"ml-eeee000{n}"
        _enqueue(exp, item_id, run_id)
        _place(exp, item_id, run_id)
        _seed_run(exp, run_id, status="complete")
    _enqueue(exp, "i-live", "ml-ffff0001")
    _place(exp, "i-live", "ml-ffff0001")
    _seed_run(exp, "ml-ffff0001", status="in_flight")

    assert len(_lines(exp)) == 14
    report = groom_queue_stores(exp, keep_terminal_runs=0)

    assert report["dropped_items"] == 6
    assert report["pruned_runs"] == 6
    assert report["protected_runs"] == 1
    # Ledger: only the live item's two records remain.
    assert len(_lines(exp)) == 2
    assert all("eeee" not in json.dumps(line) for line in _lines(exp))
    # Journal: only the live run's record file remains.
    assert [p.name for p in sorted(JournalLayout(exp).runs.glob("*.json"))] == ["ml-ffff0001.json"]
    # And the pass still reports the live item truthfully.
    assert _by_id(_status(exp))["i-live"].in_flight is True


def test_prune_never_removes_a_run_a_live_ledger_item_still_references(
    tmp_path: Path,
) -> None:
    """S12's "define pruned-target semantics".

    ``queue-status`` projects ``dispatched`` by joining an item to its RunRecord,
    so pruning a referenced record would flip a real dispatch back to
    ``dispatched=false``. The guard is shown FIRING: without ``protect`` the same
    call removes the record and the projection lies.
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "i-keep", "ml-99990001")
    _place(exp, "i-keep", "ml-99990001")
    _seed_run(exp, "ml-99990001", status="complete")

    assert prune_terminal_runs(exp, 0, protect={"ml-99990001"}) == 0
    assert _by_id(_status(exp))["i-keep"].dispatched is True

    # The negative twin: unprotected, the record goes and the projection degrades.
    assert prune_terminal_runs(exp, 0) == 1
    assert _by_id(_status(exp))["i-keep"].dispatched is False


# ── 4. the retryable(n) counter ──────────────────────────────────────────────


def _tick(exp: Path, run_id: str, action: str, *, dry_run: bool = False) -> None:
    result = BlockDriveResult(action=action, run_id=run_id, reason="test")  # type: ignore[arg-type]
    with mock.patch("hpc_agent.ops.block_drive_op.run_tick", return_value=(result, 0)) as m_tick:
        block_drive(exp, spec=BlockDriveSpec(run_id=run_id, dry_run=dry_run))
    m_tick.assert_called_once()


def _on_disk(exp: Path, run_id: str) -> int:
    """The counter as a RELAUNCHED process would read it — off the file, not memory."""
    payload = json.loads((JournalLayout(exp).runs / f"{run_id}.json").read_text(encoding="utf-8"))
    return int(payload["drive_attempts"])


def test_futile_ticks_accumulate_and_a_progress_tick_resets_them(tmp_path: Path) -> None:
    """The one write point: ``awaiting_decision`` / ``skip`` moved nothing and cost
    a budget unit; anything that moved the chain earns a fresh budget."""
    exp = _exp(tmp_path)
    _seed_run(exp, "ml-drive001", status="in_flight")

    _tick(exp, "ml-drive001", "awaiting_decision")
    assert _on_disk(exp, "ml-drive001") == 1
    _tick(exp, "ml-drive001", "skip")
    assert _on_disk(exp, "ml-drive001") == 2
    _tick(exp, "ml-drive001", "advanced")
    assert _on_disk(exp, "ml-drive001") == 0
    _tick(exp, "ml-drive001", "awaiting_decision")
    assert _on_disk(exp, "ml-drive001") == 1


def test_the_counter_survives_a_relaunch_and_is_projected_by_queue_status(
    tmp_path: Path,
) -> None:
    """It is KERNEL state, not a plan variable. A pass that spent two attempts can
    die; the next pass reads the same 2 off disk through ``queue-status``."""
    exp = _exp(tmp_path)
    _enqueue(exp, "i-spin", "ml-drive002")
    _place(exp, "i-spin", "ml-drive002")
    _seed_run(exp, "ml-drive002", status="in_flight")

    _tick(exp, "ml-drive002", "awaiting_decision")
    _tick(exp, "ml-drive002", "awaiting_decision")

    # Forget everything in memory; re-read the record from the file.
    assert _on_disk(exp, "ml-drive002") == 2
    assert load_run(exp, "ml-drive002").drive_attempts == 2  # type: ignore[union-attr]
    assert _by_id(_status(exp))["i-spin"].drive_attempts == 2


def test_a_dry_run_preview_never_spends_the_budget(tmp_path: Path) -> None:
    """A ``dry_run`` executes no span. Charging it would let a plan burn an item's
    retry budget by looking at it."""
    exp = _exp(tmp_path)
    _seed_run(exp, "ml-drive003", status="in_flight")

    _tick(exp, "ml-drive003", "awaiting_decision", dry_run=True)

    assert _on_disk(exp, "ml-drive003") == 0


def test_a_tick_that_drove_no_run_counts_nothing_and_does_not_raise(tmp_path: Path) -> None:
    """A ``skip`` with a null ``run_id`` has nothing to count against, and a run
    with no record must not turn a drive into an error envelope."""
    exp = _exp(tmp_path)

    result = BlockDriveResult(action="skip", run_id=None, reason="nothing drivable")
    with mock.patch("hpc_agent.ops.block_drive_op.run_tick", return_value=(result, 0)):
        assert block_drive(exp, spec=BlockDriveSpec()).action == "skip"

    ghost = BlockDriveResult(action="skip", run_id="ml-nosuchrun", reason="no record")
    with mock.patch("hpc_agent.ops.block_drive_op.run_tick", return_value=(ghost, 0)):
        assert block_drive(exp, spec=BlockDriveSpec(run_id="ml-nosuchrun")).action == "skip"
