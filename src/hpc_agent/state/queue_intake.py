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

The watermark also carries the TOMBSTONES — the ``request_id`` of every record
compaction removed — which is what keeps R8's replay dedup true across a
compaction (see :func:`compaction_tombstones`). Without them, deleting an item's
records deletes its dedup entry, and a replayed relay turn re-enqueues a
COMPLETED run.

Reading is tolerant — a torn tail, a foreign line, or a shape the fold cannot
place is SKIPPED, never fatal (the ``state/devx_tags.py`` and decision-journal
posture). A queue read is on the "what needs me" path; a single bad byte must
never wedge it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from hpc_agent._kernel.contract.layout import RepoLayout
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.infra.time import utcnow_iso

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

_log = logging.getLogger(__name__)

__all__ = [
    "INTAKE_STATES",
    "STATE_PLACED",
    "STATE_QUEUED",
    "TOMBSTONE_CAP",
    "append_intake_item",
    "append_intake_placement",
    "compact_intake_ledger",
    "compaction_tombstones",
    "compaction_watermark",
    "compaction_watermark_path",
    "find_intake_item",
    "fold_intake_records",
    "intake_path",
    "intake_path_if_exists",
    "is_compaction_tombstone",
    "item_cmd_sha",
    "item_run_id",
    "items_in_states",
    "placement_request_id",
    "read_intake_items",
    "read_intake_records",
    "read_intake_records_counted",
]

#: Typed as ``Literal`` rather than ``str`` so a caller handing one of them to the
#: wire's ``QueueItemState`` needs no ``type: ignore``: these two ARE that alias's
#: members, and spelling that in the type is what lets the checker agree.
STATE_QUEUED: Literal["queued"] = "queued"
STATE_PLACED: Literal["placed"] = "placed"

#: The COMPLETE lifecycle vocabulary intake may store (R1). Anything else is a
#: projection over the run stores and has no business on this ledger.
INTAKE_STATES: tuple[str, ...] = (STATE_QUEUED, STATE_PLACED)

#: Keys the fold pins to the enqueue record; a later record never overlays them
#: (see the module docstring's fold rule 4).
_PINNED_KEYS: frozenset[str] = frozenset({"item_id", "request_id", "kind", "enqueued_at"})

_KIND_ENQUEUE = "enqueue"
_KIND_PLACEMENT = "placement"

#: The ``kind`` of the SYNTHETIC record :func:`append_intake_item` returns when a
#: request_id is tombstoned. Deliberately outside ``{enqueue, placement}``: it is
#: not a ledger record and is never written to the file — it is the shape of an
#: answer, and giving it a kind of its own is what lets
#: :func:`is_compaction_tombstone` classify it without a caller guessing.
_KIND_COMPACTED = "compacted"

#: Suffix appended to an ``item_id`` to derive the PLACEMENT transition's append
#: token. A ``.`` because ``item_id`` (wire alias ``QueueItemId``) is
#: ``^[A-Za-z0-9._\-]+$`` and the derived token is quoted into briefs and
#: compared as an ordinary id — a ``:`` or ``/`` would leave the charset the
#: whole queue wire surface validates against.
_PLACEMENT_TOKEN_SUFFIX = ".placed"

#: Watermark key holding the compaction TOMBSTONES — see
#: :func:`compaction_tombstones`. Ordered oldest-first so the cap can be applied
#: by truncating the front.
_TOMBSTONE_KEY = "tombstoned_request_ids"

#: How many compaction tombstones the watermark retains, newest-first.
#:
#: Argued against the replay window it has to cover, because a cap that is too
#: small silently reinstates the double-submission it exists to prevent. The
#: window R8 protects is between a relay's first call and the workflow engine's
#: cached replay of that same turn — bounded by one drain pass, minutes to hours.
#: Tombstones accrue only for GENUINELY-settled requests (the janitor compacts a
#: ``placed`` item whose run is complete or superseded and which nothing will
#: re-attempt — ``state/queue_occupancy.item_is_history``), so the accrual rate
#: is at most one per completed run. 2000 therefore covers a fleet completing a
#: run every 30 seconds for a continuous 16 hours before the oldest tombstone
#: falls off, which is orders of magnitude past any single relay's cache
#: lifetime. The cost of the cap is one bounded JSON list — read once per enqueue
#: on a path that already reads the whole ledger — instead of a second file that
#: grows with history, which is the very cost compaction exists to remove.
TOMBSTONE_CAP = 2000


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

    **A COMPACTED request_id dedups too.** The in-flock probe can only see
    records the file still holds, so once compaction removes a settled item its
    dedup entry goes with it and a late replay would re-enqueue a run that
    already finished (see :func:`compaction_tombstones` for the end-to-end
    harm). So the tombstone set is consulted first, and a hit returns a
    SYNTHETIC record — ``kind`` :data:`_KIND_COMPACTED`, classifiable through
    :func:`is_compaction_tombstone` — carrying the reason, with no line written.
    It is not the original record because the original is gone; saying so
    explicitly is the honest answer, and inventing a plausible-looking ledger row
    would be worse than the duplicate.

    That probe is outside the flock, unlike the record-scan one, and the window
    that leaves is stated rather than papered over: a compaction that tombstones
    this exact request_id between the probe and the append will let one line
    through. It is a strictly smaller window than the one this closes (it needs a
    replay to land inside a single compaction's critical section, rather than any
    time after it), and closing it would mean holding the ledger lock across a
    watermark read — nesting the same non-reentrant flock the append itself
    takes.
    """
    if not request_id or not request_id.strip():
        raise ValueError("request_id must be a non-empty string (it is the dedup key)")
    if request_id in compaction_tombstones(experiment_dir):
        return {
            "kind": _KIND_COMPACTED,
            "item_id": request_id,
            "request_id": request_id,
            "reason": (
                f"request_id {request_id!r} was already enqueued, dispatched, and its "
                "run has settled; the item's records were compacted off the intake "
                "ledger and its request_id is tombstoned. Nothing was written — "
                "re-enqueueing it would resubmit a finished run (R8)."
            ),
        }
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


def compaction_tombstones(experiment_dir: Path) -> frozenset[str]:
    """The ``request_id``s compaction has removed from this ledger (R8's memory).

    ``append_jsonl_line`` dedups by scanning the FILE for the record's
    ``request_id``, so compacting an item's records deletes its dedup entry with
    them. The shipped code argued that away as safe because a compacted item's
    run had retired; the argument does not hold once the journal prune removes
    the RunRecord too (``KEEP_TERMINAL_RUNS`` is the only thing that was
    protecting it). Past that point a replayed relay turn dedup-MISSES,
    re-enqueues, re-dispatches, finds no record to adopt, and REALLY resubmits a
    completed run — polluting the optuna prior with a duplicate trial. That is
    the whole reason this set exists.

    It lives in the existing compaction watermark rather than a new store: it is
    bookkeeping ABOUT the ledger, it is written by the one seat that already
    rewrites the ledger, and a second append-only file would be exactly the
    history-shaped cost compaction exists to remove. Bounded by
    :data:`TOMBSTONE_CAP`.

    Tolerant like every other read here — an unreadable or ill-shaped watermark
    reads as "no tombstones", because failing an enqueue over lost bookkeeping
    would be a worse outcome than the duplicate it guards against.
    """
    raw = compaction_watermark(experiment_dir).get(_TOMBSTONE_KEY)
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(value for value in raw if isinstance(value, str) and value)


def is_compaction_tombstone(record: dict[str, Any] | None) -> bool:
    """True when *record* is the synthetic answer :func:`append_intake_item` gives a replay.

    THE test for that outcome, so no caller re-inlines a string comparison
    against the synthetic ``kind``. A tombstone answer is a SUCCESS — the
    request was already honoured and its item has since settled — but it is not
    a ledger row, so a caller that echoes ``record`` verbatim must be able to
    tell the two apart before it goes looking for the item on the ledger.
    """
    return isinstance(record, dict) and record.get("kind") == _KIND_COMPACTED


def compact_intake_ledger(
    experiment_dir: Path,
    *,
    drop_item_ids: Iterable[str],
    witnessed_records: Mapping[str, int] | None = None,
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
    :func:`hpc_agent.state.queue_occupancy.retired_item_census` owns that judgement
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

    **The drop set is VERIFIED under the flock** (*witnessed_records*, F10). The
    caller's census is necessarily computed outside this lock — the janitor has
    to read and join the run stores to decide what retired — and the dispatch
    lock that would otherwise serialize it is per-cid while this ledger is
    global. So a ``queue-dispatch`` tick for a DIFFERENT campaign can append a
    placement transition for an item this census already condemned, and the
    rewrite then deletes a row that other process is at that moment reporting as
    ``placed``. Pass ``{item_id: record_count}`` as observed by the census and
    this function re-counts each item's records in the file it just read under
    the lock, dropping ONLY the items whose count is unchanged; the rest are
    disclosed as ``raced_items`` and left for a later pass, which will see the
    new records and judge them afresh. Omitting *witnessed_records* keeps the
    old unverified behaviour and is meant for a caller that has no census to
    offer (a test calling the mechanics directly).

    Re-reading was preferred over the alternative fix — dropping the orphaned
    transition afterwards — for the ordering it can actually see. A line that
    arrives BEFORE this lock is still evidence of a decision another process
    made and is now reporting on, so declining to compact is the answer that
    keeps both stores true. Orphan-dropping is applied only to the one ordering
    re-reading cannot reach (below), where the line is provably the tail of a
    rewrite THIS ledger already made.

    * **An orphan of a PREVIOUS compaction is reaped.** The other side of the
      same race: the concurrent placement lands AFTER the rewrite, leaving a
      transition record whose enqueue is gone. Fold rule 2 (only an enqueue
      opens an item) makes such a line permanently unfoldable, and because the
      drop set is derived from FOLDED items it is permanently uncompactable too —
      an immortal ledger line and a permanent "inspect the ledger" note on every
      ``queue-status``. A non-enqueue record whose ``item_id`` is a known
      compaction TOMBSTONE is therefore dropped: this ledger authored that
      removal, so the line is the tail of its own rewrite rather than foreign
      evidence, and :func:`append_intake_item` refuses to mint a new item under a
      tombstoned id, so the id cannot mean anything else. An orphan with no
      tombstone behind it is KEPT — that one is unexplained, and unexplained is
      exactly what compaction must never erase.

    **R8 (replay dedup) is preserved rather than argued away.**
    ``append_jsonl_line`` dedups an enqueue by scanning the file for the record's
    ``request_id``, so dropping an item's records drops its dedup entry too. The
    shipped argument for that being safe — "a compacted item's run has RETIRED,
    so a replay would be ADOPTED by ``queue-dispatch`` against the surviving
    RunRecord" — held only while the RunRecord survived, and
    ``KEEP_TERMINAL_RUNS`` is the only thing that makes it. Past that bound the
    replay dedup-misses, re-enqueues, finds nothing to adopt, and really
    resubmits a completed run. So every dropped record's ``request_id`` is
    persisted as a TOMBSTONE on the watermark
    (:func:`compaction_tombstones`), and :func:`append_intake_item` consults it:
    the replay writes no line and is disclosed as a duplicate. Every STILL-LIVE
    request keeps every one of its records, so dedup for live items is untouched
    either way.

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
        "raced_items": [],
        "reaped_orphans": 0,
    }
    tombstones = compaction_tombstones(experiment_dir)
    if not drop and not tombstones:
        return report
    if not path.is_file():
        return report

    lock_path = path.with_suffix(path.suffix + ".lock")
    with advisory_flock(lock_path, timeout_sec=120.0):
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            _log.warning("queue_intake: cannot compact unreadable %s (%s)", path, exc)
            return report
        parsed: list[tuple[str, dict[str, Any] | None]] = []
        counted: dict[str, int] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                parsed.append((line, None))  # unparseable: evidence, not history
                continue
            if not isinstance(rec, dict):
                parsed.append((line, None))
                continue
            parsed.append((line, rec))
            item_id = rec.get("item_id")
            if isinstance(item_id, str) and item_id:
                counted[item_id] = counted.get(item_id, 0) + 1

        # F10: the census was taken outside this lock. An item whose record count
        # has moved since then acquired evidence this pass never saw — another
        # process is mid-decision on it — so leave it whole and say so.
        raced = (
            sorted(
                item_id
                for item_id in drop
                if witnessed_records.get(item_id) != counted.get(item_id)
            )
            if witnessed_records is not None
            else []
        )
        report["raced_items"] = raced
        drop -= set(raced)

        kept: list[str] = []
        dropped_ids: set[str] = set()
        dropped_records = 0
        dropped_request_ids: list[str] = []
        reaped = 0
        for line, rec in parsed:
            if rec is None:
                kept.append(line)
                continue
            item_id = rec.get("item_id")
            if not isinstance(item_id, str) or not item_id:
                kept.append(line)
                continue
            if item_id in drop:
                dropped_ids.add(item_id)
                dropped_records += 1
                request_id = rec.get("request_id")
                if isinstance(request_id, str) and request_id:
                    dropped_request_ids.append(request_id)
                continue
            if rec.get("kind") != _KIND_ENQUEUE and item_id in tombstones:
                # An orphan of a compaction this ledger already performed.
                reaped += 1
                continue
            kept.append(line)
        report["kept_records"] = len(kept)
        report["reaped_orphans"] = reaped
        if not dropped_records and not reaped:
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
                _TOMBSTONE_KEY: _merged_tombstones(prior, dropped_request_ids),
            },
        )
    return report


def _merged_tombstones(prior: dict[str, Any], minted: Iterable[str]) -> list[str]:
    """The watermark's next tombstone list — oldest-first, deduped, capped.

    Order is carried over from the PRIOR list so the cap truncates the OLDEST
    entries rather than an arbitrary set: the newest tombstones are the ones
    whose replay window may still be open (see :data:`TOMBSTONE_CAP`).
    """
    raw = prior.get(_TOMBSTONE_KEY)
    ordered = [v for v in raw if isinstance(v, str) and v] if isinstance(raw, list) else []
    seen = set(ordered)
    for request_id in minted:
        if request_id in seen:
            continue
        seen.add(request_id)
        ordered.append(request_id)
    return ordered[-TOMBSTONE_CAP:]


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
