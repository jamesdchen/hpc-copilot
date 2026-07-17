---
name: settle-aggregate
verb: workflow
side_effects:
- writes-journal: <experiment>/.hpc/runs/<run_id>.decisions.jsonl (the directed aggregate-settle
    sign-off)
idempotent: false
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent settle-aggregate --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.settle_aggregate.settle_aggregate
---
# settle-aggregate

Give an **operator-bypass table** a provenance home. When the reducer runs
OUTSIDE hpc-agent (the operator's direct reduce — run-13 finding 14) the table
has no aggregate record, no harvest receipt, and its journal provenance is LOST.
`settle-run` settles a single run's TERMINAL state; there was no analogue for "a
table was produced outside the flow — attach provenance to it retroactively."
`settle-aggregate` is that analogue, extending the `settle-run` directed-evidence
pattern to the AGGREGATE stage.

It **records, it never gates.** Given the table + the runs the human claims it
derives from + a typed human utterance naming the artifact, it validates SHAPE
(the artifact exists → its sha256 is computed at record time; every named run
exists) and journals the human's utterance as a directed sign-off — with
`source: "operator-settled, provenance human-asserted"`. The numbers are **never
blessed**: the record attests only that a human settled this table over this
human-asserted run-set.

It **never synthesizes consent.** The utterance must be human-authored (the same
harness-captured utterance-log evidence tier `append-decision`'s human-authorship
gate uses): when the utterance log exists, an agent-composed utterance sharing no
words with anything the human typed is REFUSED, not silently accepted. Without a
log (the capture hook not installed) it records at the `unverified-fallback`
tier — disclosed on the record, never hidden.

Once journaled, `verify-relay` treats the named contributing ids as authorized
via its normal auth-id join (it folds a settle-aggregate record's
`contributing_run_ids` into `auth_ids` exactly as it folds a run's `campaign_id`
/ `parent_run_ids`), so a truthful relay of the operator-settled table's run-set
is no longer flagged.

## Inputs

A `SettleAggregateInput` (`hpc_agent._wire.workflows.settle_aggregate`):

- `run_id` (string, required) — the run scope the settle is journaled under (the
  run the table is cited under; it need not have gone through the sanctioned
  flow).
- `aggregate_ref` (string, required) — path to the table artifact. It MUST exist
  — its sha256 is computed at record time (a hash is never asserted into
  existence); an absent artifact is refused.
- `derives_from` (list of strings, required, non-empty) — the run-set the human
  claims the table derives from. Every named run MUST exist (a record or
  sidecar); the settle records a human-asserted lineage, it does not invent runs.
- `utterance` (string, required) — the human's typed consent naming the artifact.
  Human-authored; an agent-composed utterance is refused.
- `provenance` (string, optional) — a note on how the settle was captured.
- `--experiment-dir` (path, default cwd) — the experiment root.

## Outputs

`data` is a `SettleAggregateResult`:

- `stage_reached` — always `settled`.
- `artifact_sha256` — the sha computed over the table's bytes at record time.
- `contributing_run_ids` — the human-asserted derives-from set now authorized for
  `verify-relay`.
- `authorship` — the evidence tier the utterance cleared (`harness-captured` /
  `unverified-fallback` — disclosed, not hidden).
- `decision_ts` — the journaled record's timestamp.

The journaled decision (under the run scope, block `settle-aggregate`) carries the
utterance verbatim as its `proposal`, and a `provenance` block with `directed:
true`, `kind: "human-directed-aggregate-settle"`, the artifact ref + sha, the
contributing ids, and `source: "operator-settled, provenance human-asserted"`.

## Errors

- `spec_invalid` — the artifact does not exist / cannot be read; a named
  contributing run has no record or sidecar; or the utterance is agent-composed
  (the utterance log is present and the utterance shares no word with it). Not
  retry-safe; fix the input (or the human types the utterance).

## Idempotency

Not idempotent: each call appends a new directed-settle record (the append-only
decision journal accretes evidence). A re-settle records a fresh human attestation
rather than mutating the prior one.

## Usage

```
hpc-agent settle-aggregate --spec spec.json --experiment-dir .
```

where `spec.json` is `{"run_id": "<id>", "aggregate_ref": "<path>",
"derives_from": ["<run_id>", ...], "utterance": "<the human's typed consent>"}`.
Like `settle-run`, `settle-aggregate` is not MCP-curated — it is a human-consent
recording surface, reachable through the CLI registry, and the human types the
utterance (the verb never composes it).
