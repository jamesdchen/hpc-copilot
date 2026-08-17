`/check-hpc` is the **human-interview wrapper** around the `hpc-check` skill — the post-exploration fidelity check that ADOPTS an already-executed freestyle run (hand-rolled scripts, raw `sbatch`/`qsub`, an ad-hoc result layout), gives it an honest identity, reduces its results in code, and — only if the caller brought a claimed number — checks the claim against the adopted run's records. The slash parses the caller's arguments, elicits the run's facts, invokes the skill, and relays each code-rendered brief for a `y`/nudge. The human decides, the code executes, the LLM translates — never decides.

## The flow

1. **Parse `$ARGUMENTS`** — whatever the caller pre-states: the `run_id`, the exact per-task `command`, `cluster`, `ssh_target`, `remote_path`, `job_ids` if the run is still in flight, the result layout (or a `results_sample` path to infer it from), `terminal_evidence` when there are no `job_ids`, and any `claimed_values`.
2. **Elicit what is missing as free text — never pre-filled options** (a click carries no authorship). Claimed values are HUMAN-AUTHORED: the caller types the number they want checked. COMPOSE what the repo already proves (the cwd is the `experiment_dir` when it carries experiment markers) and DISCLOSE the composed value rather than asking again.
3. **Invoke the skill** on the resolved facts. It runs `adopt-run`, relays the envelope, drives the run to terminal, aggregates via `aggregate-check` → `aggregate-run`, and — if a claim was brought — claim-checks via `verify-reproduction`.
4. **Relay each brief, collect `y` or a nudge.** Show the code-rendered brief VERBATIM (the adopt envelope, the watch terminal digest, the reducer's results table, the claim-check verdict). The caller greenlights with a single `y` or nudges. No per-field `[Y/n]` dialogs.
5. **Loop.** On `y`, the skill journals the greenlight (`append-decision`) and fires the verb the envelope named. Continue until the evidence brief.

## Invocation

Invoke the `hpc-check` skill via the Skill tool (only the fields the caller pinned):

```
Skill("hpc-check", {
  experiment_dir: ".",
  run_id: <required — the caller's own name for the already-executed run>,
  command: <required — the exact per-task command; cmd_sha is derived from it, never free-typed>,
  cluster: <required>,
  ssh_target: <required>,
  remote_path: <required>,
  job_ids: <if the run is still in flight>,
  terminal_evidence: <required when job_ids absent>,
  results_sample: <a path to infer result_dir_template/task_count/summary_artifact from, if those aren't stated>,
  claimed_values: <if the caller brings a claimed number — human-authored free text>
})
```

The skill resolves the rest through the flow; the slash never enumerates every field.

## Relaying a brief

- **Adopt envelope:** relay the render VERBATIM; it carries `next_block` (drive-to-terminal when `job_ids` are present, else straight to aggregation).
- **In-flight watch:** relay the `status-watch` terminal digest VERBATIM; reconcile is the only source of run state.
- **Aggregate:** relay the reducer's results table VERBATIM — the reducer computes every number; the caller chooses any interpretation, never you.
- **Claim-check verdict:** relay the CODE-rendered verdict VERBATIM. On a match, the comparator's consistency sentence; on a mismatch, the dated FINDING (`needs_decision: true`, exit-0, never blocking). Then offer the optional `verify-relay` audit of the relayed text against the durable records.

## The naming lock

This is a **claim-check, NEVER a reproduction.** An external claim was never observed, so the machinery may only assert consistency — the code-rendered consistency sentence for a claim checked against an ADOPTED run is "the claim is consistent with the adopted run's records (within caller tolerance)," else a dated FINDING. (The fresh-observed variant, "the claim is consistent with a fresh observed run (within caller tolerance)," belongs to `hpc-claim-check`'s double-fresh-observed flow.) Relay the code-rendered verdict VERBATIM; never call a match a "reproduction" and never characterize match/mismatch in your own words. Only `hpc-claim-check`'s double-fresh-observed flow approaches reproduction-grade evidence, and even that is never called "reproduced."

## Args

`$ARGUMENTS` formats:
- Free-form intent: `"verify my freestyle ridge_h2 run"` — parse to `run_id` + facts.
- Flags: `--run-id <id>`, `--command <cmd>`, `--cluster <name>`, `--ssh-target <t>`, `--remote-path <p>`, `--job-ids <ids>`, `--results-sample <path>`, `--claimed <json>`.
- Empty: invoke with `{experiment_dir: "."}`; the skill's elicit step surfaces what needs the caller.

## Notes

- **hpc-check adopts an ALREADY-EXECUTED run — it never re-runs.** For a CLAIMED literature value that needs a fresh double-run under observation, use `hpc-claim-check`.
- **The reducer computes every aggregate number.** Never hand-assemble a metric or interpret the raw results.
- **No verdict on the CLAIM's truth** — only consistency with the adopted run's records under the caller's tolerance.
