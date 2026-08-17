---
name: hpc-check
description: "Adopt an ALREADY-EXECUTED freestyle run — hand-rolled scripts, raw sbatch/qsub, an ad-hoc result layout, possibly long finished — and verify it mechanically (the post-exploration fidelity checker, hpc-agent's new front door). The skill orchestrates: elicit the run's facts, adopt it via adopt-run (which writes the sidecar, mints the journal record, and settles a terminal run), drive it to terminal if it is still in flight, aggregate via aggregate-check then aggregate-run (the reducer — never the LLM — computes every aggregate number), and — only if the caller brought a claimed number — run verify-reproduction in external-baseline mode and relay the CODE-rendered verdict VERBATIM. A claim-check is NEVER a reproduction: the only honest sentences are the code-rendered consistency sentence — for an adopted run, 'the claim is consistent with the adopted run's records (within caller tolerance)' — or a dated FINDING; mismatch is exit-0, needs_decision, never blocking. Claimed values are HUMAN-AUTHORED and authorship-gated. The skill never resolves a decision and never interprets raw results."
allowed-tools: Bash Read Write Glob
execution: inline
category: agent-autonomous
---

Drive the **post-exploration fidelity check** — hpc-agent's new front door for a run that already happened. An agent (or scientist) ran an exploration FREESTYLE: hand-rolled scripts, raw `sbatch`/`qsub`, an ad-hoc result layout, possibly already finished. Now they want hpc-agent to verify it mechanically. This skill ADOPTS that already-executed run — it never re-runs it — gives it an honest identity (sidecar + journal record), drives it to terminal if it is still in flight, reduces its results in code, and, only if the caller brought a claimed number, checks the claim against the adopted run's records. The slash `/check-hpc` is the human-interview wrapper; an external autonomous agent invokes this skill directly. Either way the loop is the same — the difference is only who types the `y`/nudge.

## The doctrine this skill enforces

- **The reducer — never the LLM — computes every aggregate number.** Aggregation is `aggregate-run`'s reducer — deterministic code. An LLM in the compute loop is the exact failure this skill exists to prevent (wrong arithmetic *and* `ok: true`). NEVER hand-compute a mean, never write `metrics.json` from prose arithmetic, never "fill in" a missing number; relay the reducer's render VERBATIM. If the reducer cannot run — a readiness or integrity gate blocked, partials missing — surface the block's typed failure; do NOT fabricate a number.
- **A `claim-check` is NEVER a reproduction.** "Reproduced" requires two OBSERVED runs; an external claim was never observed. The machinery may only assert consistency, and the honest sentence it emits on a match is CODE-rendered — for an adopted run, *"the claim is consistent with the adopted run's records (within caller tolerance)"*; for a fresh observed run (hpc-claim-check's flow), *"the claim is consistent with a fresh observed run (within caller tolerance)"*. Relay the code-rendered consistency sentence VERBATIM; never call a claim-match a "reproduction" and never characterize match/mismatch in your own words (the consistency determination is the comparator's — trusted code, caller tolerance as data).
- **Claimed values are HUMAN-AUTHORED and authorship-gated.** Elicit them as FREE TEXT the human types — never a pre-filled option they click (a click carries no authorship the sign-off gate accepts). They live in the spec and are embedded verbatim in the receipt; they ride the human-authorship gate at `append-decision` like every human spec.
- **A mismatch is a dated FINDING, never an accusation, never blocking** (exit-0, `needs_decision: true`). The brief surfaces which identity dimension moved — code, env, or data. The human concludes; core compares.
- **The skill never resolves a decision and never interprets raw results.** The verbs compute; you relay the code-rendered projection VERBATIM. The human decides, the code executes, the LLM translates — never decides.

## Disambiguation — hpc-check vs hpc-claim-check

- **hpc-check** adopts an ALREADY-EXECUTED freestyle run — the computation already happened; this skill gives it an honest identity and verifies it mechanically. It never re-runs.
- **hpc-claim-check** takes a CLAIM (e.g. from a paper) and runs it fresh TWICE under observation (the double canary mints n=2 OBSERVED fingerprint samples), then checks the claim against a fresh observed run.

A claim checked against an ADOPTED run earns only *"the claim is consistent with the adopted run's records (within caller tolerance)"* — the code-rendered sentence, quoted exactly; `hpc-claim-check`'s fresh-observed flow renders *"the claim is consistent with a fresh observed run (within caller tolerance)"* instead. Only `hpc-claim-check`'s double-fresh-observed flow can approach reproduction-grade evidence — and even that is NEVER called *"reproduced."* The naming lock is absolute: a claim-check is never a reproduction.

## Execution style

- **Batch independent tool calls into one assistant message.** Multiple Read / Glob / Bash tool-call blocks in one message run concurrently. Do NOT use shell-level concurrency (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — it trips the permission classifier as a compound command.
- **Be terse.** Lead with the action or result; skip filler and trailing restatements of what tool output already shows.
- **Final action MUST be a tool call.** A closing chat message with no tool call ends the turn and strands the flow; make the `evidence-brief` relay (or the next verb) the turn's last act.

## Inputs

| Field | Source |
|---|---|
| `experiment_dir` | Required (absolute path) |
| `run_id` | Required — the caller's own name for the already-executed run |
| `command` | Required — the EXACT per-task command that was executed; `cmd_sha` is DERIVED from it, never free-typed |
| `cluster` | Required — the cluster the run executed on |
| `ssh_target` | Required |
| `remote_path` | Required |
| `job_ids` | Caller (optional) — present ⇒ the run is still in flight |
| `terminal_evidence` | Required when `job_ids` is absent — evidence that the run reached a terminal state |
| `results_sample` | Caller (optional) — a sample result path from which `result_dir_template` / `task_count` / `summary_artifact` are inferred; otherwise pass those fields directly |
| `claimed_values` | Caller (optional) — the human-authored claimed metric values, keyed by the (flattened) metric key; elicited as free text the human types, authorship-gated |

## Steps

### 1. Elicit the run's facts

Elicit from the caller (the human via `/check-hpc`, or an external agent feeding the spec) the facts that identify the already-executed run: the experiment dir (`experiment_dir`), the run's id (`run_id`), the EXACT per-task command that was executed (`command` — `cmd_sha` is derived from it, never free-typed), where it ran (`cluster`, `ssh_target`, `remote_path`), the `job_ids` when the run is still in flight, the result layout (`result_dir_template` / `task_count` / `summary_artifact`) or a `results_sample` path to infer them from, and — when there are no `job_ids` — a `terminal_evidence` that the run reached a terminal state. COMPOSE what the repo already proves (the cwd's git root IS the `experiment_dir` when it carries experiment markers — an `interview.json` or a `.hpc/` tree) and DISCLOSE the composed value in the restatement rather than asking again. If the caller has a claimed number they want checked, elicit `claimed_values` (and any `tolerance` / `claimed_data_sha`) as FREE TEXT the human authors — never a pre-filled option they click. Restate the composed spec for correction, then fold it into the `hpc-agent adopt-run` spec.

### 2. Adopt the run

Run `hpc-agent adopt-run` with the elicited spec — `run_id`, `command` (`cmd_sha` is derived from it, never free-typed), `cluster`, `ssh_target`, `remote_path`, `job_ids` when still in flight, the result layout (or the `results_sample` to infer it from), and `terminal_evidence` when `job_ids` is absent. `adopt-run` writes the sidecar, mints the journal record, and settles a terminal run; its envelope carries `next_block`. Write the spec to a file and shell the CLI (`--spec` takes a file path only), or call the typed MCP tool with inline args:

```bash
hpc-agent adopt-run --spec .hpc/specs/adopt-run.json --experiment-dir .
```

Relay the envelope's render VERBATIM.

### 3. Drive the run to terminal

Branch on the adopted run's lifecycle as the envelope reports it:

- **terminal** — proceed to aggregation (step 4).
- **in flight** (`job_ids` present) — drive `hpc-agent status-watch` until terminal, or surface the watch so the caller drives it. Background the watch through the harness's native backgrounding (Claude Code `run_in_background`), never a shell `&`, and never a hand-rolled local-log tail on a cluster job — that is the wrong-machine improvisation class, blind to the cluster's own state. Relay each watch brief VERBATIM.
- **failed / abandoned** — surface the typed failure; recovery is a human branch, never an auto-resubmit.

Reconcile is the only source of run state: NEVER infer "still running" from an open log, a live pid, elapsed time, or an empty output file — report only what `hpc-agent status-snapshot` / the watch brief returns.

### 4. Aggregate (the reducer computes every number)

Run `hpc-agent aggregate-check` over the adopted `run_id`; it gates readiness + integrity and surfaces every issue, never auto-masked. On a clean gate, commit the greenlight through `hpc-agent append-decision` BEFORE `aggregate-run` — `aggregate-run` carries its greenlight gate inside the verb body (`assert_greenlit_target`): a call the run's journal does not greenlight is refused with a self-remediating message. Then advance to `hpc-agent aggregate-run` — the deterministic combine + reduce whose reducer is the sole source of every aggregate number:

```bash
hpc-agent aggregate-check --spec .hpc/specs/aggregate-check.json --experiment-dir .
hpc-agent aggregate-run --spec .hpc/specs/aggregate-run.json --experiment-dir .
```

Relay the reducer's results-table render VERBATIM. Never re-compute or re-interpret the numbers — the reducer computed them; the human chooses any interpretation from the code-extracted table.

### 5. Claim-check compare (only if `claimed_values` were given)

Only if the caller brought `claimed_values`: run `hpc-agent verify-reproduction` in EXTERNAL-BASELINE mode only, over `{repro_run_id: <the adopted run_id>, external_baseline: {claimed_values, tolerance?, claimed_data_sha?}}`. Do NOT pass `original_run_id` or a top-level `tolerance` — they are mutually exclusive with `external_baseline` and the verb refuses the pairing. Write the spec to a file and shell the CLI (`--spec` takes a file path only), or call the typed MCP tool with inline args:

```bash
hpc-agent verify-reproduction --spec .hpc/specs/claim-check.json --experiment-dir .
```

Relay the CODE-rendered verdict VERBATIM — this is a claim-check, NEVER a reproduction. On a match, the comparator's consistency sentence ("the claim is consistent with the adopted run's records (within caller tolerance)"); on a mismatch or incomparable, the dated FINDING with the drift-dimension disclosure (`needs_decision: true`, exit-0, never blocking). Do NOT paraphrase, summarize, or characterize the verdict in your own words — the comparator decided consistency; you point the caller at the render.

**Closing guard — after the verdict is relayed, offer (non-blocking, optional) `hpc-agent verify-relay`:** it audits the relayed text against the run's durable records. Model-carried text is worth nothing to the gates; the relay audit is what makes the summary itself checked. If no `claimed_values` were given, skip to step 6.

### 6. Record the exchange

Journal the exchange via `hpc-agent append-decision` — `scope_kind: "run"`, `scope_id: <run_id>`, `block: <the verb that terminated — adopt-run | aggregate-check | verify-reproduction>`, `evidence_digest: <brief>`, `proposal: <what you surfaced>`, `response: "y"`, and the approved spec under `resolved`. Claimed values ride the human-authorship gate like every human spec; a refused authorship gate is remedied by the HUMAN stating the values, never by a verb. One record per exchange, append-only.

### 7. Hand back the evidence pointer

End with the `hpc-agent evidence-brief` pointer so the caller can cite the durable records — the sidecar, the journal record, the reducer's results table, and (when run) the claim-check receipt. Relay the brief's `render_path` + shas VERBATIM; the caller cites the durable records, never a paraphrase you composed.

## Notes

- **hpc-check adopts an ALREADY-EXECUTED run — it never re-runs.** For a CLAIMED literature value that needs a fresh run under observation (the double-fresh-run flow), use `hpc-claim-check` instead; that skill stays as-is for claimed values, and hpc-check does not borrow its flow.
- **No fetch of external artifacts.** The caller brings the facts; we manifest what arrives, we never fetch (the refusal list).
- **No verdict on the CLAIM's truth** — only consistency with the adopted run's records under the caller's tolerance.
- **No forced memory record.** The sidecar + journal + reducer output (+ claim-check receipt when run) are the durable records; a citing conclusion is optional composition through the evidence-memory machinery.
- **Every `y`/nudge is journaled** (append-only, one record per exchange).
