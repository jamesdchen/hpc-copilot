"""The run-queue INTAKE ledger — arrival facts, and deliberately nothing else.

One append-only JSONL ledger per experiment,

    <experiment_dir>/.hpc/queue/intake.jsonl

written through :func:`hpc_agent.infra.io.append_jsonl_line` (the canonical
flock-append discipline every ledger in the package uses — append-only,
whole-line-atomic, crash-durable, with in-lock replay dedup).

**R1 — the ledger is an INDEX, not a second journal**
(``docs/plans/run-queue-placement-2026-07-28.md`` §7 R1, §8 S10). Intake is
the ONE new store the run queue introduces, and it records exactly the one
fact no existing store holds: *this slot is spoken for, and here is what was
asked for*. Concretely, a record carries only

* **arrival facts** — ``item_id`` / ``request_id`` / ``enqueued_at``, the
  resolve spec (or a pointer to it), the typed resource asks, an optional
  explicit cluster pin, an optional campaign base, and (once resolved) the
  COMPUTED ``run_id`` (§10.S2: run ids are derived, never minted); and
* **lifecycle in ``{queued, placed}``** and nothing wider.

Resolved identity on the enqueue record (Phase 2 / §10.S3)
----------------------------------------------------------
An item whose caller ALREADY resolved it may arrive carrying ``run_id`` and
``cmd_sha`` (:func:`item_run_id` / :func:`item_cmd_sha`). That is the
campaign-refill path (§10.S3 D5): a refill slot must consume the optuna
sidecar index exactly once, so it resolves FIRST and enqueues the resolved
identity, and the enqueue is what closes the crash window between "this trial
was chosen" and "this trial is a run".

Those are still arrival facts, not lifecycle: ``run_id`` is COMPUTED
(``"<run_name>-<cmd_sha[:8]>"``, a pure query), so recording it asserts only
*which run this item will be*, never that a run exists. Carrying it is a
CORRECTNESS precondition rather than a convenience — ``state/queue_occupancy``
collapses an item and the run it becomes onto one slot only when the item
knows its ``run_id``; without it the enqueue→dispatch handoff counts one
committed slot twice and every refill tick inside that window under-refills.

Neither key is pinned by the fold, so a later placement record's optional
``run_id`` overlays cleanly (rule 3) — an item that arrives unresolved and is
resolved later reads the same way as one that arrived resolved.

``dispatched`` / ``parked`` / ``in-flight`` / ``terminal`` are **PROJECTIONS**
computed at read time over stores that already exist and are already durable
(``state/index.py`` RunRecords, ``state/journal.py`` pending-decision markers,
``state/decision_journal.py`` committed greenlights). S10 is the finding this
closes: an intake ``dispatched`` mirror would duplicate the shipped
``submitting`` RunRecord organ, no write spans both stores atomically, and
every crash order either strands or duplicates. Never copy a run status into
a record here. The submit-once ``submitting`` record is the handoff fact.

The fold
--------
The ledger is append-only: a state transition is a NEW record keyed by
``item_id``, never a rewrite of an earlier line. The CURRENT state of an item
is therefore the left-to-right fold over its records
(:func:`fold_intake_records`):

1. Records are read in FILE ORDER — the flock serializes appenders, so file
   order is the durable total order of what happened.
2. The FIRST record for an ``item_id`` must be the enqueue record
   (``kind == "enqueue"``); it establishes the item's arrival facts. A
   transition record naming an ``item_id`` with no prior enqueue is skipped:
   its arrival facts are unrecoverable, and inventing them would make the
   ledger author history it never saw.
3. Every later record for that ``item_id`` OVERLAYS its own keys onto the
   fold — last writer wins, per key. So a placement record carrying
   ``{state: "placed", cluster: ..., campaign_id: ...}`` leaves the resolve
   spec and the resource asks untouched.
4. Four keys are PINNED to the enqueue record and never overlaid:
   ``item_id`` (identity), ``request_id`` (the item's minting token — later
   records carry their OWN append token, which is not the item's), ``kind``,
   and ``enqueued_at`` (arrival is a fact about arrival, not about the last
   write). ``updated_at`` exposes the last record's ``ts`` instead.
5. A record whose ``state`` is outside ``{queued, placed}`` is skipped rather
   than folded — R1 is enforced by the reader, not merely documented, so a
   future writer that tries to smuggle ``dispatched`` in cannot change what a
   projection sees.

Replay dedup (R8, §10.S2)
-------------------------
Every append passes ``dedup_key=("request_id", request_id)``. A relayed CLI
turn — the workflow engine replays a completed call verbatim from cache — must
not enqueue a second item, and the guard runs INSIDE the flock so it is also
race-free against a concurrent duplicate. ``append_jsonl_line`` returns the
PRE-EXISTING record on a replay hit and ``None`` when a line was written; both
appenders below preserve that contract by returning the replayed record.

``item_id`` and ``request_id`` are distinct on purpose. §10.S2 collapses them
at ENQUEUE (the item's id IS its minting request id), but a later transition
needs its own append token or its dedup probe would match the enqueue record
and silently no-op the transition. So: ``item_id`` is the item's identity for
the fold; ``request_id`` is the idempotency token of ONE append.

A transition's token is DERIVED, not random (:func:`placement_request_id`).
Two dispatchers racing one item — or one dispatcher whose turn is replayed —
must leave ONE placement record on the ledger; a fresh uuid4 per attempt would
dedup against nothing and append a second placement line every time, which the
fold would happily overlay and ``record_count`` would then report as a torn
item. Deterministic per (item, transition) is what makes the transition as
replay-safe as the arrival it follows.

Compaction (S12, and the reason R1 permits it)
----------------------------------------------
The ledger is append-only per WRITE, not per LIFETIME. Because it is an INDEX
rather than a journal, a record whose item has SETTLED — the run it became
reached a terminal status, or was superseded — answers no question any reader
still asks, while still costing every reader a line. §7's relaunch-cheapness
invariant makes that unacceptable rather than untidy: pass-startup work must
scale with ACTIVE items, never with ledger history.

:func:`compact_intake_ledger` rewrites the file without those items' records,
under the appenders' own flock, atomically, keeping any line it could not parse
(evidence is never compacted away). It is called from a WRITE authority
(``queue-dispatch``, the queue's only actor) and never from a read: a query that
groomed the store it reports on would be the F46 error one layer up.
:func:`compaction_watermark` records what was removed, so the shrink is
auditable rather than a file that mysteriously got shorter.

Reading is tolerant — a torn tail, a foreign line, or a shape the fold cannot
place is SKIPPED, never fatal (the ``state/devx_tags.py`` and decision-journal
posture). A queue read is on the "what needs me" path; a single bad byte must
never wedge it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent._kernel.contract.layout import RepoLayout
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.infra.time import utcnow_iso

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_log = logging.getLogger(__name__)

__all__ = [
    "INTAKE_STATES",
    "STATE_PLACED",
    "STATE_QUEUED",
    "append_intake_item",
    "append_intake_placement",
    "compact_intake_ledger",
    "compaction_watermark",
    "compaction_watermark_path",
    "find_intake_item",
    "fold_intake_records",
    "intake_path",
    "intake_path_if_exists",
    "item_cmd_sha",
    "item_run_id",
    "items_in_states",
    "placement_request_id",
    "read_intake_items",
    "read_intake_records",
    "read_intake_records_counted",
]

STATE_QUEUED = "queued"
STATE_PLACED = "placed"

#: The COMPLETE lifecycle vocabulary intake may store (R1). Anything else is a
#: projection over the run stores and has no business on this ledger.
INTAKE_STATES: tuple[str, ...] = (STATE_QUEUED, STATE_PLACED)

#: Keys the fold pins to the enqueue record; a later record never overlays them
#: (see the module docstring's fold rule 4).
_PINNED_KEYS: frozenset[str] = frozenset({"item_id", "request_id", "kind", "enqueued_at"})

_KIND_ENQUEUE = "enqueue"
_KIND_PLACEMENT = "placement"

#: Suffix appended to an ``item_id`` to derive the PLACEMENT transition's append
#: token. A ``.`` because ``item_id`` (wire alias ``QueueItemId``) is
#: ``^[A-Za-z0-9._\-]+$`` and the derived token is quoted into briefs and
#: compared as an ordinary id — a ``:`` or ``/`` would leave the charset the
#: whole queue wire surface validates against.
_PLACEMENT_TOKEN_SUFFIX = ".placed"


def intake_path(experiment_dir: Path) -> Path:
    """``.hpc/queue/intake.jsonl`` for *experiment_dir* — the WRITE-side accessor.

    Goes through :class:`RepoLayout`, whose ``.hpc`` property materializes the
    directory (and its ``.gitignore``) on access. Use this from an appender.
    Readers must use :func:`intake_path_if_exists` instead.
    """
    return RepoLayout(experiment_dir).hpc / "queue" / "intake.jsonl"


def intake_path_if_exists(experiment_dir: Path) -> Path:
    """The same path, computed WITHOUT materializing anything — the READ side.

    F46's rule: a read must never scaffold. ``RepoLayout.hpc`` mkdirs and writes
    ``.hpc/.gitignore`` on first access, so routing a pure query (``queue-status``,
    ``queue-advance``) through :func:`intake_path` would have those verbs create
    the very tree they are reporting on — the same class of bug the non-creating
    ``journal_root_if_exists`` probe was added to close. Resolution matches
    ``RepoLayout.root`` so writers and readers from different cwds agree on the
    file. The returned path may not exist.
    """
    return Path(experiment_dir).resolve() / ".hpc" / "queue" / "intake.jsonl"


def append_intake_item(
    experiment_dir: Path,
    *,
    record: dict[str, Any],
    request_id: str,
) -> dict[str, Any] | None:
    """Append one ENQUEUE record; return ``None`` on write, the original on replay.

    *record* carries the caller's arrival facts (resolve spec / spec pointer,
    resource asks, cluster pin, campaign base, and — when the caller resolved
    the item before enqueueing it, the §10.S3 refill path — the computed
    ``run_id`` and ``cmd_sha``). This seam stamps the four fields the fold owns
    — ``kind``, ``item_id``, ``request_id``, ``enqueued_at``/``ts`` — and forces
    ``state`` to ``"queued"``: an item cannot ARRIVE placed, and letting a
    caller assert otherwise would put a placement on the ledger that
    ``queue-advance`` never decided (R3/R5). The identity keys are deliberately
    NOT forced or validated here — they are ordinary arrival facts, the wire
    model owns their shape, and the readers below treat an absent or
    ill-shaped one as simply unknown.

    ``item_id`` is set to *request_id* (§10.S2: the client's minting token IS
    the item's identity). The append passes ``dedup_key=("request_id", …)``, so
    a replayed relay writes nothing and the PRE-EXISTING record comes back —
    the caller must echo that record rather than mint a second item (R8).
    """
    if not request_id or not request_id.strip():
        raise ValueError("request_id must be a non-empty string (it is the dedup key)")
    now = utcnow_iso()
    payload: dict[str, Any] = dict(record)
    payload.update(
        {
            "kind": _KIND_ENQUEUE,
            "item_id": request_id,
            "request_id": request_id,
            "state": STATE_QUEUED,
            "enqueued_at": now,
            "ts": now,
        }
    )
    return append_jsonl_line(
        intake_path(experiment_dir),
        payload,
        dedup_key=("request_id", request_id),
    )


def placement_request_id(item_id: str) -> str:
    """The DERIVED append token for *item_id*'s placement transition.

    ``<item_id>.placed`` — deterministic, so the transition inherits the same
    replay safety the arrival has (module docstring, "Replay dedup"). A
    dispatcher that crashes after appending and is re-run, and two dispatchers
    racing the same item, all compute this same token; the in-flock dedup probe
    then finds the existing placement and no second line is written.

    Distinct from *item_id* by construction, which
    :func:`append_intake_placement` requires — a token equal to the item's own
    would dedup against the ENQUEUE record and drop the transition silently.
    """
    if not item_id or not item_id.strip():
        raise ValueError("item_id must be a non-empty string")
    return f"{item_id}{_PLACEMENT_TOKEN_SUFFIX}"


def append_intake_placement(
    experiment_dir: Path,
    *,
    item_id: str,
    request_id: str,
    cluster: str,
    campaign_id: str,
    reason: str,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    """Append one PLACEMENT transition for *item_id*; ``None`` on write, original on replay.

    The transition is a NEW record, never a rewrite — the fold turns the pair
    (enqueue, placement) into one item in state ``placed``. *reason* is the
    disclosed WHY the placement chose this cluster (R4: no held-back or placed
    item ever travels without its reason), carried on the durable record so the
    brief that quotes it and the ledger that justified it cannot drift.

    *request_id* is THIS append's idempotency token and must differ from the
    item's — a transition reusing ``item_id`` as its dedup key would match the
    enqueue record and silently no-op.

    ``queue-dispatch`` is the ONE writer (``queue-advance`` is pure, R3): it
    records the cluster it is about to start the item's lifecycle on, and the
    item leaves ``queue-advance``'s scope the moment it does — advance reads
    only ``queued`` items, so the placement append is the handoff. Pass
    :func:`placement_request_id` as *request_id* unless you have a reason not
    to; a derived token is what makes a retried or raced dispatch leave one
    placement record instead of one per attempt.
    """
    if not item_id or not item_id.strip():
        raise ValueError("item_id must be a non-empty string")
    if not request_id or not request_id.strip():
        raise ValueError("request_id must be a non-empty string (it is the dedup key)")
    if request_id == item_id:
        raise ValueError(
            "a transition's request_id must differ from the item_id it transitions "
            "(reusing it would dedup against the enqueue record and drop the transition)"
        )
    payload: dict[str, Any] = {
        "kind": _KIND_PLACEMENT,
        "item_id": item_id,
        "request_id": request_id,
        "state": STATE_PLACED,
        "cluster": cluster,
        "campaign_id": campaign_id,
        "reason": reason,
        "ts": utcnow_iso(),
    }
    if run_id is not None:
        payload["run_id"] = run_id
    return append_jsonl_line(
        intake_path(experiment_dir),
        payload,
        dedup_key=("request_id", request_id),
    )


def read_intake_records_counted(experiment_dir: Path) -> tuple[list[dict[str, Any]], int]:
    """Records in FILE ORDER, paired with how many lines were DROPPED reading them.

    The tolerant read and the count of what tolerance cost, from one pass. A
    projection that reports ``skipped_records`` cannot compute the reader's half
    of that number after the fact — the dropped lines are gone by the time it
    sees the list — so the reader is the only place the two can be kept honest
    together. Dropped means: a line that is not JSON (a torn tail), or JSON that
    is not an object (a foreign line). Blank lines are NOT counted: whitespace
    is not lost data.

    Tolerance is applied PER LINE, never to the whole file. Decoding is
    ``errors="replace"`` for exactly that reason: one non-UTF-8 byte — a torn
    multi-byte write, a foreign writer — would otherwise fail the whole-file
    decode and make a ledger holding N queued items read as ``([], 0)``, i.e.
    indistinguishable from a virgin experiment, on both "what needs me"
    surfaces at once. Replaced bytes turn that one line into something
    ``json.loads`` rejects, so it lands in *dropped* and the other N-1 records
    survive.

    A file that EXISTS but cannot be opened at all (permission denied, EIO)
    reports ``([], 1)`` and logs a warning: nothing could be read, but the file
    is there, so something IS known to have been lost and the caller must
    disclose that rather than report an empty queue. ``(_, 0)`` is reserved for
    "there was nothing to lose". Probed through the non-creating path accessor
    so the read leaves no trace.
    """
    import json

    path = intake_path_if_exists(experiment_dir)
    if not path.is_file():
        return [], 0
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        _log.warning("queue_intake: skipping unreadable %s (%s)", path, exc)
        return [], 1
    out: list[dict[str, Any]] = []
    dropped = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            dropped += 1
            continue
        if isinstance(rec, dict):
            out.append(rec)
        else:
            dropped += 1
    return out, dropped


def read_intake_records(experiment_dir: Path) -> list[dict[str, Any]]:
    """Every parseable record in the ledger, in FILE ORDER (the raw journal view).

    A torn tail, a blank line, or a foreign line is skipped, not fatal. Returns
    ``[]`` for an experiment that has never enqueued anything. Thin wrapper on
    :func:`read_intake_records_counted` so the records and the drop count can
    never come from two different scans of the file.
    """
    return read_intake_records_counted(experiment_dir)[0]


def fold_intake_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold raw records into current items, in ARRIVAL order. Pure — no I/O.

    Implements the five fold rules in the module docstring. Split out from
    :func:`read_intake_items` so the fold is unit-testable against a literal
    record list (R3's determinism argument applies to the substrate too): a
    projection bug should be reproducible without a filesystem.

    Each returned item is the overlaid record plus ``updated_at`` (the ``ts`` of
    the last record folded into it) and ``record_count`` (how many records back
    it — a torn ledger is visible rather than silently smoothed over).
    """
    items: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        item_id = rec.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            continue
        state = rec.get("state")
        # Rule 5: R1 is enforced here, not merely documented.
        if state not in INTAKE_STATES:
            continue
        known = items.get(item_id)
        if known is None:
            # Rule 2: only an enqueue record may open an item.
            if rec.get("kind") != _KIND_ENQUEUE:
                continue
            item = dict(rec)
            item["updated_at"] = rec.get("ts")
            item["record_count"] = 1
            items[item_id] = item
            order.append(item_id)
            continue
        # Rules 3 + 4: overlay everything except the pinned arrival keys.
        for key, value in rec.items():
            if key in _PINNED_KEYS:
                continue
            known[key] = value
        known["updated_at"] = rec.get("ts", known.get("updated_at"))
        known["record_count"] = int(known.get("record_count", 1)) + 1
    return [items[item_id] for item_id in order]


def read_intake_items(experiment_dir: Path) -> list[dict[str, Any]]:
    """The CURRENT items on the ledger, in arrival order — read + fold.

    This is what every projection consumes: one dict per item, ``state`` in
    ``{queued, placed}``, arrival facts intact. It is emphatically NOT a run
    status view — join to ``state/index.py`` for that (R1).
    """
    return fold_intake_records(read_intake_records(experiment_dir))


def items_in_states(
    items: Sequence[dict[str, Any]],
    states: Iterable[str] = INTAKE_STATES,
) -> list[dict[str, Any]]:
    """Filter folded *items* to those whose ``state`` is in *states*.

    Trivial, and deliberately shared: "queued or placed" is the exact set R9's
    occupancy predicate means by *a slot this ledger has spoken for*, and the
    membership test belongs next to the vocabulary that defines it rather than
    re-inlined at each call site.
    """
    wanted = frozenset(states)
    return [item for item in items if item.get("state") in wanted]


def _nonempty_str(item: dict[str, Any], key: str) -> str | None:
    """*item*[*key*] when it is a non-empty string, else ``None``.

    The ledger is read tolerantly (module docstring): a key may be absent, may
    be ``None`` because the enqueue spec left it unset, or may be a shape a
    foreign writer put there. All three mean the same thing to a reader — the
    fact is not known — and collapsing them here keeps every consumer from
    re-inlining the same ``isinstance`` dance and drifting on one of the cases.
    """
    value = item.get(key)
    return value if isinstance(value, str) and value else None


def item_run_id(item: dict[str, Any]) -> str | None:
    """The COMPUTED run id a folded *item* carries, or ``None`` when unknown.

    Set by an enqueue that resolved first (§10.S3 D5) or overlaid by a
    placement record that learned it. THE accessor for that fact: the occupancy
    predicate collapses an item onto its run by this value, ``queue-advance``
    discloses it, ``queue-status`` joins the run stores on it, and a dispatcher
    claims on it — four readers of one key, which is three too many to each
    decide for themselves what a non-string ``run_id`` means.
    """
    return _nonempty_str(item, "run_id")


def item_cmd_sha(item: dict[str, Any]) -> str | None:
    """The resolved parameter identity a folded *item* carries, or ``None``.

    The pre-image half of ``run_id = "<run_name>-<cmd_sha[:8]>"``. Recorded
    because the truncation is lossy: a dispatcher that adopts an existing run
    can state which cmd_sha the ledger committed to, rather than inferring
    agreement from eight hex characters.
    """
    return _nonempty_str(item, "cmd_sha")


def compaction_watermark_path(experiment_dir: Path) -> Path:
    """``.hpc/queue/intake.compaction.json`` — the ledger's compaction watermark.

    A SINGLE document with cumulative counters, never a second ledger: a
    per-compaction journal would be one more append-only file growing with
    history, which is the very cost compaction exists to remove. Computed
    without materializing anything, the same non-creating rule
    :func:`intake_path_if_exists` follows (F46).
    """
    return Path(experiment_dir).resolve() / ".hpc" / "queue" / "intake.compaction.json"


def compaction_watermark(experiment_dir: Path) -> dict[str, Any]:
    """The recorded compaction watermark, or ``{}`` when nothing was ever compacted.

    Tolerant like every other read here: an unreadable or non-object watermark
    reads as absent rather than raising. It is bookkeeping ABOUT the ledger, so
    losing it must never make the ledger itself unreadable.
    """
    import json

    path = compaction_watermark_path(experiment_dir)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def compact_intake_ledger(
    experiment_dir: Path,
    *,
    drop_item_ids: Iterable[str],
    now: str | None = None,
) -> dict[str, Any]:
    """Rewrite the ledger without *drop_item_ids*' records. Returns what it did.

    **S12's compaction watermark** (§8 S12, §7's relaunch-cheapness invariant).
    The intake ledger is an INDEX, not a journal (R1) — its job is to answer
    *which slots are spoken for right now*, and a record whose item is settled
    answers nothing while still costing every reader a line. Left alone the file
    grows with HISTORY, and pass-startup cost growing with history is precisely
    what §7 forbids: "a drain pass that starts, finds nothing drivable, and
    returns must cost near-zero".

    WHO decides what is droppable is deliberately NOT here.
    :func:`hpc_agent.state.queue_occupancy.retired_item_ids` owns that judgement
    because it already owns :func:`~hpc_agent.state.queue_occupancy.run_occupies`
    — the ONE test for "has the run this item became retired?" (R9). This
    function is pure file mechanics over a set of ids handed to it, so there is
    no second, subtly different notion of settled.

    Conduct, and why each rule is what it is:

    * **Under the ledger's OWN flock** (``<intake.jsonl>.lock``, the same
      sentinel :func:`hpc_agent.infra.io.append_jsonl_line` takes), so a
      concurrent enqueue or placement can neither be lost by the rewrite nor
      read a half-written file. This is the only writer in the package that does
      not merely append, which is exactly why it must share the appenders' lock.
    * **Atomic replace**, so a crash leaves the previous ledger intact rather
      than a truncated one.
    * **A line the reader could not parse is KEPT verbatim.** A torn tail or a
      foreign line is data this code did not author and cannot classify;
      dropping it would silently shrink ``queue-status``'s ``skipped_records``
      and erase the only evidence that something went wrong. Compaction removes
      ANSWERED questions, never unanswerable ones.
    * **No-op when nothing matches** — the file is not rewritten and its mtime
      is not touched, so a healthy drain tick over an already-compact ledger
      costs one read.

    **R8 (replay dedup), stated honestly.** ``append_jsonl_line`` dedups an
    enqueue by scanning the file for the record's ``request_id``; dropping an
    item's records therefore drops its dedup entry too. That is safe for exactly
    the reason it is scoped this way: a compacted item's run has RETIRED, so it
    is not inside any enqueue→dispatch replay window — the window R8 protects is
    the one between a relay's first call and its cached replay, and a run that
    reached a terminal status has long since left it. Every STILL-LIVE request
    keeps every one of its records, so dedup for live items is untouched. The
    residual case (a very old cached relay replaying an enqueue for a long-dead
    run) re-enqueues an item that resolves to the SAME computed run id and is
    ADOPTED by ``queue-dispatch`` against the surviving RunRecord — which is why
    ledger compaction and journal pruning keep different retention (the prune
    keeps the newest terminal records).

    *now* overrides the watermark stamp for deterministic tests.
    """
    import json

    from hpc_agent.infra.io import advisory_flock, atomic_write_json, atomic_write_text

    drop = {item_id for item_id in drop_item_ids if isinstance(item_id, str) and item_id}
    path = intake_path_if_exists(experiment_dir)
    report: dict[str, Any] = {
        "path": str(path),
        "compacted": False,
        "dropped_items": 0,
        "dropped_records": 0,
        "kept_records": 0,
    }
    if not drop or not path.is_file():
        return report

    lock_path = path.with_suffix(path.suffix + ".lock")
    with advisory_flock(lock_path, timeout_sec=120.0):
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            _log.warning("queue_intake: cannot compact unreadable %s (%s)", path, exc)
            return report
        kept: list[str] = []
        dropped_ids: set[str] = set()
        dropped_records = 0
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)  # unparseable: evidence, not history
                continue
            item_id = rec.get("item_id") if isinstance(rec, dict) else None
            if isinstance(item_id, str) and item_id in drop:
                dropped_ids.add(item_id)
                dropped_records += 1
                continue
            kept.append(line)
        report["kept_records"] = len(kept)
        if not dropped_records:
            return report
        atomic_write_text(path, "".join(f"{line}\n" for line in kept))
        report["compacted"] = True
        report["dropped_items"] = len(dropped_ids)
        report["dropped_records"] = dropped_records

        prior = compaction_watermark(experiment_dir)
        atomic_write_json(
            compaction_watermark_path(experiment_dir),
            {
                "last_compacted_at": now or utcnow_iso(),
                "compactions": int(prior.get("compactions") or 0) + 1,
                "items_compacted": int(prior.get("items_compacted") or 0) + len(dropped_ids),
                "records_dropped": int(prior.get("records_dropped") or 0) + dropped_records,
                "records_kept": len(kept),
            },
        )
    return report


def find_intake_item(
    items: Sequence[dict[str, Any]],
    item_id: str,
) -> dict[str, Any] | None:
    """The folded item with this *item_id*, or ``None`` if the ledger has none.

    Pure lookup over an ALREADY-folded list, deliberately not a reader: every
    caller that wants one item also wants the ledger-wide numbers from the same
    scan (the queued depth it echoes, the occupancy it reports), and a
    convenience reader here would invite a second, separately-timed read of the
    file behind those two answers.
    """
    return next((item for item in items if item.get("item_id") == item_id), None)
