"""The DECLARED retryable(n) failure-class leg — kernel-consumed, never judged.

``docs/plans/run-queue-placement-2026-07-28.md`` §7 "Design decisions still
open (v2 additions)" proposed it and this module builds exactly that proposal
(RESOLVED 2026-07-29):

    Failure classes on parked items: needs_human vs retryable(n) as DECLARED
    item data consumed by plan code — never an agent's judgment call.
    Proposed: default needs_human; retryable only by explicit intake flag.

The shape, stated once:

* **The class is DECLARED at enqueue.** ``QueueRunSpec.retryable`` (a small
  positive int, junk refused at the wire) is the ONLY way an item becomes
  retryable. Absent means ``needs_human`` — a failed run parks for a person
  and nothing here touches it, byte-for-byte today's behavior. No terminal
  text, no failure fingerprint, and no agent opinion can widen the class:
  :func:`hpc_agent.state.queue_intake.item_retryable` reads the declared int
  or nothing.
* **A retry is a NEW intake item with a DERIVED identity.** The ledger is
  append-only and the fold has no "re-queued" transition, so re-flipping the
  failed item's own state would need a second placement token vocabulary and
  would erase the chain's history. Instead each retry is one more enqueue
  record, id ``<root>.retry<k>``
  (:func:`hpc_agent.state.queue_intake.retry_request_id`), which is also the
  append's dedup ``request_id`` — two dispatch ticks that both decide
  "attempt k+1 is owed" derive the same token and the in-flock probe leaves
  ONE line. The retry copies the tip's recorded resolved identity VERBATIM
  (spec, ``run_id``, ``cmd_sha``, ``run_name``): §10.S3's rule that resolving
  consumes the optuna sidecar index exactly once makes a re-resolve the worse
  bug (a different trial under the same slot), so the fresh attempt is minted
  over the SAME computed run id — which the dispatch decision table already
  supports (``_ADOPTABLE_STATUSES`` excludes ``failed``: "a corpse is not a
  dispatch, and the submit-once minter's own decision table mints a fresh
  attempt over one"). The retry also PINS the cluster the resolved spec
  targets: placement re-choosing would trip dispatch's spec-vs-cluster
  agreement check and the §10.S1 placement-drift leg, and R5 makes a pin the
  honest spelling of "this item's cluster is already decided".
* **Counting is durable and race-safe** —
  :func:`hpc_agent.state.queue_intake.retries_used` over the chain's folded
  ledger items, never an in-memory counter. The dedup on the derived id is
  what makes the count race-safe; append-only is what makes it durable; and a
  chain whose failure is still live cannot compact (a resubmittable-terminal
  run's items are never history — ``state/queue_occupancy.item_is_history``),
  so a spent budget cannot be groomed back.
* **What still ALWAYS parks.** Only a MECHANICAL failure terminal retries:
  journal status ``failed`` with no supersession, no pending human decision,
  no escalation hold, and no kill ever requested
  (:func:`is_declared_retry_failure`). ``complete`` obviously never;
  ``abandoned`` deliberately never — that status folds together confirmed
  kills (a human's halt), never-dispatched submit-window events, and lost
  tracking, and a class a machine cannot tell apart defaults to the human
  (the plan's stated default). Anomaly terminators and gate refusals never
  reach this module at all: an anomaly hold is ``pending_verdict``
  (``is_held``, folded in via ``is_resubmittable_terminal``) and a refused
  gate leaves no failed run to classify.
* **Everything is disclosed.** Each decision — enqueued, replayed, exhausted —
  is a ``QueueDeclaredRetry`` row on the dispatch result naming itself a
  declared-retry, and ``queue-status`` shows ``retryable`` / ``retries_used``
  per item with an exhaustion note when the budget is spent.

Why the consumer seat is ``queue-dispatch``: the plan's chain-dispatch edge
(§5/§6.4) already fires one dispatch tick at every run retirement, so putting
the producer at the top of that tick makes the loop event-driven — a run
fails, the retiring driver chains a dispatch, the dispatch enqueues the
declared retry and places + starts it in the same tick — with no daemon, no
new wake edge, and no write authority beyond the one the queue already has
(D10: the dispatcher is the queue's only actor). The gates are untouched: the
retry's fresh attempt still meets the greenlight / standing-consent gates at
the cluster boundary inside the lifecycle it starts (D1), so a declared
budget automates re-DISPATCH, never consent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from hpc_agent._kernel.contract.vocabulary import JournalStatus
from hpc_agent._wire.workflows.queue_dispatch import QueueDeclaredRetry
from hpc_agent.state.journal import is_resubmittable_terminal, load_run
from hpc_agent.state.queue_intake import (
    STATE_PLACED,
    append_intake_item,
    item_retryable,
    item_run_id,
    retries_used,
    retry_chains,
    retry_request_id,
    retry_tip,
)

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.state.run_record import RunRecord

__all__ = ["is_declared_retry_failure", "produce_declared_retries"]

_FAILED = str(JournalStatus.FAILED)

#: Arrival facts a retry item copies VERBATIM from the chain's tip. The
#: resolved identity (``spec`` / ``run_id`` / ``cmd_sha`` / ``run_name``) is
#: §10.S3's rule — never re-resolved; the rest keeps placement's inputs
#: (asks, campaign base) and the declaration itself flowing down the chain.
_COPIED_KEYS: tuple[str, ...] = (
    "spec",
    "spec_ref",
    "run_name",
    "run_id",
    "cmd_sha",
    "campaign_base",
    "resources",
    "retryable",
)


def is_declared_retry_failure(record: RunRecord | None) -> bool:
    """Is *record* a MECHANICAL failure terminal a declared budget may re-dispatch?

    THE one classifier for the retryable(n) leg — the dispatch producer and
    ``queue-status``'s disclosure both route through it, so the tick that
    retries and the digest that explains cannot disagree (R7). It is a test
    over recorded run-store facts only, never over log text or failure prose:
    the CLASS is declared at enqueue, and this predicate only answers whether
    the terminal is the mechanical kind the declaration covers.

    ``True`` requires ALL of:

    * status ``failed`` — the monitor reached a genuine failure verdict.
      ``complete`` is excluded by definition; ``abandoned`` is excluded by
      DECISION: it folds together confirmed kills (a human's halt),
      never-dispatched submit-window events, and lost tracking, and a class a
      machine cannot tell apart defaults to needs_human (the plan's default);
      a timed-out run stays ``in_flight`` and never reaches here;
    * not superseded — ``superseded_by`` marks a retirement with no failure
      in it, and re-dispatching one would resurrect a slot the occupancy
      predicate already retired;
    * no pending human decision (``pending_decision``) — a parked boundary is
      a human's open question, whatever the status says;
    * no kill ever requested (``kill_requested_at``) — a deliberate halt is a
      human's decision even when the terminal it produced reads ``failed``;
    * :func:`~hpc_agent.state.journal.is_resubmittable_terminal` — the one
      shipped definition of "a corpse a fresh attempt is minted over", which
      also folds in the escalation hold (a HELD failed run returns False
      there, so an anomaly/escalation verdict always parks). Routing through
      it guarantees the complement property the whole leg leans on: a run
      this predicate accepts is one ``queue-dispatch`` will MINT a fresh
      attempt over rather than adopt (``_ADOPTABLE_STATUSES``'s stated
      exclusion), so a declared retry can never no-op into an adopt of the
      corpse it is retrying.

    ``None`` is ``False``: no record means no failure to classify — the
    enqueue→dispatch window is the ledger half's fact, not a terminal.
    """
    if record is None:
        return False
    if str(record.status) != _FAILED:
        return False
    if getattr(record, "superseded_by", ""):
        return False
    if getattr(record, "pending_decision", None):
        return False
    if getattr(record, "kill_requested_at", None):
        return False
    return is_resubmittable_terminal(record)


def _journal_present(experiment_dir: Path) -> bool:
    """F46's non-creating probe — no journal namespace, nothing failed, no reads."""
    from hpc_agent.state.run_record import journal_root_if_exists

    return journal_root_if_exists(experiment_dir).exists()


def produce_declared_retries(
    experiment_dir: Path,
    items: list[dict[str, Any]],
) -> tuple[list[QueueDeclaredRetry], bool]:
    """Consume declared budgets over *items*: enqueue owed retries, name exhaustion.

    Returns ``(rows, appended)`` — one disclosure row per chain this tick
    decided about, and whether any ledger line was written (so the caller
    knows its earlier fold is stale). Runs at the TOP of a ``queue-dispatch``
    tick, before ``queue-advance`` reads the ledger, so a retry enqueued here
    is placed and dispatched by the same tick's ordinary machinery — no
    second placement path, no second submit path (D1 applied to retries).

    Cost when unused is the point: an experiment whose ledger declares no
    budget is one generator pass over an already-folded list — no journal
    read, no write, no rows. With budgets declared, the join is one
    ``load_run`` per chain whose tip is placed — bounded by declared-retry
    chains (active work), never by ledger history (§7).

    Per chain, in fold order (deterministic output for identical state):

    1. only the TIP speaks (:func:`~hpc_agent.state.queue_intake.retry_tip`) —
       an earlier attempt's failure was already answered by the retry that
       followed it;
    2. a ``queued`` tip is skipped silently: the chain's answer is already on
       the ledger awaiting placement/dispatch, and minting attempt k+2 before
       k+1 ran would burn budget on nothing;
    3. the tip's run must be a mechanical failure terminal
       (:func:`is_declared_retry_failure`) — everything else parks exactly as
       it does today;
    4. budget spent (``retries_used >= retryable``) → an ``exhausted`` row,
       no write: the item parks for a human, and the row names the exhaustion
       out loud;
    5. otherwise append the retry enqueue record under the derived id. A
       dedup hit (a racing or replayed tick) is a ``replayed`` row — the
       chain moved, this tick just was not the one that moved it. A
       compaction tombstone under the derived id is treated the same way and
       written by nobody: a tombstoned retry id means the chain settled and
       was groomed, so nothing is owed.

    The accepted imprecision, stated rather than hidden: a retry whose
    dispatch appended its placement (durable-first) and then crashed before
    the start leaves a PLACED tip over the old corpse, and the next tick
    charges attempt k+1 for an attempt k that never ran. The budget errs in
    the conservative direction only — fewer real submissions than declared,
    never more — and ``queue-dispatch --item-ids`` remains the shipped
    recovery for the stranded placement itself.
    """
    declared = [item for item in items if item_retryable(item) is not None]
    if not declared:
        return [], False
    if not _journal_present(experiment_dir):
        return [], False

    rows: list[QueueDeclaredRetry] = []
    appended = False
    chains = retry_chains(items)
    # Only chains that carry a declaration are consulted at all; iteration
    # follows fold order via the chains dict (insertion-ordered by first
    # member), so two ticks over one ledger emit rows in one order.
    for root, chain in chains.items():
        tip = retry_tip(chain)
        if tip is None:
            continue
        budget = item_retryable(tip)
        if budget is None:
            continue
        if tip.get("state") != STATE_PLACED:
            # A queued tip IS the pending response — it has not been
            # dispatched, so its chain owes nothing new this tick.
            continue
        run_id = item_run_id(tip)
        if run_id is None:
            continue
        if not is_declared_retry_failure(load_run(experiment_dir, run_id)):
            continue

        used = retries_used(chain)
        if used >= budget:
            rows.append(
                QueueDeclaredRetry(
                    root_item_id=root,
                    item_id=None,
                    attempt=used,
                    retryable=budget,
                    run_id=run_id,
                    outcome="exhausted",
                    reason=(
                        f"declared-retry budget EXHAUSTED for item {root!r}: run "
                        f"{run_id} failed and all {used} of retryable({budget}) "
                        "declared retries are spent — the item now parks for a "
                        "human. The budget was declared at enqueue and consumed "
                        "by kernel code; nothing here judged the failure."
                    ),
                )
            )
            continue

        attempt = used + 1
        retry_id = retry_request_id(root, attempt)
        payload: dict[str, Any] = {key: tip.get(key) for key in _COPIED_KEYS}
        payload.update(
            {
                # R5: the chain's cluster is already decided — the resolved
                # spec targets it and dispatch refuses a mismatch — so the
                # retry pins the tip's placed cluster rather than letting
                # policy re-choose a cluster the spec cannot follow.
                "cluster_pin": tip.get("cluster") or tip.get("cluster_pin"),
                "retry_root": root,
                "retry_attempt": attempt,
                "retry_of": tip.get("item_id"),
                "retry_reason": (
                    f"declared-retry: run {run_id} reached the mechanical failure "
                    f"terminal 'failed'; attempt {attempt} of the retryable({budget}) "
                    f"budget declared at enqueue on item {root!r}"
                ),
            }
        )
        replayed = append_intake_item(experiment_dir, record=payload, request_id=retry_id)
        # None means a line was written. A dedup hit and a compaction
        # tombstone (`is_compaction_tombstone(replayed)`) both come back as a
        # record and read the same way here: the chain already moved (or
        # settled and was groomed), and this tick wrote nothing.
        fresh = replayed is None
        appended = appended or fresh
        outcome: Literal["enqueued", "replayed"] = "enqueued" if fresh else "replayed"
        rows.append(
            QueueDeclaredRetry(
                root_item_id=root,
                item_id=retry_id,
                attempt=attempt,
                retryable=budget,
                run_id=run_id,
                outcome=outcome,
                reason=(
                    f"declared-retry: re-enqueued failed run {run_id} as item "
                    f"{retry_id!r} (attempt {attempt} of retryable({budget}), "
                    "declared at enqueue) reusing the recorded resolved identity "
                    "verbatim — never re-resolved. It is placed and dispatched by "
                    "the normal machinery, and its fresh attempt still meets every "
                    "cluster-boundary gate (D1)."
                    + ("" if fresh else " A racing or replayed tick already wrote it.")
                ),
            )
        )
    return rows, appended
