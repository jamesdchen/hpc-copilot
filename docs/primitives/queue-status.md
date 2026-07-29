---
name: queue-status
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent queue-status --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.queue.status.queue_status
---
# queue-status

## Purpose

The run queue's read surface: **intake items joined to projections over the
stores that already exist**. The intake ledger
(`<experiment>/.hpc/queue/intake.jsonl`) is an INDEX, not a second journal — it
records arrival facts and the two states `queued` / `placed`, and nothing else
(run-queue plan §7 R1, §8 S10). Every other fact this verb reports —
`dispatched`, `run_status`, `in_flight`, `terminal`, `parked`,
`greenlight_committed` — is RECOMPUTED on every read from RunRecords
(`state/index.py`), pending-decision markers (`state/journal.py`) and the
decision journal (`state/decision_journal.py`). A ledger that COPIED park state
would drift from the journal the first time a `y` landed; a ledger that projects
it cannot, which is why "the subagent returns the parked item to the ledger" is
already true the moment `block-drive` writes the marker — there is no return
step to build.

The greenlight question routes through
`decision_journal.is_committed_greenlight_for_boundary`, the same
boundary-scoped predicate `attention-queue`, the `block-drive` Stop guard and
`doctor` all key on (§8 S12). Never the unscoped latest-greenlight read: a
consumed `y` is never removed from the journal, so after a tick consumes it and
re-parks, the latest record is a stale greenlight and the unscoped read would
mint a false "the human already said yes" on every scan. Two "what needs me"
surfaces that disagree are worse than one surface, and this is the seam where
they would.

Read-only and non-creating: no SSH, no cache, no digest file, no watermark, and
the ledger path is computed without materializing `.hpc/` — a query must never
scaffold the tree it reports on.

## Inputs

A `QueueStatusSpec` JSON spec, every field optional:

- `state` ("queued" | "placed" | null) — restrict to items in this LEDGER
  state. It filters the STORED state, never a projected one: "show me the
  parked items" would be a filter over a recomputed fact and offering it would
  teach callers to treat a projection as storage.
- `campaign_base` (str | null) — restrict to items enqueued under this logical
  campaign base.
- `cluster` (str | null) — restrict to items PINNED to, or already PLACED on,
  this `clusters.yaml` key. Both facets match, because "what is queued for carc"
  must answer with the items headed there and the items already there.
- `include_settled` (bool, default false) — when false, items whose projected
  run is terminal are omitted from `items` but still counted. Carrying settled
  history into every read is the O(history) startup cost that violates the
  relaunch-cheapness invariant; true is the audit read.
- `limit` (int, 1–500, default 50) — the page bound. The result is a relayed
  digest, not a database cursor.
- `now` (str | null) — ISO-8601 UTC evaluation-instant override for
  deterministic tests (the `doctor` / `attention-queue` precedent). It sets
  `computed_at` and the instant `age_sec` is measured against; it is not a knob
  for reshaping ages.

`experiment_dir` arrives through the standard `--experiment-dir` CLI arg.

## Outputs

A `QueueStatusResult` with:

- `computed_at` (str) — the single instant the whole projection was computed
  against, so a digest read an hour later is visibly an hour old.
- `path` (str) — absolute path of the intake ledger, reported even when it does
  not exist yet.
- `items` (list[QueueStatusItem]) — matching items in ARRIVAL order, clipped to
  `limit`. Arrival order, not a priority order: ranking the human's attention is
  `attention-queue`'s job and re-deriving it here would be a second, divergent
  ranking. Each item carries its LEDGER facts (`item_id`, `state`,
  `enqueued_at`, `updated_at`, `age_sec`, `run_name`, `run_id`, `campaign_base`,
  `campaign_id`, `cluster_pin`, `cluster`, `placement_reason`, `resources`,
  `spec_ref`) and its PROJECTED facts (`dispatched`, `run_status`, `in_flight`,
  `terminal`, `parked`, `park_block`, `awaiting_since`, `greenlight_committed`,
  `greenlight_unadvanced`, `collides_with`).
- `counts` (dict[str, int]) — `queued`, `placed`, `dispatched`, `in_flight`,
  `parked`, `greenlight_unadvanced`, `terminal` over ALL matching items before
  the clip and before settled items are hidden. Always all seven keys,
  zero-filled, so a brief can quote one without a membership test.
- `occupancy` (dict[str, int]) — campaign_id → occupied pool slots, from the one
  shared predicate `state/queue_occupancy.occupied_slots`
  (`occupied = journal {in_flight, submitting} ∪ intake {queued, placed}`,
  deduplicated by slot key). Surfaced from the same read that shows the items so
  a second, subtly different occupancy count has no reason to exist.
- `total_items` (int) / `truncated` (bool) — how many matched, and whether the
  bound bit. A partial read is never silent.
- `skipped_records` (int) — every line the ledger held that this digest does not
  show: lines the reader dropped (unparseable, or JSON that is not an object),
  records the fold could not place (a transition naming an unknown item, a state
  outside `{queued, placed}`), and folded items that could not satisfy the wire
  shape. The three causes are disjoint and the reader's half comes from
  `state.queue_intake.read_intake_records_counted`, so the count is the whole
  drop rather than the part visible after the read. Non-zero is the signal to go
  look at the file.
- `notes` (list[str]) — deterministic, code-computed disclosure lines. Never
  authored prose: each restates a fact already in the result.

## Errors

- `spec_invalid` — `spec.now` is not an ISO-8601 UTC timestamp. Everything else
  degrades rather than raising: a torn tail, a foreign line, a record naming an
  unknown item, a record carrying a state outside `{queued, placed}`, or an item
  the wire shape rejects is skipped, counted in `skipped_records`, and named in
  `notes`. A queue read sits on the "what needs me" path, so one bad byte must
  never wedge it — but it must not hide the drop either.

## Idempotency

Idempotent and side-effect-free: reads only, writes nothing — not a file, not a
watermark, not a cache. Two calls against unchanged state return byte-identical
results (modulo `computed_at`, which is why `now` exists). The projection is
recomputed on every read by construction, so there is no cached digest that can
go stale against the journal.

## Notes

- Storage read: `state/queue_intake.py` (fold), `state/journal.py`
  (`load_run`, `read_pending_decision`), `state/decision_journal.py`
  (`is_committed_greenlight_for_boundary`), `state/queue_occupancy.py`
  (`occupied_slots`). The R7 route-through is pinned by a source assertion in
  `tests/ops/queue/test_queue_status.py`, alongside a test that the unscoped
  predicate WOULD have answered differently on a consumed-then-re-parked
  greenlight — the guard shown firing.
- Hold-back reasons for items still `queued` are not invented here.
  `queue-advance` is the placement authority and its hold-backs are its own
  output (pure, disclosed, unstored); this verb reports that the item is still
  queued and, once placed, the `placement_reason` recorded on the durable
  placement record.
- `attention-queue` enumerates parks over `in_flight` runs only
  (`find_parked_runs`). This verb joins by the item's computed `run_id`, so it
  can see a park on a `submitting` run that the fleet digest cannot; when that
  happens it says so in `notes` rather than diverging quietly. For any run both
  surfaces see, the greenlight answer is the same predicate on the same
  arguments.
- Two items whose resolved params and `run_name` agree compute the SAME
  `run_id`. That collision is reported in `collides_with` and in `notes`, over
  the whole ledger rather than the filtered page, because §10.S2 requires it be
  said rather than silently collapsed.
- No SSH, no cluster contact, no process probe.
