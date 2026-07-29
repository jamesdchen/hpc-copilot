"""The declared retryable(n) failure-class leg — §7, RESOLVED as proposed.

``docs/plans/run-queue-placement-2026-07-28.md`` §7 "Failure classes on parked
items": needs_human vs retryable(n) as DECLARED item data consumed by kernel
code — never an agent's judgment call; default needs_human; retryable only by
explicit intake flag. What is pinned, one fire-path per binding rule:

* **Default = needs_human, byte-identical.** An item with no declared budget
  whose run fails produces NO retry rows, NO ledger growth, and the same
  dispatch result as before the leg existed.
* **The class covers MECHANICAL failure terminals only.** The one classifier
  (``ops/queue/retry.is_declared_retry_failure``) accepts a plain ``failed``
  record and refuses each guarded shape — complete, abandoned (kills and
  submit-window events fold into it, so the class defaults to the human),
  superseded, parked on a decision, held on an escalation verdict,
  kill-requested — with the negative twin asserted per guard so the predicate
  provably discriminates rather than always firing.
* **A retry is a NEW derived-id item reusing the recorded resolved identity
  VERBATIM.** ``<root>.retry<k>``, same spec / run_id / cmd_sha (§10.S3:
  never a re-resolve), cluster PINNED to the placement the spec targets, the
  budget and chain facts carried as arrival facts.
* **The same tick places and starts the retry** through the ordinary
  advance→dispatch machinery — the failed record is deliberately not
  adoptable, so the start is a mint-over-corpse — and the disclosure names it
  a declared-retry on both the rows and the brief.
* **Counting is durable and race-safe.** A queued tip is the pending answer
  (no double-mint), a stale racer's append dedups into a ``replayed`` row,
  and an exhausted budget writes nothing, parks for a human, and says so —
  on the dispatch brief and in ``queue-status``'s notes, whose
  ``retryable`` / ``retries_used`` fields carry the declared shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent._wire.queries.queue_status import QueueStatusSpec
from hpc_agent._wire.workflows.campaign_run import CampaignRunResult
from hpc_agent._wire.workflows.queue_dispatch import QueueDispatchSpec
from hpc_agent.ops.queue.dispatch import queue_dispatch
from hpc_agent.ops.queue.retry import is_declared_retry_failure, produce_declared_retries
from hpc_agent.ops.queue.status import queue_status
from hpc_agent.state.journal import mark_pending_decision, upsert_run
from hpc_agent.state.queue_intake import (
    append_intake_item,
    append_intake_placement,
    read_intake_items,
    read_intake_records,
)
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-07-29T12:00:00+00:00"

#: The submit seat the actor composes, patched at its SOURCE module (the same
#: point tests/ops/queue/test_queue_dispatch.py patches).
_RUN = "hpc_agent.ops.campaign_run.campaign_run"

_CLUSTERS = """
alpha:
  scheduler: slurm
  host: alpha.edu
  user: me
  max_walltime_sec: 86400
"""

_RUN_ID = "ml-aaaa1111"
_CMD_SHA = "aaaa1111"


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


def _submit_spec(run_id: str = _RUN_ID) -> dict[str, Any]:
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


def _enqueue(exp: Path, item_id: str, **record: Any) -> None:
    """One RESOLVED item (the §10.S3 shape) — identity + inline submit spec."""
    record.setdefault("run_name", "ml")
    record.setdefault("run_id", _RUN_ID)
    record.setdefault("cmd_sha", _CMD_SHA)
    record.setdefault("cluster_pin", "alpha")
    record.setdefault("campaign_base", "study")
    record.setdefault("spec", _submit_spec(str(record["run_id"])))
    append_intake_item(exp, record=record, request_id=item_id)


def _place(exp: Path, item_id: str, run_id: str = _RUN_ID) -> None:
    append_intake_placement(
        exp,
        item_id=item_id,
        request_id=f"{item_id}.placed",
        cluster="alpha",
        campaign_id="study_alpha",
        reason="only candidate",
        run_id=run_id,
    )


def _seed_run(exp: Path, run_id: str = _RUN_ID, *, status: str = "failed", **kw: Any) -> RunRecord:
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


def _detached(run_id: str = _RUN_ID, pid: int = 4242) -> CampaignRunResult:
    return CampaignRunResult(
        stage_reached="detached",
        needs_decision=False,
        reason="detached",
        run_id=run_id,
        started=True,
        watch="journal",
        detached_pid=pid,
    )


def _spec(**kw: Any) -> QueueDispatchSpec:
    kw.setdefault("now", _NOW)
    return QueueDispatchSpec(**kw)


def _item(exp: Path, item_id: str) -> dict[str, Any]:
    return next(i for i in read_intake_items(exp) if i.get("item_id") == item_id)


# ── the classifier: mechanical failure terminals ONLY ────────────────────────


def _record(status: str = "failed", **kw: Any) -> RunRecord:
    return RunRecord(
        run_id=_RUN_ID,
        profile="ml",
        cluster="alpha",
        ssh_target="me@alpha.edu",
        remote_path="/scratch/ml",
        job_name="ml_array",
        job_ids=["1"],
        total_tasks=1,
        submitted_at="2026-07-29T00:00:00+00:00",
        experiment_dir="/tmp/x",
        status=status,
        **kw,
    )


def test_a_plain_failed_terminal_is_the_one_shape_the_class_covers() -> None:
    assert is_declared_retry_failure(_record("failed")) is True


@pytest.mark.parametrize(
    ("why", "record"),
    [
        ("no record: the enqueue→dispatch window is not a terminal", None),
        ("complete is a success", _record("complete")),
        ("in_flight is live (timeout stays in_flight by design)", _record("in_flight")),
        ("submitting is a live dispatch window", _record("submitting")),
        (
            "abandoned folds kills / submit-window events / lost tracking — needs_human",
            _record("abandoned"),
        ),
        (
            "superseded is a retirement with no failure in it",
            _record("failed", superseded_by="ml-bbbb2222"),
        ),
        (
            "an escalation hold is a human's open question (anomaly terminators park)",
            _record("failed", pending_verdict={"kind": "anomaly"}),
        ),
        (
            "a parked decision boundary is a human's open question",
            _record("failed", pending_decision={"block": "submit-s2", "awaiting_since": _NOW}),
        ),
        (
            "a requested kill is a human's halt even when the terminal reads failed",
            _record("failed", kill_requested_at=_NOW),
        ),
    ],
)
def test_every_guarded_shape_is_refused(why: str, record: RunRecord | None) -> None:
    """Each guard's negative twin, so the classifier provably discriminates."""
    assert is_declared_retry_failure(record) is False, why


# ── default needs_human: byte-identical when nothing is declared ─────────────


def test_no_declaration_means_no_retry_no_rows_no_ledger_growth(tmp_path: Path) -> None:
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")  # no retryable
    _place(exp, "item-1")
    _seed_run(exp)  # failed

    before = read_intake_records(exp)
    with mock.patch(_RUN) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    assert res.declared_retries == []
    assert res.stage_reached == "nothing_to_dispatch"
    assert read_intake_records(exp) == before  # not one line written
    assert "declared-retry" not in res.brief
    m_run.assert_not_called()


def test_a_declared_budget_with_a_live_run_retries_nothing(tmp_path: Path) -> None:
    """The budget is armed by a FAILURE, not by existing: a healthy in-flight
    run under a declared budget is left entirely alone."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", retryable=3)
    _place(exp, "item-1")
    _seed_run(exp, status="in_flight")

    with mock.patch(_RUN):
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    assert res.declared_retries == []
    assert len(read_intake_items(exp)) == 1


# ── the retry: derived id, verbatim identity, same-tick dispatch ─────────────


def test_a_declared_retry_is_enqueued_placed_and_started_in_one_tick(tmp_path: Path) -> None:
    """The whole loop: failed run + declared budget → derived-id retry item
    reusing the recorded resolved identity VERBATIM, placed by advance and
    started by this same tick (mint-over-corpse — the failed record is
    deliberately not adoptable), with the declared-retry named out loud."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", retryable=2)
    _place(exp, "item-1")
    _seed_run(exp)  # failed

    with mock.patch(_RUN, return_value=_detached()) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    (row,) = res.declared_retries
    assert (row.root_item_id, row.item_id, row.attempt) == ("item-1", "item-1.retry1", 1)
    assert (row.retryable, row.run_id, row.outcome) == (2, _RUN_ID, "enqueued")
    assert "declared-retry" in row.reason

    # The retry item carries the tip's recorded resolved identity verbatim
    # (§10.S3: never re-resolved) plus the chain facts, and PINS the cluster.
    retry = _item(exp, "item-1.retry1")
    assert retry["run_id"] == _RUN_ID
    assert retry["cmd_sha"] == _CMD_SHA
    assert retry["spec"] == _submit_spec()
    assert retry["cluster_pin"] == "alpha"
    assert retry["retryable"] == 2
    assert (retry["retry_root"], retry["retry_attempt"], retry["retry_of"]) == (
        "item-1",
        1,
        "item-1",
    )

    # Same tick: advance placed the retry and dispatch STARTED it (not adopted
    # — a corpse is not a dispatch), over the same computed run id.
    (started,) = res.dispatched
    assert (started.item_id, started.outcome, started.run_id) == (
        "item-1.retry1",
        "started",
        _RUN_ID,
    )
    m_run.assert_called_once()
    assert "declared-retry" in res.brief


def test_a_queued_tip_is_the_pending_answer_no_double_mint(tmp_path: Path) -> None:
    """Once a retry sits queued, another producer pass mints NOTHING: attempt
    k+2 before k+1 ran would burn budget on nothing."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", retryable=3)
    _place(exp, "item-1")
    _seed_run(exp)

    first, appended_first = produce_declared_retries(exp, read_intake_items(exp))
    second, appended_second = produce_declared_retries(exp, read_intake_items(exp))

    assert [r.outcome for r in first] == ["enqueued"] and appended_first is True
    assert second == [] and appended_second is False
    assert len(read_intake_items(exp)) == 2  # root + retry1, nothing else


def test_a_stale_racer_dedups_into_a_replayed_row(tmp_path: Path) -> None:
    """Two ticks deciding over the SAME pre-append fold derive the same
    ``<root>.retry1`` and the ledger keeps one line — the loser's row says
    'replayed', never a second enqueue (the §10.S2 dedup argument)."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", retryable=3)
    _place(exp, "item-1")
    _seed_run(exp)

    stale_fold = read_intake_items(exp)  # both racers read BEFORE either writes
    first, _ = produce_declared_retries(exp, stale_fold)
    second, appended_second = produce_declared_retries(exp, stale_fold)

    assert [r.outcome for r in first] == ["enqueued"]
    assert [r.outcome for r in second] == ["replayed"]
    assert appended_second is False
    assert sum(1 for i in read_intake_items(exp) if i["item_id"] == "item-1.retry1") == 1


def test_the_budget_counts_up_the_chain_from_the_ledger(tmp_path: Path) -> None:
    """Attempt 2 is derived from the DURABLE chain (max retry_attempt on the
    fold), and the retry-of pointer names the tip, not the root."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", retryable=2)
    _place(exp, "item-1")
    # Retry 1 already spent: on the ledger and PLACED (dispatched, failed again).
    _enqueue(
        exp,
        "item-1.retry1",
        retryable=2,
        retry_root="item-1",
        retry_attempt=1,
        retry_of="item-1",
    )
    _place(exp, "item-1.retry1")
    _seed_run(exp)  # the shared run id is failed again

    rows, appended = produce_declared_retries(exp, read_intake_items(exp))

    (row,) = rows
    assert (row.item_id, row.attempt, row.outcome) == ("item-1.retry2", 2, "enqueued")
    assert appended is True
    retry2 = _item(exp, "item-1.retry2")
    assert (retry2["retry_root"], retry2["retry_attempt"], retry2["retry_of"]) == (
        "item-1",
        2,
        "item-1.retry1",
    )


# ── exhaustion: parks for a human, named out loud ────────────────────────────


def _exhausted_chain(exp: Path) -> None:
    """retryable(1), retry 1 spent and failed again — the budget is gone."""
    _enqueue(exp, "item-1", retryable=1)
    _place(exp, "item-1")
    _enqueue(
        exp,
        "item-1.retry1",
        retryable=1,
        retry_root="item-1",
        retry_attempt=1,
        retry_of="item-1",
    )
    _place(exp, "item-1.retry1")
    _seed_run(exp)


def test_an_exhausted_budget_writes_nothing_and_names_the_exhaustion(tmp_path: Path) -> None:
    exp = _exp(tmp_path)
    _exhausted_chain(exp)

    before = read_intake_records(exp)
    with mock.patch(_RUN) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    (row,) = res.declared_retries
    assert (row.outcome, row.item_id, row.attempt, row.retryable) == ("exhausted", None, 1, 1)
    assert "EXHAUSTED" in row.reason and "parks for a human" in row.reason
    assert read_intake_records(exp) == before  # nothing enqueued
    # The tick had nothing to dispatch, and the brief STILL names the
    # exhaustion — the one fact the human must not have to diff two reads for.
    assert res.stage_reached == "nothing_to_dispatch"
    assert "declared-retry (exhausted)" in res.brief
    m_run.assert_not_called()


def test_the_terminals_that_always_park_are_never_retried(tmp_path: Path) -> None:
    """Integration twin of the classifier table: abandoned, superseded, held,
    parked-on-decision — each with a declared budget — produce no rows."""
    exp = _exp(tmp_path)
    cases = [
        ("i-abandoned", "ml-11111111", {"status": "abandoned"}),
        ("i-superseded", "ml-22222222", {"status": "failed", "superseded_by": "ml-99999999"}),
        ("i-held", "ml-33333333", {"status": "failed", "pending_verdict": {"kind": "anomaly"}}),
        ("i-parked", "ml-44444444", {"status": "failed"}),
        ("i-killed", "ml-55555555", {"status": "failed", "kill_requested_at": _NOW}),
    ]
    for item_id, run_id, kw in cases:
        _enqueue(exp, item_id, run_id=run_id, cmd_sha=run_id.split("-")[1], retryable=3)
        _place(exp, item_id, run_id)
        _seed_run(exp, run_id, **kw)
    mark_pending_decision(
        "ml-44444444",
        block="submit-s2",
        workflow="submit",
        brief={"summary": "canary done"},
        resume_cursor={"next_verb": "submit-s3"},
        awaiting_since=_NOW,
        experiment_dir=exp,
    )

    rows, appended = produce_declared_retries(exp, read_intake_items(exp))
    assert rows == [] and appended is False


# ── queue-status: the declared shape is disclosed ────────────────────────────


def test_queue_status_reports_retryable_and_retries_used_per_item(tmp_path: Path) -> None:
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", retryable=2)
    _place(exp, "item-1")
    _enqueue(
        exp,
        "item-1.retry1",
        retryable=2,
        retry_root="item-1",
        retry_attempt=1,
        retry_of="item-1",
    )
    _enqueue(exp, "item-plain", run_id="ml-bbbb2222", cmd_sha="bbbb2222")
    _seed_run(exp)  # failed → the chain's rows are settled-hidden by default

    # include_settled=True is the audit read: a failed-terminal join hides the
    # chain's rows from the default page, and the declared shape must still be
    # readable there.
    res = queue_status(experiment_dir=exp, spec=QueueStatusSpec(include_settled=True))
    by_id = {item.item_id: item for item in res.items}

    assert by_id["item-1"].retryable == 2
    assert by_id["item-1"].retries_used == 1
    assert by_id["item-1.retry1"].retryable == 2
    assert by_id["item-1.retry1"].retries_used == 1  # identical across the chain
    assert by_id["item-plain"].retryable is None  # needs_human, the default
    assert by_id["item-plain"].retries_used == 0


def test_queue_status_names_exhaustion_as_the_reason_the_item_parks(tmp_path: Path) -> None:
    """The load-bearing disclosure: an exhausted chain must read as 'budget
    exhausted, parks for a human', not as an unexplained failed run — spoken
    ONCE, by the chain's tip."""
    exp = _exp(tmp_path)
    _exhausted_chain(exp)

    res = queue_status(experiment_dir=exp)
    exhaustion = [n for n in res.notes if "EXHAUSTED" in n]
    assert len(exhaustion) == 1  # the tip speaks; the root does not repeat it
    assert "retryable(1)" in exhaustion[0]
    assert "parks for a human" in exhaustion[0]
    assert "item-1.retry1" in exhaustion[0]


def test_queue_status_says_a_declared_retry_is_pending_not_parked(tmp_path: Path) -> None:
    """Mid-chain honesty: a failed run whose budget still has room reads as
    'the next tick re-enqueues', so the human is not summoned for a park the
    kernel is about to answer."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", retryable=2)
    _place(exp, "item-1")
    _seed_run(exp)

    res = queue_status(experiment_dir=exp)
    pending = [n for n in res.notes if "declared retry" in n or "retryable(2)" in n]
    assert len(pending) == 1
    assert "re-enqueues declared retry 1" in pending[0]


def test_queue_status_notes_stay_silent_without_a_declaration(tmp_path: Path) -> None:
    """needs_human items keep today's surfaces byte-identical: no retry note,
    null budget, zero used."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")
    _place(exp, "item-1")
    _seed_run(exp)

    res = queue_status(experiment_dir=exp, spec=QueueStatusSpec(include_settled=True))
    assert not any("retry" in note.lower() for note in res.notes)
    (item,) = res.items
    assert item.retryable is None and item.retries_used == 0
