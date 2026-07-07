---
name: attention-queue
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent attention-queue --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.attention_op.attention_queue
---
# attention-queue

The **fleet-wide digest ordered by needs-your-verdict-first**: every place in the
system where a human action is the blocking edge — pending greenlights,
committed-but-unadvanced decisions, anomaly briefs, campaign completion briefs,
unsigned/stale notebook-audit sections, dead detached workers, unacknowledged
alerts, open ssh circuits — collected across one experiment or the whole fleet,
ordered by a deterministic code-computed rule, rendered as a deterministic
markdown digest relayed to the human **verbatim**.

A read-only `query` (the `doctor` posture: no SSH, no side effects,
`idempotent=True`). Pure ordering/identity projection — **code computes the queue;
no LLM prioritization prose anywhere in the path** (`docs/design/attention-queue.md`,
D1). It is a **standing TODO** recomputed on every read, never persisted: there is
no digest file, no cache, no watermark. It moves **no state** — the `mark_seen` /
alert-acknowledgment watermarks stay `status-snapshot`'s job (D6).

## Inputs

An `AttentionQueueSpec` (`hpc_agent._wire.queries.attention_queue`) plus the
standard `--experiment-dir`:

- `fleet` (bool, default `false`) — when `false`, scope is the single
  `experiment_dir`. When `true`, widen to **every experiment this machine has
  journaled** (glob the journal home for `*/repo.json`, recover each experiment
  root, run the identical per-experiment collection). A namespace whose
  `experiment_dir` no longer exists, is unreadable, or is torn is **skipped
  silently and counted** in `skipped` (a wiped demo repo never crashes the read).
- `class_order` (list of string, optional) — a CLASS-sequence override (the T12
  `attention_order` precedent): listed classes (`blocked` / `verdict` /
  `informational`) first in the given order, **unknown names ignored** (not
  refused), unlisted classes keep the default order after. The default is
  `blocked, verdict, informational`. It overrides **only** the class tiebreak — the
  primary fan-out key, the within-class rule, and the fan-out computation itself
  are fixed (re-ranking individual items would be prioritization prose).
- `now` (string, optional) — an ISO-8601 UTC `now` override for deterministic
  testing (the `doctor` precedent). It sets the single `computed_at` stamp and the
  instant ages render against. **Never** an agent-facing knob for reshaping ages.

## The ordering rule (D2, revised)

A **total order**, so the render is byte-reproducible for a given fleet state:

**fan-out descending → class order → oldest `since` first → `(kind, scope_id)`**.

The primary key is **LEVERAGE** = the item's unblock **fan-out** — the count of
pending downstream subjects that become actionable when this one verdict clears,
**COUNTED** over the dependency edges the journals already encode, never scored
(D2 revision, user 2026-07-08):

- a committed-unadvanced greenlight → its run (fan-out 1);
- an unsigned/stale audit section → the module's `passed` gate → every run whose
  sidecar `audited_source` echo names that audit (fan-out = that count);
- a campaign-pending verdict → the campaign's remaining (non-terminal) runs.

Where no encoded edge exists the fan-out is `0` and the item falls through to the
class order **byte-identically** with the pre-revision rule. This stays inside the
no-fabrication boundary: there is no urgency score, no `critical/high/low`
vocabulary, no cross-source age interpretation — priority is class + position,
both recomputable from the record.

## Outputs

`data` is an `AttentionQueueResult`:

```
{
  "computed_at": "<ISO-8601 UTC — the single instant the queue was computed>",
  "items": [
    {
      "kind": "greenlight-unadvanced | run-parked | run-stalled | run-anomaly |
               dead-worker | campaign-pending | audit-section-unsigned |
               audit-section-stale | alert | ssh-circuit-open",
      "class": "blocked | verdict | informational",
      "subject": {"scope_kind": "run|campaign|scope|notebook|null", "scope_id": "...", "block": "..."},
      "experiment_dir": "<which experiment (fleet disambiguator)>",
      "cluster": "<where, or null>",
      "since": "<ISO ts the condition began, or null>",
      "action": "<the source's OWN drafted proposal/note, or null>",
      "unblocks": <int — the fan-out leverage count, 0 when no encoded edge>,
      "evidence": {<the source's own structured dict, opaque>}
    }
  ],
  "counts": {"blocked": n, "verdict": m, "informational": k},
  "skipped": [{"ref": "<repo_hash | audit_id>", "reason": "..."}],
  "render": "<the deterministic markdown digest — relay VERBATIM>"
}
```

`render` rides the result the way `relay` rides a `StatusBlockResult` — the agent
relays it verbatim; the fan-out shows on each line as an honest `unblocks N` count
(only when `> 0`), never as urgency prose.

## Errors

- `spec_invalid` — a non-ISO-8601 `now` override. Not retry-safe; fix the string.

## Idempotency

A pure query with no side effects and no natural identity key. The queue is
recomputed from the journals on every call, so replaying after more work simply
reflects the current state — a persisted digest would be a second source of truth
that drifts from the journal (reconcile-is-truth).

## The snapshot embed

`status-snapshot`'s brief carries an additive `attention` field — this
experiment's queue items in the same order — computed by the **same**
`collect_queue` seat this verb calls (D4). The in-flow morning read and this
standalone digest therefore cannot disagree on ordering.

## Usage

```
hpc-agent attention-queue --spec spec.json --experiment-dir .
```

where `spec.json` is e.g. `{"fleet": true}` or `{}`. Read-only QUERY verbs go
DIRECT through MCP — call the typed tool with inline args and relay the returned
`render`; never a spec-file round-trip just to read state back.
