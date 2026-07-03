---
name: campaign-greenlight
verb: workflow
side_effects:
- writes-campaign-state: <experiment_dir>/.hpc/campaigns/<campaign_id>/ (manifest
    greenlit marker + decisions.jsonl, on confirm only)
idempotent: true
idempotency_key: spec.campaign_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent campaign-greenlight --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.meta.campaign.blocks.campaign_greenlight
---
# campaign-greenlight

The **start** touchpoint of the campaign flow as a human-amplification block
(design §4). It digests the greenlit-once campaign spec — the manifest's
goal / budget / strategy / stop_criteria / anomaly_policy / async_refill — into
a code-digested *brief* for the `y`/nudge propose loop, or records a
caller-supplied greenlight. A campaign's spec is drafted and greenlit **once,
at campaign start**; that spec is then the complete contract and execution runs
fully asynchronously against it. This verb never decides on its own: it
digests, or it stamps a greenlight the human already gave.

## Inputs

- `campaign_id` (str, required) — the campaign whose manifest to digest.
- `confirm` (bool, default false) — set on the post-`y` re-invocation to
  RECORD the greenlight: stamp `mark_greenlit` onto the manifest and journal
  the decision. Left false, the block only DIGESTS the spec (nothing stamped).
- `response` (str, default `"y"`) — the human's answer to journal when
  `confirm` is set (`"y"`, or the nudge text that shaped the final spec).
- `proposal` (str | list | dict | null) — the LLM's drafted proposal over the
  spec brief, journaled verbatim alongside the response when `confirm` is set.
- `journal` (bool, default true) — when `confirm` is set, also append the
  greenlight to the campaign decision journal. Disable to stamp the marker
  without a record (e.g. a re-stamp).

## Outputs

A `CampaignBlockResult`: `{block: "greenlight", stage_reached, needs_decision,
reason, campaign_id, brief}`. `stage_reached` is one of:

- `needs_greenlight` (`needs_decision=true`) — the spec was digested into
  `brief`; awaiting the once-at-start `y`/nudge. Nothing stamped.
- `greenlit` (`needs_decision=false`) — a `confirm` re-invocation stamped the
  marker and (unless `journal=false`) journaled the decision.
- `already_greenlit` (`needs_decision=false`) — an idempotent re-read; the
  marker was already set. Nothing stamped or journaled.

The `brief` carries the digested spec (`goal`, `budget`, `strategy`,
`stop_criteria`, `anomaly_policy`, `async_refill`, `max_in_flight`) plus the
`greenlit` / `greenlit_at` provenance marker.

## Errors

- `SpecInvalid` — the campaign has no manifest. The greenlight marker rides the
  spec, so the manifest must exist first (write it via `campaign-init` /
  `write_manifest`); greenlighting a campaign with no manifest is a loud
  failure, never a silent no-op.

## Idempotency

Idempotent on `campaign_id`. A non-confirm read never mutates state. A
`confirm` re-stamp refreshes `greenlit_at` but the marker stays `true`; the
already-greenlit re-read leaves the timestamp untouched. The greenlight is a
DATA marker, not an execution gate — no primitive blocks on it.

## Notes

The verb composes `mark_greenlit` (manifest) and `append_decision` (the
campaign-scope decision journal) directly — same `meta.campaign` /
`state` package surface — rather than re-implementing either. It is the campaign
analogue of the submit S1 resolve brief: code digests, the human greenlights,
the LLM only translates.
