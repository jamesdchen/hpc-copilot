---
name: queue-run
verb: mutate
side_effects:
- file_write: <experiment>/.hpc/queue/intake.jsonl
idempotent: true
idempotency_key: request_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: cluster_unknown
  category: user
  retry_safe: false
- code: config_invalid
  category: user
  retry_safe: false
- code: journal_corrupt
  category: internal
  retry_safe: false
backed_by:
  cli: hpc-agent queue-run --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.queue.run.queue_run
---
# queue-run

## Purpose

Put ONE requested run onto the experiment's run queue — the intake half of the
organ the fleet was missing (`docs/plans/run-queue-placement-2026-07-28.md`
§1, §6). Experiments come in from any session at any time, and workflows are
stateless relays that die and replay from cache, so arrival has to land in a
durable kernel store rather than in a workflow's memory. `queue-run` is that
store's only writer: it appends one record to
`<experiment>/.hpc/queue/intake.jsonl` carrying the item's **arrival facts**
(the resolve spec or a pointer to it, a run name, an optional explicit cluster
pin, an optional campaign base, typed resource asks) and nothing else.

**Enqueueing is ungated (R2, §1): it spends nothing.** No consent is consumed,
no greenlight is probed, no journal is written, no cluster is contacted. That
is the design, not a gap to close later. The human's y binds to a spec that
NAMES a cluster (§3 — placement lives inside the y), and an item on the ledger
has not been placed; gating arrival would take a verdict against a question
the queue cannot yet ask, and would make writing down what someone wants as
expensive as occupying a login node. Gates bind where they always bind: at the
cluster boundary of the run the item becomes.

The ledger is an **index, not a second journal** (R1, §7 R1 / §8 S10). A record
stores lifecycle in `{queued, placed}` and nothing wider — `dispatched`,
`parked`, `in-flight` and `terminal` are projections `queue-status` recomputes
over the run stores that already own them.

## Inputs

A `QueueRunSpec` JSON spec with:

- `request_id` (str, `^[A-Za-z0-9._\-]+$`) — **required.** The client-minted
  idempotency token (a UUID4 is the expected shape), passed straight through as
  the intake append's `dedup_key` and adopted as the item's `item_id`
  (§10.S2). Required rather than derived because the CLIENT is the only party
  that knows which two calls are the same call.
- `spec` (object | null) — the resolve spec, recorded VERBATIM. Exactly one of
  `spec` / `spec_ref` must be given; an empty object is refused. Opaque here:
  `queue-run` validates that work is named, never what the work is — the
  resolver owns that invariant.
- `spec_ref` (str | null) — a pointer to a resolve spec on disk, for a spec
  that is large or already versioned in the repo. **Not dereferenced at
  enqueue:** a pointer that goes stale is a fact for the projection to surface,
  not a reason to refuse arrival.
- `run_name` (str | null) — optional logical run name. Load-bearing later:
  run ids are COMPUTED, not minted (`run_id = "<run_name>-<cmd_sha[:8]>"`,
  §10.S2), so this is what lets a resolved item collapse onto the same
  occupancy slot as the run it becomes.
- `cluster` (str | null) — an optional EXPLICIT cluster pin, a `clusters.yaml`
  top-level key. R5 makes a pin supreme over placement policy, and the pin is
  therefore **verified here against the live config** through the real loader:
  an unknown key, or a config that cannot be read, is refused (see Errors). The
  reason is R5 itself — nothing downstream re-chooses for a pinned item, so a
  typo would become a permanently unplaceable record whose disclosed reason
  reaches the operator hours later in a brief instead of now. A blank string is
  refused rather than demoted to "unpinned": `""` and absent mean opposite
  things.
- `campaign_base` (str | null) — an optional logical campaign base. Placement
  composes the concrete `<base>_<clusterkey>` campaign id
  (`docs/design/campaign-multi-cluster.md` §2); null means an open-loop item,
  which belongs to no campaign and occupies no campaign pool slot.
- `resources` (object) — typed resource asks (`gpu`, `gpu_type`, `cores`,
  `walltime_sec`, `est_core_hours`), the hard-constraint leg of placement.
  Defaults to an empty ask, which every configured cluster satisfies.

`experiment_dir` arrives through the standard `--experiment-dir` CLI arg.

## Outputs

A `QueueRunResult` with:

- `path` (str) — absolute path of the intake ledger.
- `item_id` (str) — the item's identity on the ledger (equal to `request_id`).
- `request_id` (str) — echo of the token, so a relay can confirm the match.
- `state` (`"queued"` | `"placed"`) — `"queued"` on arrival; an item cannot
  ARRIVE placed. A replay of an item that has since been placed reports
  `"placed"` — the honest current state.
- `replayed` (bool) — `true` when the token was already on the ledger: **no
  line was written** and `record` is the ORIGINAL item.
- `run_id` (str | null) — null in Phase 1. The run id is computed from a
  RESOLVED spec (§10.S2) and Phase 1 does not resolve at enqueue; the field is
  populated from the ledger when a later transition put one there.
- `enqueued_at` (str) — ISO-8601 UTC arrival stamp of the item (of the
  ORIGINAL item on a replay).
- `queued_count` (int) — how many items on this ledger are currently in state
  `queued`, i.e. the depth the next `queue-advance` will consider.
- `record` (dict) — the item as the ledger reads back (the record as written,
  plus the fold's `updated_at` / `record_count`), so the session relays what
  the queue holds rather than a restatement of it. The verified pin appears as
  `cluster_pin`, never as `cluster`: `cluster` is written only by a placement
  transition, and collapsing the two would make an operator's REQUEST
  indistinguishable from `queue-advance`'s disclosed DECISION.

## Errors

- `spec_invalid` — the spec names no work or names it twice (`spec` /
  `spec_ref`), an empty `spec` object, or a blank `cluster` pin. Nothing is
  appended: every refusal happens before the write.
- `cluster_unknown` — the `cluster` pin names a key absent from the active
  `clusters.yaml`. The message carries the near-miss suggestion, and the code's
  own remediation is `hpc-agent clusters list` — which is why this is
  `cluster_unknown` and not `spec_invalid`, matching `clusters-describe`,
  `dir-digest`, and `host-retarget` for the same fact. `queue-advance`'s
  `cluster_pin_unknown` hold-back covers the complementary case: a pin that was
  valid at enqueue and went stale under a later `clusters.yaml` edit.
- `config_invalid` — a `cluster` pin was given but `clusters.yaml` could not be
  loaded, or did not parse to a mapping of cluster keys. An unverified pin is
  not a verified pin.
- `journal_corrupt` — the record was appended but the ledger does not read it
  back. Echoing a record no reader can see would report an enqueue that never
  happened.

An **unpinned** item reaches none of the cluster-config paths at all — it has
no cluster question to ask, so it asks none, and the yaml is never even parsed.

## Idempotency

**Idempotent, keyed on `request_id`.** The client's token is handed to
`infra.io.append_jsonl_line` as its `dedup_key`, so a replayed call — the
workflow engine relaying a completed turn verbatim from cache — finds the
earlier record, writes NO second line, and returns the ORIGINAL item with
`replayed=true` (R8, §10.S2). The probe runs *inside* the ledger's flock, so it
is also race-free against a genuinely concurrent duplicate.

The dedup compares that ONE field: a replay whose payload differs is still a
replay, and the stored record wins. That is deliberate — the ledger records
what arrived first, and a second call under the same token is a relay, not a
revision.

This is the opposite of `tag-session`'s honest-duplication posture, and the
difference is what the ledger is for: a devx ingestion ledger may duplicate and
dedup on read, whereas a duplicated queue item is a duplicated submission.

## Notes

- Storage: `state/queue_intake.py` — one append-only JSONL ledger per
  experiment through `infra.io.append_jsonl_line` (whole-line-atomic,
  flock-serialized, fsync'd, torn-tail self-healing). Reads are tolerant: a
  torn or foreign line is skipped, never fatal, so one bad byte cannot wedge a
  "what needs me" path.
- Pure local write. No SSH, no scheduler contact, no journal write.
- Siblings: `queue-advance` (pure placement authority — reads only, returns a
  decision with a disclosed reason per item) and `queue-status` (the projection
  over intake plus the existing run stores).
- `queue-dispatch`, the drain workflow, eager submit, and per-cluster consent
  caps are Phase 2/3 and deliberately absent.
