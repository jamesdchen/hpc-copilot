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
    "fold_intake_records",
    "intake_path",
    "intake_path_if_exists",
    "items_in_states",
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
    resource asks, cluster pin, campaign base, computed ``run_id``, ...). This
    seam stamps the four fields the fold owns — ``kind``, ``item_id``,
    ``request_id``, ``enqueued_at``/``ts`` — and forces ``state`` to
    ``"queued"``: an item cannot ARRIVE placed, and letting a caller assert
    otherwise would put a placement on the ledger that ``queue-advance`` never
    decided (R3/R5).

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

    Phase 1 ships no writer for this: ``queue-advance`` is pure (R3) and
    ``queue-dispatch`` is Phase 2. It exists because the ``{queued, placed}``
    vocabulary R1 fixes is not expressible without it, and because putting the
    transition anywhere but this module would re-open the two-store bookkeeping
    S10 closed.
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
