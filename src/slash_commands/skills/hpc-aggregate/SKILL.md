---
name: hpc-aggregate
description: "Start the aggregate blocks (`aggregate-check`) and relay each block's code-digested brief to the human for a `y`/nudge, journaling every exchange and invoking exactly the block the envelope's `next_block` names. Check surfaces readiness + integrity issues (never auto-masked); a clean run's `next_block` is `aggregate-run`, the deterministic combine+reduce whose reducer — never the LLM — computes every aggregate number. The skill never resolves a decision and never interprets raw results."
allowed-tools: Bash Read Write
execution: inline
category: agent-autonomous
---

Start the aggregate workflow by invoking the **`aggregate-check`** block, then run the propose→`y`/nudge loop (design [§2](../../../../docs/design/human-amplification-blocks.md)): surface each block's code-digested **brief** plus its machine-computed **`next_block`** suggestion to the human, collect a `y` or a natural-language nudge, journal the exchange, and on `y` invoke **exactly** `next_block.verb`.

The two aggregate blocks (`ops/aggregate_blocks.py`) are `aggregate-check` (readiness: run-terminal gate + `aggregate-preflight` — which reconciles a journal-only in-flight run against the cluster before refusing — plus the `verify-aggregation-complete` integrity gate, every violation surfaced as a **never-auto-masked** decision point with a conservative recommendation) and `aggregate-run` (the deterministic `aggregate-flow` pipeline: combine → reduce → a code-extracted results table). Each hands back `{block, stage_reached, needs_decision, reason, brief, next_block?, run_id?}`. **The reducer is the whole execution and the sole source of every aggregate number.**

The slash `/aggregate-hpc` is the human-interview wrapper; an external autonomous agent invokes this skill directly.

> **NEVER compute an aggregate metric yourself, and NEVER write `metrics.json` from your own arithmetic or a `Read`-then-mean-in-your-head shortcut.** Aggregation is the `aggregate-run` block's reducer — deterministic code (cluster-reduce when an `aggregate_cmd` is set, the cluster combiner, or a per-task `metrics.json` weighted-mean otherwise). That reducer is the SoT for every number; an LLM in the compute loop is the exact failure this skill exists to prevent (wrong arithmetic *and* `ok: true`). If the reducer cannot run — a readiness or integrity gate blocked, partials missing — surface the block's typed failure or park the anomaly; do NOT fabricate a number or "fill in" a missing `metrics.json`.

## Invocation surface

- **Batch independent tool calls into one assistant message.** Multiple Bash / Read / Grep / Glob tool-call blocks in one message run concurrently. Do NOT use shell-level concurrency (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — trips the permission classifier as a compound command.
- **MCP-first (preferred):** the typed `aggregate-check` / `aggregate-run` tools from `hpc-agent mcp-serve`.
- **CLI fallback:** one call per block, spec written to a file with the `Write` tool:
  ```bash
  hpc-agent aggregate-check --spec <path> --experiment-dir <dir>
  ```
  Parse the envelope from stdout. Read files with `Read`/`Grep`/`Glob`, never a shell `python -c` / `bash -c` / `jq` (the auto-mode classifier hard-blocks those).

## The block loop

Repeat until a terminal block (the harvest brief) or the human ends the run:

1. **Invoke the block.** First iteration: `aggregate-check`. On `y`, the verb its `next_block.verb` named (`aggregate-run`), seeded from `spec_hint`.
2. **Relay the brief.** Render `reason` + `brief`: the check's readiness digest (record found, terminal status, combined/failed waves, `integrity_report`) and its `integrity_issues` (each with `auto_masked: false` and a recommendation), or the run's results table + error-sweep summary + harvest-ledger tail. Relay the code-extracted table; never re-interpret the raw metrics.
3. **Collect the answer.** A single `y` greenlights the suggested `next_block`; anything else is a nudge.
4. **Journal the exchange:**
   ```bash
   hpc-agent append-decision --spec <path> --experiment-dir <dir>
   ```
   `scope_kind: "run"`, `scope_id: <run_id>`, `block: <terminated block>`, `evidence_digest: <brief>`, `proposal: <what you surfaced>`, `response: "y"` or the nudge text; on a greenlight, `resolved: {"next_block": "<next_block.verb>"}`. The block-gate (`ops/block_gate.py`) reads exactly this and refuses an `aggregate-run` that the human did not greenlight against the check brief — a loud, self-enforcing sequence.
5. **Advance.** On `y`, invoke `next_block.verb`. On a nudge, fold it into the current block's spec (e.g. an `allow_partial` decision on `missing_waves`) and re-invoke the same block. A `not_ready` / `integrity_review` check carries `next_block: null` and `needs_decision: true` — a non-terminal run or a contamination/provenance/column violation is a human branch (keep watching, reconcile, or investigate); surface the recommendation and let the nudge name the action. An integrity issue is **never** auto-masked to proceed.

## Never-stall

`aggregate-run` touches the cluster (wave combine + rsync pull). When the pull is slow, run the block through your harness's native backgrounding (Claude Code's `run_in_background`), **never** a shell `&`; the results-table brief arrives as a notification.

## Inputs

| Field | Source |
|---|---|
| `experiment_dir` | Required |
| `profile` | Caller, else the check auto-discovers the profile with terminal runs |
| `run_id` | Caller, else the latest terminal run for the profile |
| `allow_partial` | Caller (default `false`; surfaced as the `missing_waves` decision otherwise) |

## Notes

- **The reducer computes every aggregate number; the skill never does.** No hand-computed means, no prose arithmetic, no model-authored `metrics.json` — even "it's just a mean of ten numbers." If the reducer genuinely cannot run, return the block's typed failure or park the anomaly.
- **Refuse partial by default.** `missing_waves` under the default surfaces as a decision (safe recommendation: investigate); the human explicitly greenlights `allow_partial` after seeing what's missing.
- **Reconcile before "nothing to aggregate."** The journal can lag the cluster; `aggregate-check`'s preflight reconciles a journal-only in-flight run against live cluster state before the block refuses — a run that ran-and-failed surfaces as a typed failure with the classified error, an `abandoned` run (no alive jobs, no on-disk failure evidence) as its own typed failure with a re-submit / combiner-only remediation, never as an indefinite "still in-flight."
- **Idempotent.** Re-aggregating the same `(run_id, profile, stage)` produces byte-identical output.
- **Every `y`/nudge is journaled** (append-only, one record per exchange).
