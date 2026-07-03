---
name: append-decision
verb: mutate
side_effects:
- file_write: <experiment>/.hpc/runs/<run_id>.decisions.jsonl | <experiment>/.hpc/campaigns/<campaign_id>/decisions.jsonl
idempotent: false
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent append-decision --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.decision.journal.append_decision
---
## Purpose

Append one `y`/nudge exchange to a run's or campaign's **decision journal** — the append-only JSONL store that resolves design §2's mandate: "Every `y`/nudge exchange is journaled: the decision record, not the chat scroll, is the source of truth for why a run took the shape it did." It generalizes the per-run `verdict_history` audit (`state/run_record.py`, "why a non-deterministic decision took its branch") from failure-escalations to **every** human touchpoint — submit S1–S4 briefs, canary greenlights, campaign specs, anomalies, harvest interpretations.

Takes an `AppendDecisionInput` JSON spec; auto-stamps `ts` (UTC ISO-8601) and `schema_version`; appends one line under an advisory flock (never rewrites a prior record). Returns `{"path": ..., "record": {...}, "count": N}`.

## Decision-journal schema (resolves design §8 TODO #4)

A recorded `y`/nudge exchange persists exactly these fields, and no more:

| Field | Type | Why it is persisted |
|---|---|---|
| `schema_version` | int | Store-shape version. Bumped only on a breaking change; readers tolerate additive fields (forward-compat), so it rarely moves. |
| `ts` | str (ISO-8601 UTC) | When the exchange was recorded. Establishes the append order and answers "what changed since the human last looked" (§5 state contract). Auto-stamped — no caller asserts it. |
| `scope_kind` | `"run"` \| `"campaign"` | Which store this belongs to. A run journals submit/anomaly/harvest touchpoints; a campaign journals the once-at-start spec greenlight plus anomaly/completion briefs (§4). |
| `scope_id` | str (filesystem-safe) | The `run_id` or `campaign_id`. Becomes a path segment, so it is slug-constrained. |
| `block` | str (free-text) | The block terminator that raised the decision point — e.g. `submit.S1`, `submit.S2`, `campaign.spec`, `anomaly`, `harvest`. Free-text because the block grammar (§3) is open and experiment-agnostic; the journal must not hard-code a closed enum of touchpoints. |
| `evidence_digest` | str \| dict | The **code-digested** evidence the proposal was drafted over (status, errors+logs, metrics). Persisted so the audit answers "what did the human actually see when they decided?" — the load-bearing input, never re-interpreted by the journal. |
| `proposal` | str \| list \| dict | The **LLM-drafted** proposal over that evidence: a debugging fix, a set of interpretation options, or a next-block suggestion (§2). Text and/or a list of options. |
| `response` | str | The human's answer: the literal `"y"` (greenlight sentinel, by protocol) **or** the natural-language nudge text. This is the load-bearing human input — the whole point of the journal. |
| `resolved` | dict | The resulting decision as structured data. On a **greenlight** it carries the settled decision (what the `y` accepted). On a **nudge** it is typically empty — the exchange re-drafts and re-presents (§2's loop-until-`y`), so nothing is settled yet. Each nudge round is its own record with an empty `resolved`; the terminal `y` round carries the resolution. |
| `provenance` | dict (optional) | Who/how — e.g. `{"decided_by": "human", "surface": "slash", "session": "..."}`. Optional; defaults to `{}`. |

**Why this set and not more.** The journal is the *decision* record, not the chat transcript: it captures the three load-bearing surfaces of §2 — the code-digested `evidence_digest`, the LLM-drafted `proposal`, and the human's `response` — plus enough addressing (`scope_kind`/`scope_id`/`block`) and ordering (`ts`) to reconstruct "why this run took this shape" without replaying the conversation. Palatable rendering is syntactic sugar (§2) and is deliberately **not** persisted. The record is opaque to the journal — `evidence_digest`/`proposal`/`resolved` are round-tripped verbatim, never interpreted (the "results are never interpreted raw by an LLM" doctrine, §2, applied to the audit layer).

**Why append-only, one record per exchange.** A nudge loops (§2 step 4: re-draft, re-present, until `y`), and each round is journaled — so the trail shows the *sequence* of nudges that shaped the final decision, not just the endpoint. Rewriting or collapsing rounds would destroy exactly the "why it took the shape it did" signal the journal exists to keep.

## Inputs

- `scope_kind` (`"run"` | `"campaign"`) — which store.
- `scope_id` (str) — the `run_id` or `campaign_id`; filesystem-safe.
- `block` (str, non-empty) — the block terminator id.
- `response` (str, non-empty) — `"y"` or the nudge text.
- `evidence_digest` (str | dict, optional) — code-digested evidence.
- `proposal` (str | list | dict, optional) — the LLM-drafted proposal.
- `resolved` (dict, optional) — the settled decision (empty on a nudge round).
- `provenance` (dict, optional) — who/how.

## Outputs

`{"path": "<journal jsonl>", "record": {<persisted record>}, "count": N}` — `count` is the total records in the journal after this append (≥ 1).

## Errors

- `spec_invalid` — unknown `scope_kind`, non-filesystem-safe `scope_id`, empty `block`, or empty `response`.

## Idempotency

**Not idempotent.** The journal is an audit log: a replayed append records a second line rather than deduping, and there is no natural idempotency key (`ts` is auto-stamped per call). Callers must not retry blindly expecting a no-op.

## Storage locality

- Run scope: `<experiment_dir>/.hpc/runs/<run_id>.decisions.jsonl` (beside the run sidecar).
- Campaign scope: `<experiment_dir>/.hpc/campaigns/<campaign_id>/decisions.jsonl` (inside the campaign's canonical scratch dir).

One JSON object per line, newest last. Appends are serialized under an advisory `flock` (same discipline as `state/journal.py` and the monitor tick log) and `fsync`-ed.

## Notes

- **Separate store.** The decision journal never touches `state/run_record.py` or the `RunRecord` JSON. It generalizes `verdict_history`; it does not replace it.
- **`response == "y"`** is the greenlight sentinel by protocol; any other value is a nudge. To distinguish machine-side, compare against `"y"`.
- **Read it back** with `read-decisions --spec <path>`.
