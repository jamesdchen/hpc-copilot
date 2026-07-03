---
name: hpc-aggregate
description: "Start the aggregate workflow with the code-driven chain (`block-drive`, first block `aggregate-check`) and relay each decision brief to the human for a `y`/nudge; on `y` commit the approved input spec to the journal's `resolved` and let the driver advance. Check surfaces readiness + integrity issues (never auto-masked); a clean run advances to `aggregate-run`, the deterministic combine+reduce whose reducer — never the LLM — computes every aggregate number. The skill never resolves a decision and never interprets raw results."
allowed-tools: Bash Read Write
execution: inline
category: agent-autonomous
---

Start the aggregate workflow by invoking the **`block-drive`** verb — the code-driven chain (design [§2](../../../../docs/design/human-amplification-blocks.md), [§6](../../../../docs/design/block-drive.md)). It starts at the **`aggregate-check`** block, chains the deterministic spans in code, and exits at each human decision point returning a **brief**. You are the translator at those rendezvous points **only**: render the brief as a proposal, take the human's `y` or nudge, and on `y` commit the approved input spec so the next `block-drive` tick advances. You do **not** read `next_block` and dispatch the next verb yourself — that sequencing is re-homed off the LLM into the driver's chaining table (design §6).

The two aggregate blocks (`ops/aggregate_blocks.py`) `block-drive` composes are `aggregate-check` (readiness: run-terminal gate + `aggregate-preflight` — which reconciles a journal-only in-flight run against the cluster before refusing — plus the `verify-aggregation-complete` integrity gate, every violation surfaced as a **never-auto-masked** decision point with a conservative recommendation) and `aggregate-run` (the deterministic `aggregate-flow` pipeline: combine → reduce → a code-extracted results table). Each hands back `{block, stage_reached, needs_decision, reason, brief, next_block?, run_id?}`. **The reducer is the whole execution and the sole source of every aggregate number.**

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

## The driver loop

`block-drive` drives the sequence in code; you translate at the rendezvous points it stops at. Each tick:

1. **Invoke `block-drive`.** The first call starts the chain at `aggregate-check`; each later call consumes the approved spec from the journal's `resolved` and advances (to `aggregate-run`) — or re-runs the block a nudge changed (e.g. an `allow_partial` decision on `missing_waves`). The route is a **function of the spec** (design §4), computed in code — never a verb you pick.
2. **Render the brief the driver returns as a proposal.** Relay `reason` + `brief`: the check's readiness digest (record found, terminal status, combined/failed waves, `integrity_report`) and its `integrity_issues` (each with `auto_masked: false` and a recommendation), or the run's results table + error-sweep summary + harvest-ledger tail. Relay the code-extracted table; never re-interpret the raw metrics.
3. **The human answers `y` or nudges.** A single `y` approves the proposed input spec; anything else is a nudge, which you fold into the block's **inputs** (never a hand-edited derived output) and re-present. Loop until `y`.
4. **On `y`, commit the approved input spec to the journal's `resolved`, then invoke `block-drive` again to advance.** The commit *is* the approval (design §3, §5). Append the record:
   ```bash
   hpc-agent append-decision --spec <path> --experiment-dir <dir>
   ```
   `scope_kind: "run"`, `scope_id: <run_id>`, `block: <terminated block>`, `evidence_digest: <brief>`, `proposal: <what you surfaced>`, `response: "y"`, and the approved input spec under `resolved` (a spec, never the nudge string) — the block-gate (`ops/block_gate.py`) reads exactly this and refuses an `aggregate-run` the human did not greenlight against the check brief. **Do not end your turn after committing without firing the next tick** — the decision-rendezvous Stop-hook (design §5) blocks the stop until the driver advances.

A `not_ready` / `integrity_review` check carries `needs_decision: true` — a non-terminal run or a contamination/provenance/column violation is a human branch (keep watching, reconcile, or investigate); the driver surfaces the recommendation and the nudge names the action. An integrity issue is **never** auto-masked to proceed. **NEVER hand-compute an aggregate metric or interpret raw results:** the `aggregate-run` reducer is the sole source of every number; the human decides.

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
