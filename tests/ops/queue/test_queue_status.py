"""``queue-status`` — the projection, and the rules it can be shown to obey.

What these tests PIN beyond the happy path, one per binding rule:

* **R1** (the ledger is an INDEX) — a run status is never read back from
  intake: the same ledger row projects ``in_flight`` and then ``terminal`` with
  no second write, and a record that tries to smuggle a wider state onto the
  ledger is refused by the fold and disclosed rather than believed.
* **R7** (the R7 test, and the point of the verb) — the committed-greenlight
  question agrees with ``attention-queue`` in BOTH directions, including the
  case that separates the boundary-scoped predicate from the unscoped one: a
  consumed ``y`` followed by a re-park, where ``is_latest_committed_greenlight``
  still says yes. A guard you cannot show firing is a guard you have not
  tested, so that case asserts the wrong predicate WOULD have said True.
* **R4** (nothing is dropped silently) — hidden settled items, a clipped page,
  unfoldable records and a shared ``run_id`` each surface in ``notes`` and in
  ``skipped_records`` / ``counts``.
* **R9** (one occupancy predicate) — the reported occupancy equals
  ``state/queue_occupancy.occupied_slots`` for the same campaign.
* **no writes, not even a cache** — a read against a virgin experiment creates
  nothing, and a read against a populated one leaves the tree byte-identical.

Cluster-free: the journal home is redirected via ``HPC_JOURNAL_DIR``; nothing
here touches a process, a socket, or a cluster.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

import hpc_agent.ops.queue.status as status_mod
from hpc_agent import errors
from hpc_agent._wire.queries.queue_status import QueueStatusSpec
from hpc_agent.ops.attention_queue import (
    GREENLIGHT_UNADVANCED,
    RUN_PARKED,
    collect_greenlight_and_parked,
)
from hpc_agent.ops.queue.status import queue_status
from hpc_agent.state.decision_journal import (
    append_decision,
    is_latest_committed_greenlight,
)
from hpc_agent.state.journal import mark_pending_decision, upsert_run
from hpc_agent.state.queue_intake import (
    append_intake_item,
    append_intake_placement,
    intake_path,
    intake_path_if_exists,
)
from hpc_agent.state.queue_occupancy import occupied_slots
from hpc_agent.state.run_record import RunRecord

_NOW = "2026-07-29T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the journal tree so RunRecords land under tmp, not the real home."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


def _spec(**kw: Any) -> QueueStatusSpec:
    kw.setdefault("now", _NOW)
    return QueueStatusSpec(**kw)


def _enqueue(exp: Path, item_id: str, **record: Any) -> None:
    append_intake_item(exp, record=record, request_id=item_id)


def _place(
    exp: Path, item_id: str, *, cluster: str, campaign_id: str, reason: str, **kw: Any
) -> None:
    append_intake_placement(
        exp,
        item_id=item_id,
        request_id=f"{item_id}-place",
        cluster=cluster,
        campaign_id=campaign_id,
        reason=reason,
        **kw,
    )


def _run(
    exp: Path,
    run_id: str,
    *,
    status: str = "in_flight",
    job_ids: list[str] | None = None,
    **kw: Any,
) -> RunRecord:
    record = RunRecord(
        run_id=run_id,
        profile="prof",
        cluster="hoffman2",
        ssh_target="user@hoffman2",
        remote_path="/scratch/exp",
        job_name="job",
        job_ids=["1"] if job_ids is None else job_ids,
        total_tasks=4,
        submitted_at="2026-07-29T00:00:00+00:00",
        experiment_dir=str(exp),
        status=status,
        **kw,
    )
    upsert_run(exp, record)
    return record


def _tree(root: Path) -> dict[str, str]:
    """Every file under *root*, path -> content digest (the no-writes witness)."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _by_id(result: Any) -> dict[str, Any]:
    return {item.item_id: item for item in result.items}


# ── empty / non-creating ─────────────────────────────────────────────────────


def test_virgin_experiment_reads_clean_and_scaffolds_nothing(tmp_path: Path) -> None:
    """F46: a query must never create the tree it reports on."""
    exp = tmp_path / "exp"
    exp.mkdir()

    result = queue_status(experiment_dir=exp, spec=_spec())

    assert result.path == str(intake_path_if_exists(exp))
    assert result.items == []
    assert result.total_items == 0
    assert result.truncated is False
    assert result.counts == {
        "queued": 0,
        "placed": 0,
        "dispatched": 0,
        "in_flight": 0,
        "parked": 0,
        "held": 0,
        "greenlight_unadvanced": 0,
        "terminal": 0,
    }
    assert result.occupancy == {}
    assert result.notes == []
    # Nothing materialized: not the ledger, not .hpc, not its .gitignore.
    assert not (exp / ".hpc").exists()
    assert _tree(exp) == {}


def test_an_item_carrying_a_run_id_scaffolds_no_journal_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F46 on the JOIN side, which the virgin-experiment test cannot reach.

    With no ledger there is no ``run_id``, so ``_project`` returns before the
    journal is ever touched — the whole non-creating claim rested on a case that
    never exercised the join. An item that DOES carry a ``run_id`` (what
    ``append_intake_placement`` writes, and what queue-dispatch will write)
    routed straight into ``load_run`` -> ``JournalLayout.run_record`` ->
    ``journal_dir``, which mkdirs ``~/.claude/hpc/<repo_hash>/`` and writes
    ``repo.json`` — a ghost namespace minted by a verb declaring
    ``side_effects=[]``, and exactly what the fleet-wide ``*/repo.json`` walks
    then pick up.
    """
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(home / "hpc"))
    exp = tmp_path / "exp"
    exp.mkdir()
    _enqueue(exp, "item-a", run_id="sweep-abcd1234")
    _place(exp, "item-a", cluster="carc", campaign_id="sweep_carc", reason="only candidate")

    result = queue_status(experiment_dir=exp, spec=_spec())

    assert _by_id(result)["item-a"].dispatched is False
    assert list(home.rglob("*")) == []


def test_computed_at_honours_the_now_override(tmp_path: Path) -> None:
    assert queue_status(experiment_dir=tmp_path, spec=_spec()).computed_at == _NOW


def test_bad_now_override_is_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        queue_status(experiment_dir=tmp_path, spec=QueueStatusSpec(now="last tuesday"))


# ── ledger facts + the R1 projection ─────────────────────────────────────────


def test_queued_item_carries_arrival_facts_and_no_run_projection(tmp_path: Path) -> None:
    _enqueue(
        tmp_path,
        "item-a",
        run_name="sweep",
        run_id="sweep-abcd1234",
        campaign_base="sweep",
        cluster_pin="hoffman2",
        spec_ref="specs/sweep.json",
        resources={"gpu": True, "gpu_type": "a100", "cores": 8},
    )

    result = queue_status(experiment_dir=tmp_path, spec=_spec())
    (item,) = result.items

    assert item.item_id == "item-a"
    assert item.state == "queued"
    assert item.enqueued_at
    assert item.updated_at == item.enqueued_at
    assert item.age_sec is not None and item.age_sec >= 0
    assert item.run_name == "sweep"
    assert item.run_id == "sweep-abcd1234"
    assert item.campaign_base == "sweep"
    assert item.cluster_pin == "hoffman2"
    assert item.cluster is None  # only a placement sets the chosen cluster
    assert item.campaign_id is None
    assert item.spec_ref == "specs/sweep.json"
    assert item.resources.gpu is True
    assert item.resources.gpu_type == "a100"
    assert item.resources.cores == 8
    # No run exists: every projected fact is false, none of them stored.
    assert item.dispatched is False
    assert item.run_status is None
    assert (item.in_flight, item.terminal, item.parked) == (False, False, False)
    assert item.greenlight_committed is False
    assert result.counts["queued"] == 1
    assert any("still queued" in note for note in result.notes)


def test_placement_record_folds_into_placed_with_its_disclosed_reason(tmp_path: Path) -> None:
    """R4: a placed item never travels without the reason its cluster was chosen."""
    _enqueue(tmp_path, "item-a", run_id="sweep-abcd1234", campaign_base="sweep")
    _place(
        tmp_path,
        "item-a",
        cluster="carc",
        campaign_id="sweep_carc",
        reason="hoffman2 at 2 occupied; carc 0",
        run_id="sweep-abcd1234",
    )

    (item,) = queue_status(experiment_dir=tmp_path, spec=_spec()).items
    assert item.state == "placed"
    assert item.cluster == "carc"
    assert item.campaign_id == "sweep_carc"
    assert item.placement_reason == "hoffman2 at 2 occupied; carc 0"
    assert item.campaign_base == "sweep"  # the enqueue's facts survive the overlay


def test_run_status_is_projected_live_not_stored(tmp_path: Path) -> None:
    """R1: the SAME ledger row reads in_flight then terminal, with no intake write."""
    _enqueue(tmp_path, "item-a", run_id="sweep-abcd1234")
    _run(tmp_path, "sweep-abcd1234", status="in_flight")

    before = intake_path(tmp_path).read_bytes()
    item = _by_id(queue_status(experiment_dir=tmp_path, spec=_spec()))["item-a"]
    assert (item.dispatched, item.run_status, item.in_flight, item.terminal) == (
        True,
        "in_flight",
        True,
        False,
    )

    _run(tmp_path, "sweep-abcd1234", status="complete")
    result = queue_status(experiment_dir=tmp_path, spec=_spec(include_settled=True))
    item = _by_id(result)["item-a"]
    assert (item.run_status, item.in_flight, item.terminal) == ("complete", False, True)
    assert item.state == "queued"  # the LEDGER state never learned any of this
    assert intake_path(tmp_path).read_bytes() == before


def test_submitting_run_projects_dispatched_but_not_in_flight(tmp_path: Path) -> None:
    """The handoff fact S10 names: 'submitting' IS dispatched, and is not terminal."""
    _enqueue(tmp_path, "item-a", run_id="sweep-abcd1234")
    _run(tmp_path, "sweep-abcd1234", status="submitting", job_ids=[])

    item = _by_id(queue_status(experiment_dir=tmp_path, spec=_spec()))["item-a"]
    assert item.dispatched is True
    assert item.run_status == "submitting"
    assert (item.in_flight, item.terminal) == (False, False)


def test_fold_refuses_a_state_outside_the_intake_vocabulary(tmp_path: Path) -> None:
    """R1 enforced by the reader: a smuggled 'dispatched' is dropped and disclosed."""
    _enqueue(tmp_path, "item-a")
    with intake_path(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "kind": "placement",
                    "item_id": "item-a",
                    "request_id": "item-a-smuggle",
                    "state": "dispatched",
                    "ts": _NOW,
                }
            )
            + "\n"
        )

    result = queue_status(experiment_dir=tmp_path, spec=_spec())
    (item,) = result.items
    assert item.state == "queued"
    assert result.skipped_records == 1
    assert any("ledger line(s) skipped" in note for note in result.notes)


def test_torn_and_foreign_ledger_lines_are_counted_not_merely_survived(tmp_path: Path) -> None:
    """``skipped_records`` covers the READER's drops too, not only the fold's.

    The tolerant read is not the interesting property — every journal reader in
    this repo already survives a torn tail. The property is that surviving it is
    VISIBLE: a count the reader discards cannot be recovered downstream, which is
    why the drop count comes back from ``read_intake_records_counted`` rather
    than being inferred from the record list.
    """
    _enqueue(tmp_path, "item-a")
    with intake_path(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "enqueue", "item_id": "item-b", "sta\n')  # torn tail
        fh.write("\n")  # blank: whitespace is not lost data
        fh.write('["not", "an", "object"]\n')  # foreign: JSON, but not a record

    result = queue_status(experiment_dir=tmp_path, spec=_spec())
    (item,) = result.items
    assert item.item_id == "item-a"
    assert result.skipped_records == 2
    assert any("ledger line(s) skipped" in note for note in result.notes)


# ── R7: agreement with attention-queue, in both directions ───────────────────


def _park(exp: Path, run_id: str, *, block: str, awaiting_since: str) -> None:
    mark_pending_decision(
        run_id,
        block=block,
        workflow="submit",
        brief={},
        resume_cursor={},
        awaiting_since=awaiting_since,
        experiment_dir=exp,
    )


def test_parked_and_greenlit_agrees_with_attention_queue(tmp_path: Path) -> None:
    """R7 positive: both surfaces call the same boundary-scoped predicate."""
    _enqueue(tmp_path, "item-a", run_id="sweep-abcd1234")
    _run(tmp_path, "sweep-abcd1234", status="in_flight")
    _park(tmp_path, "sweep-abcd1234", block="submit-s3", awaiting_since="2026-07-29T03:00:00+00:00")
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="sweep-abcd1234",
        block="submit-s3",
        response="y",
        ts="2026-07-29T04:00:00+00:00",
    )

    item = _by_id(queue_status(experiment_dir=tmp_path, spec=_spec()))["item-a"]
    assert item.parked is True
    assert item.park_block == "submit-s3"
    assert item.awaiting_since == "2026-07-29T03:00:00+00:00"
    assert item.greenlight_committed is True
    assert item.greenlight_unadvanced is True

    attention = {i.scope_id: i.kind for i in collect_greenlight_and_parked(tmp_path, now=_NOW)}
    assert attention["sweep-abcd1234"] == GREENLIGHT_UNADVANCED


def test_parked_without_a_greenlight_agrees_with_attention_queue(tmp_path: Path) -> None:
    """R7 negative, plain: no y at all — both surfaces say 'still awaiting'."""
    _enqueue(tmp_path, "item-a", run_id="sweep-abcd1234")
    _run(tmp_path, "sweep-abcd1234", status="in_flight")
    _park(tmp_path, "sweep-abcd1234", block="submit-s2", awaiting_since="2026-07-29T03:00:00+00:00")

    item = _by_id(queue_status(experiment_dir=tmp_path, spec=_spec()))["item-a"]
    assert item.parked is True
    assert item.greenlight_committed is False
    assert item.greenlight_unadvanced is False

    attention = {i.scope_id: i.kind for i in collect_greenlight_and_parked(tmp_path, now=_NOW)}
    assert attention["sweep-abcd1234"] == RUN_PARKED


def test_consumed_greenlight_then_repark_is_not_unadvanced(tmp_path: Path) -> None:
    """R7, THE guard firing (run-12 finding 21 / bug-sweep #1).

    A ``y`` is journaled, a tick consumes it and re-parks with a NEWER
    ``awaiting_since``. Nothing is ever removed from the decision journal, so
    the unscoped ``is_latest_committed_greenlight`` still reads True — and a
    queue-status that used it would mint a false 'the human already said yes'
    on every scan. The boundary-scoped predicate refuses, and attention-queue
    refuses identically.
    """
    _enqueue(tmp_path, "item-a", run_id="sweep-abcd1234")
    _run(tmp_path, "sweep-abcd1234", status="in_flight")
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="sweep-abcd1234",
        block="submit-s3",
        response="y",
        ts="2026-07-29T04:00:00+00:00",
    )
    # ... consumed, block ran, re-parked at the SAME boundary, later stamp.
    _park(tmp_path, "sweep-abcd1234", block="submit-s3", awaiting_since="2026-07-29T05:00:00+00:00")

    # The wrong predicate would have said yes. That is why this test exists.
    assert is_latest_committed_greenlight(tmp_path, "run", "sweep-abcd1234") is True

    item = _by_id(queue_status(experiment_dir=tmp_path, spec=_spec()))["item-a"]
    assert item.parked is True
    assert item.greenlight_committed is False
    assert item.greenlight_unadvanced is False

    attention = {i.scope_id: i.kind for i in collect_greenlight_and_parked(tmp_path, now=_NOW)}
    assert attention["sweep-abcd1234"] == RUN_PARKED


def test_unparked_run_is_never_greenlit(tmp_path: Path) -> None:
    """The predicate is boundary-SCOPED: with no park there is no boundary to ask about."""
    _enqueue(tmp_path, "item-a", run_id="sweep-abcd1234")
    _run(tmp_path, "sweep-abcd1234", status="in_flight")
    append_decision(
        tmp_path, scope_kind="run", scope_id="sweep-abcd1234", block="submit-s3", response="y"
    )

    item = _by_id(queue_status(experiment_dir=tmp_path, spec=_spec()))["item-a"]
    assert item.parked is False
    assert item.greenlight_committed is False


def test_park_on_a_non_in_flight_run_is_disclosed(tmp_path: Path) -> None:
    """attention-queue enumerates parks over in_flight runs only — say so, don't hide it."""
    _enqueue(tmp_path, "item-a", run_id="sweep-abcd1234")
    _run(tmp_path, "sweep-abcd1234", status="submitting", job_ids=[])
    _park(tmp_path, "sweep-abcd1234", block="submit-s2", awaiting_since="2026-07-29T03:00:00+00:00")

    result = queue_status(experiment_dir=tmp_path, spec=_spec())
    assert _by_id(result)["item-a"].parked is True
    assert any("invisible to the fleet digest" in note for note in result.notes)
    # And the fleet digest genuinely does not see it — that is the disclosed fact.
    assert collect_greenlight_and_parked(tmp_path, now=_NOW) == []


def test_module_routes_through_the_scoped_predicate_only(tmp_path: Path) -> None:
    """R7 as a source pin, the attention-queue route-through discipline."""
    source = inspect.getsource(status_mod)
    assert "is_committed_greenlight_for_boundary(" in source
    # The unscoped reads are named in the module docstring as the thing NOT to
    # use; what must never appear is a CALL to either.
    assert "is_latest_committed_greenlight(" not in source
    assert "latest_decision(" not in source
    assert "read_decisions(" not in source


# ── boundedness, filters, disclosure (R4) ────────────────────────────────────


def test_settled_items_are_hidden_but_still_counted(tmp_path: Path) -> None:
    _enqueue(tmp_path, "item-live", run_id="live-abcd1234")
    _enqueue(tmp_path, "item-done", run_id="done-abcd1234")
    _run(tmp_path, "live-abcd1234", status="in_flight")
    _run(tmp_path, "done-abcd1234", status="complete")

    result = queue_status(experiment_dir=tmp_path, spec=_spec())
    assert [i.item_id for i in result.items] == ["item-live"]
    assert result.total_items == 1
    assert result.counts["terminal"] == 1
    assert result.counts["queued"] == 2  # counted before the hide
    assert any("settled item(s) hidden" in note for note in result.notes)

    audit = queue_status(experiment_dir=tmp_path, spec=_spec(include_settled=True))
    assert [i.item_id for i in audit.items] == ["item-live", "item-done"]
    assert audit.total_items == 2


def test_limit_clips_the_page_and_says_so(tmp_path: Path) -> None:
    for n in range(5):
        _enqueue(tmp_path, f"item-{n}")

    result = queue_status(experiment_dir=tmp_path, spec=_spec(limit=2))
    assert [i.item_id for i in result.items] == ["item-0", "item-1"]  # arrival order
    assert result.total_items == 5
    assert result.truncated is True
    assert any("clipped 5 matching item(s) to 2" in note for note in result.notes)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"state": "placed"}, ["item-placed"]),
        ({"state": "queued"}, ["item-pinned", "item-other"]),
        ({"campaign_base": "sweep"}, ["item-placed", "item-pinned"]),
        ({"cluster": "carc"}, ["item-placed", "item-pinned"]),
    ],
    ids=["by-state", "by-state-queued", "by-campaign-base", "by-cluster"],
)
def test_filters_are_ledger_side_only(
    tmp_path: Path, kwargs: dict[str, Any], expected: list[str]
) -> None:
    """The cluster filter matches a PIN or a placement — 'what is queued for carc'."""
    _enqueue(tmp_path, "item-placed", campaign_base="sweep")
    _place(tmp_path, "item-placed", cluster="carc", campaign_id="sweep_carc", reason="least loaded")
    _enqueue(tmp_path, "item-pinned", campaign_base="sweep", cluster_pin="carc")
    _enqueue(tmp_path, "item-other", campaign_base="other", cluster_pin="hoffman2")

    result = queue_status(experiment_dir=tmp_path, spec=_spec(**kwargs))
    assert [i.item_id for i in result.items] == expected


def test_run_id_collision_is_said_not_collapsed(tmp_path: Path) -> None:
    """§10.S2: two items resolving to one run id must be visible on both rows."""
    _enqueue(tmp_path, "item-a", run_id="sweep-abcd1234")
    _enqueue(tmp_path, "item-b", run_id="sweep-abcd1234")

    result = queue_status(experiment_dir=tmp_path, spec=_spec())
    items = _by_id(result)
    assert items["item-a"].collides_with == ["item-b"]
    assert items["item-b"].collides_with == ["item-a"]
    assert any("is claimed by 2 items" in note for note in result.notes)


def test_collision_is_visible_even_when_the_filter_narrows_to_one(tmp_path: Path) -> None:
    """Computed over the whole ledger: a filter must not hide the duplicate."""
    _enqueue(tmp_path, "item-a", run_id="sweep-abcd1234", campaign_base="sweep")
    _enqueue(tmp_path, "item-b", run_id="sweep-abcd1234", campaign_base="other")

    result = queue_status(experiment_dir=tmp_path, spec=_spec(campaign_base="sweep"))
    assert [i.item_id for i in result.items] == ["item-a"]
    assert result.items[0].collides_with == ["item-b"]


def test_placed_item_without_a_run_record_is_disclosed(tmp_path: Path) -> None:
    """Pre-dispatch or pruned — indistinguishable from here, so say both."""
    _enqueue(tmp_path, "item-a", run_id="sweep-abcd1234")
    _place(tmp_path, "item-a", cluster="carc", campaign_id="sweep_carc", reason="only candidate")

    result = queue_status(experiment_dir=tmp_path, spec=_spec())
    assert _by_id(result)["item-a"].dispatched is False
    assert any("not yet dispatched, or the record was" in note for note in result.notes)


def test_torn_and_foreign_ledger_lines_degrade_to_the_parseable_subset(tmp_path: Path) -> None:
    """A queue read is on the 'what needs me' path; one bad byte must never wedge it."""
    _enqueue(tmp_path, "item-a")
    with intake_path(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "enqueue", "item_id": "torn"\n')
        fh.write('"a bare json string, not a record"\n')
    _enqueue(tmp_path, "item-b")

    result = queue_status(experiment_dir=tmp_path, spec=_spec())
    assert [i.item_id for i in result.items] == ["item-a", "item-b"]


def test_a_non_utf8_byte_costs_one_line_not_the_whole_ledger(tmp_path: Path) -> None:
    """Tolerance is PER LINE. A whole-file decode failure is not tolerance, it is a wedge.

    ``read_text(encoding="utf-8")`` on a ledger holding one torn multi-byte
    write raised ``UnicodeDecodeError``, which the reader swallowed into
    ``([], 0)`` — so N queued items read as an empty queue with
    ``skipped_records=0`` and no note, on both "what needs me" surfaces at once,
    indistinguishable from a virgin experiment.
    """
    for n in range(5):
        _enqueue(tmp_path, f"item-{n}")
    with intake_path(tmp_path).open("ab") as fh:
        fh.write(b"\xff\n")

    result = queue_status(experiment_dir=tmp_path, spec=_spec())

    assert [i.item_id for i in result.items] == [f"item-{n}" for n in range(5)]
    assert result.counts["queued"] == 5
    assert result.skipped_records == 1
    assert any("skipped" in note for note in result.notes)


def test_an_unopenable_ledger_is_disclosed_not_reported_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that EXISTS but cannot be opened is not "there was nothing to lose".

    ``(_, 0)`` is reserved for that. Here the read yields nothing, so the drop
    count must be non-zero and a note must fire — reporting an empty queue over
    a ledger the reader could not open is the same silent-wedge failure by
    another route.
    """
    for n in range(3):
        _enqueue(tmp_path, f"item-{n}")

    real_read_bytes = Path.read_bytes

    def boom(self: Path) -> bytes:
        if self.name == "intake.jsonl":
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", boom)

    result = queue_status(experiment_dir=tmp_path, spec=_spec())

    assert result.items == []
    assert result.skipped_records == 1
    assert any("skipped" in note for note in result.notes)


# ── R9: the one occupancy predicate ──────────────────────────────────────────


def test_occupancy_matches_the_shared_predicate(tmp_path: Path) -> None:
    """R9: the number a refill decision rests on, from the one module that defines it."""
    _enqueue(tmp_path, "item-a", run_id="sweep-aaaa1111")
    _place(tmp_path, "item-a", cluster="carc", campaign_id="sweep_carc", reason="least loaded")
    _run(tmp_path, "sweep-bbbb2222", status="in_flight", campaign_id="sweep_carc")

    result = queue_status(experiment_dir=tmp_path, spec=_spec())
    assert result.occupancy == {"sweep_carc": occupied_slots(tmp_path, "sweep_carc")}
    # A placed item and a live run of the same cid are two distinct slots.
    assert result.occupancy["sweep_carc"] == 2


def test_occupancy_is_empty_when_no_item_is_placed(tmp_path: Path) -> None:
    _enqueue(tmp_path, "item-a", campaign_base="sweep")
    assert queue_status(experiment_dir=tmp_path, spec=_spec()).occupancy == {}


# ── no writes, not even a cache ──────────────────────────────────────────────


def test_read_leaves_the_experiment_tree_byte_identical(tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    exp.mkdir()
    _enqueue(exp, "item-a", run_id="sweep-abcd1234", campaign_base="sweep")
    _place(exp, "item-a", cluster="carc", campaign_id="sweep_carc", reason="least loaded")
    _run(exp, "sweep-abcd1234", status="in_flight", campaign_id="sweep_carc")
    _park(exp, "sweep-abcd1234", block="submit-s3", awaiting_since="2026-07-29T03:00:00+00:00")
    append_decision(
        exp, scope_kind="run", scope_id="sweep-abcd1234", block="submit-s3", response="y"
    )

    before = _tree(exp)
    first = queue_status(experiment_dir=exp, spec=_spec())
    assert _tree(exp) == before

    # Recomputed on every read, and stable: no digest file, no cache to go stale.
    second = queue_status(experiment_dir=exp, spec=_spec())
    assert second.model_dump(mode="json") == first.model_dump(mode="json")
    assert _tree(exp) == before


def test_bare_call_without_spec_is_the_whole_ledger_read(tmp_path: Path) -> None:
    """spec_required=False (the net-triage precedent): ``spec=None`` == ``{}``.

    A bare ``hpc-agent queue-status`` is a valid, meaningful request — the
    whole-ledger read with defaults — so the atom must accept ``spec=None``
    and answer exactly as it would for an explicit empty spec.
    """
    exp = tmp_path / "exp"
    exp.mkdir()
    _enqueue(exp, "item-a", campaign_base="sweep")

    bare = queue_status(experiment_dir=exp)
    explicit = queue_status(experiment_dir=exp, spec=QueueStatusSpec())

    assert bare.counts == explicit.counts
    assert [i.item_id for i in bare.items] == [i.item_id for i in explicit.items]
