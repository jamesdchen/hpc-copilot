---
name: hpc-claim-check
description: "Onboard a scientist who arrives with repo + script + a CLAIMED result (onboard-by-reproduction, rung 6). The skill orchestrates: onboard the artifact via the interview path, run the fresh experiment twice under observation (the double canary mints n=2 OBSERVED fingerprint samples), then run verify-reproduction in external-baseline mode to compare the claim against a fresh observed run, and relay the CODE render VERBATIM. A claim-check is NEVER a reproduction: an external claim was never observed, so the machinery only asserts consistency-with-a-fresh-observed-run under caller tolerance, and mints no fingerprint sample from the claim. The skill never resolves a decision and never characterizes match/mismatch in its own words."
allowed-tools: Bash Read Write Glob
execution: inline
category: agent-autonomous
---

Drive **onboard-by-reproduction** (`docs/design/onboard-by-reproduction.md`, rung 6 of the onboarding map) — the strongest first interaction the copilot offers a mid-career arrival: *"let's see if your result reproduces under observation."* The scientist brings a repo, a script, and a claimed result ("my paper says QLIKE 0.1203; here's the code"); the skill onboards the artifact, runs it fresh twice under observation, and compares the fresh numbers against the claim. The output is a `claim-check` receipt — the claim's evidence history begins at the front door.

## The naming lock (the doctrine this skill enforces)

- **A `claim-check` is NEVER a reproduction.** "Reproduced" requires two OBSERVED runs; an external claim was never observed. The machinery may only assert consistency, and the ONE honest sentence it emits on a match is CODE-rendered: *"the claim is consistent with a fresh observed run (within caller tolerance)."* You relay that sentence VERBATIM; you never call a claim-match a "reproduction" and never characterize match/mismatch in your own words (the consistency determination is the comparator's — trusted code, caller tolerance as data).
- **The claim lives in the spec, embedded verbatim in the receipt.** There is deliberately no other required claim record. The claimed values + tolerances are human-authored and authorship-gated at `append-decision` like every human spec. A human MAY later write a conclusion citing the receipt's sha — optional, through the evidence-memory machinery, zero new record types.
- **The fingerprint history starts from OBSERVED runs only.** The two fresh runs mint honest n=2 samples via the double canary. The claim-check comparison itself appends NO fingerprint sample — an unobserved claim can never widen the envelope.
- **A mismatch is a dated FINDING, never an accusation, never blocking** (exit-0, `needs_decision`). The brief surfaces which identity dimension moved — code, env, or data. With a data manifest at claim time the brief can say "the data changed since the claim"; without one it discloses "cannot distinguish result decay from data drift — no manifest". The human concludes; core compares.
- **The skill never resolves a decision and never interprets raw results.** The verbs compute; you relay the code-rendered projection VERBATIM.

## Execution style

- **Batch independent tool calls into one assistant message.** Multiple Read / Glob / Bash tool-call blocks in one message run concurrently. Do NOT use shell-level concurrency (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — it trips the permission classifier as a compound command.
- **Be terse.** Lead with the action or result; skip filler and trailing restatements of what tool output already shows.
- **Final action MUST be a tool call.** A closing chat message with no tool call ends the turn and strands the flow; make the `verify-reproduction` relay (or the next verb) the turn's last act.

## Inputs

| Field | Source |
|---|---|
| `experiment_dir` | Required (absolute path) |
| `repro_run_id` | Required — the fresh observed run whose metrics are compared against the claim (one of the two fresh runs from step 2) |
| `claimed_values` | Required — the human-authored claimed metric values, keyed by the (flattened) metric key (e.g. `{ "gp.qlike": 0.1203 }`), elicited as free text the human types |
| `tolerance` | Caller (optional) — the caller-owned `ReproTolerance` (default/per-key abs+rel); absent → exact |
| `claimed_data_sha` | Caller (optional) — the data identity (manifest) recorded at claim time; enables the "the data changed since the claim" drift dimension |

## Steps

### 1. Onboard the artifact

Invoke the `hpc-wrap-entry-point` skill (via the Skill tool) to onboard the repo — detect or scaffold the entry point, decorate `@register_run`, walk the data-axis tree, and persist `tasks.py` + `interview.json`. This is the existing interview path; it mints the run identity the fresh runs will carry. Elicit the `claimed_values` (and any `tolerance` / `claimed_data_sha`) as FREE TEXT the human authors — never a pre-filled option they click (a click carries no authorship the sign-off gate accepts).

### 2. Run fresh under observation, twice

Invoke the `hpc-submit` skill (via the Skill tool) to run the onboarded experiment. The submit machinery's double-canary pattern mints honest n=2 OBSERVED fingerprint samples (`scale: canary`) — the first evidence in the claim's history. Repeat the fresh run so two observed samples accrue; each run is a real submission, staged, canaried, and harvested by the block chain. The two fresh runs are the observed baseline the fingerprint accretes from; the claim is compared against one of them.

### 3. Claim-check compare (external-baseline mode)

Run `hpc-agent verify-reproduction` in EXTERNAL-BASELINE mode over `{repro_run_id, external_baseline: {claimed_values, tolerance?, claimed_data_sha?}}`. The mode rides the existing `hpc-agent verify-reproduction` spec — there is NO new verb. The baseline side is the claim; the comparison runs the SAME caller-tolerance comparator the recorded-original mode uses. Do NOT pass `original_run_id` or a top-level `tolerance` — they are mutually exclusive with `external_baseline` and the verb refuses the pairing. Write the spec to a file and shell the CLI (`--spec` takes a file path only), or call the typed MCP tool with inline args:

```bash
hpc-agent verify-reproduction --spec .hpc/specs/claim-check.json --experiment-dir .
```

The verb emits a `claim-check` receipt (`receipt_kind: "claim-check"`) at `_aggregated/<repro_run_id>/claim_check_receipts.jsonl` — never the reproduction ledger — embedding the claim verbatim, and appends NO fingerprint sample.

### 4. Relay the code render VERBATIM

Relay the `hpc-agent verify-reproduction` result to the human VERBATIM — the `reason` field carries the CODE-emitted line: on a match, the consistency sentence (`stage_reached: "match"`, `needs_decision: false`); on a mismatch or incomparable, the dated finding with the drift-dimension disclosure (`needs_decision: true`, exit-0, never blocking). Surface the receipt's `consistency` / `drift_disclosure` fields unaltered. Do NOT paraphrase, summarize, or characterize the verdict in your own words — the comparator decided consistency; you point the human at the render. The human concludes which of code / env / data moved; the skill does not.

## Notes

- **No fetch of external artifacts.** The scientist brings the repo; we manifest what arrives, we never fetch (the refusal list).
- **No verdict on the CLAIM's truth** — only consistency with an observed run under the caller's tolerance.
- **No forced memory record.** The receipt is the durable record; a citing conclusion is optional composition through the evidence-memory machinery.
- **Standalone entry rides Phase 3.** The fingerprint sample admission (Phase 3) is the room this front door opens into — the two fresh runs mint the observed samples; the claim-check reads them but never adds to them.
