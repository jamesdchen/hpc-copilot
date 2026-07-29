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

   Publishing the fields is only half of it. What the PLAN does with them lived
   for one release as a Python paraphrase in this file — and a paraphrase is a
   copy, so deleting ``item.held !== true`` and ``!item.superseded_by`` from
   ``queue-drain.js``, and renaming ``drive_attempts`` out of its
   ``ATTEMPT_FIELDS``, left the whole suite green while the shipped plan would
   have ticked held runs forever, driven superseded ones, and disarmed
   ``maxAttempts`` (every fallback in that plan fails OPEN: ``undefined !==
   true`` is true). Section 5 below therefore EXECUTES the plan's own
   ``isDrivable`` / ``attemptsOf`` under node, over item dicts the real
   ``queue_status`` projection produced.
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
import re
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent._kernel.contract.layout import JournalLayout
from hpc_agent._kernel.lifecycle.block_drive import block_drive_once
from hpc_agent._wire.queries.queue_status import QueueStatusItem, QueueStatusSpec
from hpc_agent._wire.workflows.block_drive import BlockDriveResult, BlockDriveSpec
from hpc_agent._wire.workflows.campaign_run import CampaignRunResult
from hpc_agent._wire.workflows.queue_dispatch import QueueDispatchSpec
from hpc_agent.ops.block_drive_op import block_drive
from hpc_agent.ops.queue.dispatch import queue_dispatch
from hpc_agent.ops.queue.maintenance import groom_queue_stores
from hpc_agent.ops.queue.status import queue_status
from hpc_agent.state.decision_journal import append_decision
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
from tests._node import run_node
from tests._workflow_plans import WORKFLOWS_DIR, portable_prefix

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-07-29T12:00:00+00:00"

#: The submit seat ``queue-dispatch`` composes, patched at its SOURCE module.
_RUN = "hpc_agent.ops.campaign_run.campaign_run"

#: How the janitor is made to fail: its ledger-rewrite leg, patched where
#: ``maintenance`` binds it. Any leg inside ``groom_queue_stores``'s try block
#: proves the same thing (hygiene reports, it never vetoes), so this names the
#: DURABLE one — the compaction call — rather than whichever retirement
#: predicate the janitor currently consults, which is an implementation detail
#: these tests have no business pinning.
_BROKEN_JANITOR = "hpc_agent.ops.queue.maintenance.compact_intake_ledger"

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


def _four_field_drivable(item: QueueStatusItem) -> bool:
    """§5's FOUR-FIELD form — deliberately NOT the plan's formula.

    The plan's real predicate (``queue-drain.js``'s ``isDrivable``) is this plus
    ``¬held`` and ``¬superseded_by``, and it is executed as itself in section 5.
    This narrow form stays because the whole reason those two extra fields are
    published is that the four-field version has blind spots — which is a claim
    only demonstrable by keeping the blind version around and showing it answer
    True where the shipped plan answers False.
    """
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
    assert _four_field_drivable(live) is True
    assert (parked.dispatched, parked.terminal, parked.parked) == (True, False, True)
    assert parked.greenlight_unadvanced is False
    assert _four_field_drivable(parked) is False  # no y committed: driving it would spin
    assert (done.dispatched, done.terminal) == (True, True)
    assert _four_field_drivable(done) is False
    assert unsent.dispatched is False  # no RunRecord: the enqueue→dispatch window
    assert _four_field_drivable(unsent) is False


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
    assert _four_field_drivable(item) is True  # the four-field form's blind spot, demonstrated
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
    items already started, and the janitor's trouble is DATA.

    Asked of the janitor alone. The claim, though, is about the DISPATCH — see
    the caller-level twin below, which is where the disclosure line lives.
    """
    exp = _exp(tmp_path)
    with mock.patch(_BROKEN_JANITOR, side_effect=OSError("disk on fire")):
        report = groom_queue_stores(exp)

    assert report["error"] == "OSError: disk on fire"
    assert report["dropped_items"] == 0


def test_a_broken_janitor_still_leaves_a_succeeding_dispatch_that_says_so(
    tmp_path: Path,
) -> None:
    """The same promise, asked AT THE CALLER — the layer that can actually break it.

    One layer down (above) only shows the janitor swallowing its own failure. It
    cannot see whether ``queue-dispatch`` still succeeds, whether the failure
    reaches ``result.maintenance``, or whether anyone is TOLD: the brief's
    "maintenance did not complete" line was deletable with the whole suite green,
    which would have made a half-groomed store silently invisible.

    So: a real dispatching tick with the janitor's retirement predicate raising.
    The item started, so the dispatch must succeed and the trouble must travel as
    data — twice, once machine-readable and once in the human brief.
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "i-new", "ml-groom001")

    with (
        mock.patch(_BROKEN_JANITOR, side_effect=OSError("disk on fire")),
        mock.patch(_RUN, return_value=_detached("ml-groom001")),
    ):
        res = queue_dispatch(experiment_dir=exp, spec=QueueDispatchSpec(now=_NOW))

    assert res.stage_reached == "dispatched"  # the drive happened; hygiene did not veto it
    assert [row.item_id for row in res.dispatched] == ["i-new"]
    assert res.maintenance["error"] == "OSError: disk on fire"
    assert res.maintenance["dropped_items"] == 0
    assert "maintenance did not complete: OSError: disk on fire" in res.brief, (
        "the dispatch succeeded with the store un-groomed and said nothing about it — "
        f"a silent janitor failure is an unbounded ledger nobody knows about: {res.brief!r}"
    )
    # And the ledger really was left alone, so the disclosure is not cosmetic.
    assert {item["item_id"] for item in read_intake_items(exp)} == {"i-new"}


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


def test_grooming_protects_the_terminal_run_a_surviving_item_still_references(
    tmp_path: Path,
) -> None:
    """The same guard, asked of ``groom_queue_stores`` — its CALLER.

    The test above proves ``prune_terminal_runs`` honours ``protect=``. It says
    nothing about whether the janitor passes it: dropping ``protect=protect``
    from ``maintenance.py`` left the suite green, because in every other case the
    surviving item joins a NON-terminal run, which the prune skips anyway. The
    guard only has work to do in exactly one shape — a live (excluded) ledger
    item whose run is ALREADY terminal — so that is the shape pinned here.

    It is not a contrived shape: it is the adopt-onto-a-complete-run case the
    janitor's own docstring calls out ("never destroy the thing you're operating
    on"). Without the wiring, this call prunes the record the excluded item still
    joins to and ``queue-status`` starts answering ``dispatched=false`` about a
    run that really happened.
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "i-adopted", "ml-prot0001")
    _place(exp, "i-adopted", "ml-prot0001")
    _seed_run(exp, "ml-prot0001", status="complete")

    report = groom_queue_stores(exp, exclude_item_ids=["i-adopted"], keep_terminal_runs=0)

    assert report["dropped_items"] == 0  # excluded: the caller is reporting on it
    assert report["protected_runs"] == 1  # …so its run is protected by construction
    assert report["pruned_runs"] == 0, (
        "the prune removed a terminal RunRecord a SURVIVING ledger item still references — "
        "`protect=` is computed and then not handed to prune_terminal_runs"
    )
    assert _by_id(_status(exp))["i-adopted"].dispatched is True
    assert (JournalLayout(exp).runs / "ml-prot0001.json").is_file()


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


def test_the_detach_child_entry_never_spends_the_retry_budget(tmp_path: Path) -> None:
    """The exemption in ``_count_drive_attempt``'s docstring, pinned at the entry
    it exempts.

    ``block_drive_once`` is the console / detach-child seat: a detached child
    polls its own chain and those polls are not a drain pass relaying ticks, so
    counting them would let a healthy long wait exhaust an item's budget and get
    it reported as ``held``. The exemption is structural (the counter lives on
    the agent-facing wrapper, not in ``run_tick``) — which means nothing fails if
    someone adds a stamp here, and nothing did: adding one produced zero test
    delta.

    Both sides in one test, because the exemption is a DIFFERENCE: the same tick
    outcome through the same run costs 0 here and 1 through ``block-drive``.
    """
    exp = _exp(tmp_path)
    _seed_run(exp, "ml-detach01", status="in_flight")
    parked = BlockDriveResult(action="awaiting_decision", run_id="ml-detach01", reason="parked")

    with mock.patch(
        "hpc_agent._kernel.lifecycle.block_drive.run_tick", return_value=(parked, 0)
    ) as m_tick:
        code = block_drive_once(exp, run_id="ml-detach01", workflow=None)

    m_tick.assert_called_once()
    assert code == 0
    assert _on_disk(exp, "ml-detach01") == 0, (
        "the detach-child / console entry charged the retryable(n) budget — a detached "
        "run polling itself would exhaust maxAttempts and be held for making progress"
    )

    # The contrast that makes the exemption an exemption rather than a dead path.
    _tick(exp, "ml-detach01", "awaiting_decision")
    assert _on_disk(exp, "ml-detach01") == 1


def test_a_tick_that_drove_no_run_counts_nothing_and_does_not_raise(tmp_path: Path) -> None:
    """A ``skip`` with a null ``run_id`` has nothing to count against, a run with
    no record must not turn a drive into an error envelope — and neither must a
    record whose counter is type-corrupt.

    The third case is why the catch is ``except Exception`` and not a tuple. It
    used to be ``(FileNotFoundError, OSError)``, and ``drive_attempts: "three"``
    on disk raises ``ValueError`` out of ``int()`` in
    ``journal.stamp_drive_attempt`` — crashing a drive that had ALREADY
    succeeded, over an advisory counter. "Never raises" has to mean never.
    """
    exp = _exp(tmp_path)

    result = BlockDriveResult(action="skip", run_id=None, reason="nothing drivable")
    with mock.patch("hpc_agent.ops.block_drive_op.run_tick", return_value=(result, 0)):
        assert block_drive(exp, spec=BlockDriveSpec()).action == "skip"

    ghost = BlockDriveResult(action="skip", run_id="ml-nosuchrun", reason="no record")
    with mock.patch("hpc_agent.ops.block_drive_op.run_tick", return_value=(ghost, 0)):
        assert block_drive(exp, spec=BlockDriveSpec(run_id="ml-nosuchrun")).action == "skip"

    _seed_run(exp, "ml-corrupt1", status="in_flight")
    record_path = JournalLayout(exp).runs / "ml-corrupt1.json"
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["drive_attempts"] = "three"
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    # ``awaiting_decision`` is the branch that READS the old value (a progress
    # action would reset to 0 and never touch it), so this is the reachable case.
    corrupt = BlockDriveResult(action="awaiting_decision", run_id="ml-corrupt1", reason="parked")
    with mock.patch("hpc_agent.ops.block_drive_op.run_tick", return_value=(corrupt, 0)):
        driven = block_drive(exp, spec=BlockDriveSpec(run_id="ml-corrupt1"))

    assert driven.action == "awaiting_decision"  # the drive succeeded; bookkeeping did not


# ── 5. the PLAN's formula, executed as itself ────────────────────────────────
#
# Sections 1-4 pin what the KERNEL publishes. This one pins what the PLAN does
# with it — and does so by running ``queue-drain.js``'s own ``isDrivable`` and
# ``attemptsOf`` under node, over the exact item dicts ``queue_status`` emits on
# the wire. A Python restatement of the formula lived here instead and was a
# copy: it agreed with itself while the shipped plan lost two of its six terms.
#
# Two arms, on purpose. The static arm runs everywhere and pins the field NAMES
# (anchored to the wire model, never to literals only spelled here). The
# executed arm is the one no amount of right-looking JS can satisfy.

_PLAN_PATH = WORKFLOWS_DIR / "queue-drain.js"

#: ``const isDrivable = (item) => …`` — the predicate, up to the blank line that
#: ends it. Extracted for the STATIC arm only; the executed arm loads the whole
#: plan prefix so the function it calls is the one the engine would.
_ISDRIVABLE_RE = re.compile(r"^const\s+isDrivable\s*=[^\n]*\n(?:.+\n)+", re.M)

#: ``const ATTEMPT_FIELDS = [...]`` — the counter's field-name preference list.
_ATTEMPT_FIELDS_RE = re.compile(r"^const\s+ATTEMPT_FIELDS\s*=\s*\[[^\]]*\]", re.M)


def _plan_text() -> str:
    return _PLAN_PATH.read_text(encoding="utf-8")


def _run_plan_predicates(tmp_path: Path, items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Run the SHIPPED ``isDrivable``/``attemptsOf`` over *items*, keyed by item_id."""
    tail = (
        "\nconst out = {}\n"
        "for (const item of JSON.parse(process.argv[2])) "
        "out[item.item_id] = { drivable: isDrivable(item), attempts: attemptsOf(item) }\n"
        "process.stdout.write(JSON.stringify(out))\n"
    )
    proc = run_node(
        tmp_path / "drivable.mjs",
        portable_prefix(_plan_text(), name=_PLAN_PATH.name) + tail,
        json.dumps(items),
    )
    assert proc.returncode == 0, f"queue-drain.js predicates threw: {proc.stderr.strip()}"
    return json.loads(proc.stdout)


def test_the_plans_drivability_source_reads_every_published_field(tmp_path: Path) -> None:
    """STATIC arm: every field name the formula depends on appears in the plan's
    own predicate source, and every one of those names is a real wire field.

    Both directions matter. A name only spelled in this test would pin nothing
    (hence the ``model_fields`` check); a name only spelled in the plan would be
    a field read that is ``undefined`` forever — and every fallback in that plan
    fails OPEN (``undefined !== true`` is true), so an undefined read does not
    park an item, it drives one that should not be driven.
    """
    declared = set(QueueStatusItem.model_fields)
    predicate = _ISDRIVABLE_RE.search(_plan_text())
    attempt_fields = _ATTEMPT_FIELDS_RE.search(_plan_text())
    assert predicate is not None, "queue-drain.js declares no `const isDrivable = (item) =>`"
    assert attempt_fields is not None, "queue-drain.js declares no `const ATTEMPT_FIELDS = [...]`"

    for field in (*DRIVABLE_FORMULA_FIELDS, "held", "superseded_by", "run_id"):
        assert field in declared, f"{field!r} is not a QueueStatusItem field"
        assert f"item.{field}" in predicate.group(0), (
            f"queue-drain.js's isDrivable never reads {field!r} — the kernel publishes it "
            "precisely so the plan does not have to infer it"
        )
    assert "drive_attempts" in declared
    assert "'drive_attempts'" in attempt_fields.group(0), (
        "queue-drain.js's ATTEMPT_FIELDS no longer names 'drive_attempts' — attemptsOf "
        "would read 0 for every item and maxAttempts would never hold anything"
    )


def test_the_shipped_plan_formula_answers_every_class_of_real_item(tmp_path: Path) -> None:
    """EXECUTED arm: ``queue-drain.js``'s own predicates, over the wire dicts the
    real ``queue_status`` projection produced for one item of every class.

    This is the arm that fails when the plan loses a term. It also carries the
    contrast that justifies publishing ``held`` / ``superseded_by`` at all: the
    four-field form calls both of those items drivable, and the shipped plan
    does not.
    """
    exp = _exp(tmp_path)
    for item_id, run_id in (
        ("i-live", "ml-fx000001"),
        ("i-parked", "ml-fx000002"),
        ("i-greenlit", "ml-fx000003"),
        ("i-held", "ml-fx000004"),
        ("i-super", "ml-fx000005"),
        ("i-done", "ml-fx000006"),
        ("i-unsent", "ml-fx000007"),
        ("i-spin", "ml-fx000008"),
    ):
        _enqueue(exp, item_id, run_id)
        _place(exp, item_id, run_id)
    _seed_run(exp, "ml-fx000001", status="in_flight")
    _seed_run(exp, "ml-fx000002", status="in_flight")
    _seed_run(exp, "ml-fx000003", status="in_flight")
    _seed_run(exp, "ml-fx000004", status="in_flight", pending_verdict={"kind": "anomaly"})
    _seed_run(exp, "ml-fx000005", status="in_flight", superseded_by="ml-fx999999")
    _seed_run(exp, "ml-fx000006", status="complete")
    # i-unsent: placed, never dispatched — no RunRecord exists.
    _seed_run(exp, "ml-fx000008", status="in_flight")

    for run_id, block in (("ml-fx000002", "submit-s2"), ("ml-fx000003", "submit-s3")):
        mark_pending_decision(
            run_id,
            block=block,
            workflow="submit",
            brief={"summary": "canary done"},
            # Empty cursor: the boundary target is then the block's chain
            # successor, which is the shape a bare `y` answers (the same park
            # shape ``tests/ops/queue/test_queue_status.py`` pins against the
            # attention queue). Nothing here decides that — the kernel does.
            resume_cursor={},
            awaiting_since=_NOW,
            experiment_dir=exp,
        )
    # Only the third one's human answered — a committed y at the parked boundary.
    append_decision(
        exp,
        scope_kind="run",
        scope_id="ml-fx000003",
        block="submit-s3",
        response="y",
        ts="2026-07-29T13:00:00+00:00",
    )
    # Two futile ticks, spent through the one write point.
    _tick(exp, "ml-fx000008", "awaiting_decision")
    _tick(exp, "ml-fx000008", "skip")

    items = _by_id(_status(exp))
    wire = [item.model_dump(mode="json") for item in _status(exp).items]
    verdict = _run_plan_predicates(tmp_path, wire)

    assert verdict["i-live"]["drivable"] is True
    assert verdict["i-parked"]["drivable"] is False  # no y committed: a tick would spin
    assert verdict["i-greenlit"]["drivable"] is True  # R1: the y is in, the run has not moved
    assert verdict["i-held"]["drivable"] is False  # escalation: no boundary a y could target
    assert verdict["i-super"]["drivable"] is False  # occupancy retired it; terminal lags
    assert verdict["i-done"]["drivable"] is False
    assert verdict["i-unsent"]["drivable"] is False  # nothing to tick: no RunRecord

    # The two edge fields earn their keep: the four-field form disagrees here.
    assert _four_field_drivable(items["i-held"]) is True
    assert _four_field_drivable(items["i-super"]) is True

    # And the retryable(n) ceiling reads the KERNEL's number, through the plan's
    # own attemptsOf — the read `maxAttempts` is compared against.
    assert items["i-spin"].drive_attempts == 2
    assert verdict["i-spin"]["attempts"] == 2, (
        "the plan's attemptsOf read 0 for a run that spent two futile ticks — every item "
        "would then sit forever below maxAttempts and the retry ceiling would be disarmed"
    )
    assert verdict["i-live"]["attempts"] == 0
