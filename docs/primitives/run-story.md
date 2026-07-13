---
name: run-story
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent run-story --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.run_story.run_story
---
# run-story

Render a run's **complete journal trail** as one deterministic, ordered,
attributed timeline. A pure read: it merges every record store the run left
behind — the decision journal, the emitted briefs, the block terminals, the
journal record's lifecycle stamps + verdict history, and (keyed off the run's
sidecar) the scope journals + look ledgers and the notebook attestation journal —
into one event list ordered by recorded timestamp, then fingerprints it with
`story_sha`.

This is the decision journal's **interface** sibling (`docs/design/run-story.md`):
the journal is a trustworthy archive of typed, gated attestations, and the story
is the one code-rendered view of it — "every event has an author and a hash, and
none of it was narrated by a model". It is a PURE projection — IDENTITY (which
run/scope/section), ORDERING (recorded ts), and COUNTING (sha pointers, row/job
counts) over opaque records. It never interprets what any record MEANS: counts
render, metric values never; scope tags stay opaque slugs; an agent-drafted brief
renders as a sha pointer, never as advice.

## Inputs

A `RunStorySpec` (`hpc_agent._wire.queries.run_story`):

- `run_id` (string, required) — the run whose trail is merged into one timeline.
- `include_lineage` (bool, default `false`) — widen the read to the run's whole
  supersession lineage (the one `lineage_chain` walk), not just the single run.
- `since_ts` (string, optional) — a lexicographic ISO-8601 timestamp **floor**;
  keep only events at or after it. Omitted events are counted, never silently
  dropped.
- `limit` (int ≥ 1, optional) — keep only the most recent N events (a newest-last
  window). The omission count is a rendered, countable fact.
- `markdown` (bool, default `true`) — include the code-rendered markdown timeline
  in the result.

## Outputs

`data` is a `RunStoryResult`:

```
{
  "run_ids": ["<run_id>", ...],   // one, or the lineage chain (newest→root)
  "events": [
    {
      "ts": "<ISO-8601, or '' when absent>",
      "stream": "decision-journal | briefs | block-terminal | journal-record | scope-journal | look-ledger | notebook-journal",
      "actor": "human | code",
      "kind": "<record-class literal: block name, 'look', 'verdict', 'scope-lock', 'kill-requested', ...>",
      "subject_id": "<run_id / scope tag / audit section — opaque identity>",
      "evidence": { "<*_sha | *_digest | *_count | ...>": "..." },  // pointers + counts only
      "text": "<the human's verbatim words, or ''>"
    }
  ],
  "story_sha": "<64-hex over the WINDOWED canonical JSON>",
  "markdown": "<code-rendered timeline, or '' when not requested>",
  "total_events": <int>,     // full count before any window (>= len(events))
  "omitted_count": <int>      // events a window dropped — never silent
}
```

Attribution (`hpc_agent.state.run_story`): `actor="human"` exactly for a
human act under the existing gates — a decision-journal `response` (greenlight or
nudge), a scope **unlock**, a notebook sign-off, or a `verdict_history` entry
whose `decided_by` is not `code`. Everything else — briefs, terminals,
auto-clears, receipts, looks, locks, and the watchdog/kill/supersession stamps —
is `actor="code"`. The human's verbatim words render as `text`; agent/code-drafted
prose (a brief, a proposal, a verdict rationale) rides `evidence` as a sha digest
ONLY, never as narrative.

## Errors

- `spec_invalid` — the requested run has NEITHER a sidecar NOR a journal record
  (nothing to render — the `export-dossier` no-sidecar-no-record guard). Not
  retry-safe; fix the `run_id`. An absent *individual* store (no briefs, no
  scopes) is DATA, not an error — an empty run yields an empty story.

## Idempotency

A pure query with no side effects and no natural identity key. Derived state:
recomputed from the on-disk records on every call, so it can never drift from a
second source of truth. The story journals nothing and attests nothing —
`story_sha` is a FINGERPRINT (verifiable against a re-render or a dossier), not an
attestation.

## Usage

```
hpc-agent run-story --spec spec.json --experiment-dir .
```

where `spec.json` is `{"run_id": "<id>"}` (plus any of `include_lineage`,
`since_ts`, `limit`, `markdown`). The story is deliberately NOT MCP-curated and
is never a block — it is an operator/reviewer action, not a decision point. The
`hpc-status` wrapper may mention it after a terminal snapshot, but prose proposes,
never sequences.
