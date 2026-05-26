---
name: hpc-campaign
description: "Drive one tick of a closed-loop campaign — the per-iteration submit-flow → monitor-flow → aggregate-flow loop whose tasks.py adapts to prior results. Autonomous: interprets validate-campaign findings deterministically (errors block; warnings proceed with a recorded note), composes hpc-submit for each iteration's submission. Callers supply campaign_id + path (A: manual grid, B: strategy-driven optimizer); the skill drives ticks. The /campaign-hpc slash invokes this skill after first-time path picking; an external autonomous agent invokes it per tick."
allowed-tools: Bash Read Skill
execution: inline
category: agent-autonomous
---

Agent-facing decision layer over the **[campaign](../../docs/primitives/campaign-advance.md) workflow**. This skill drives one tick of the campaign loop — submit a new iteration, monitor it, aggregate, decide whether to advance or stop. It composes `hpc-submit`, `hpc-status`, and `hpc-aggregate` for the per-tick mechanics, and interprets the `validate-campaign` static gate's findings before each submit.

## Inputs

| Field | Default behaviour if absent |
|---|---|
| `experiment_dir` | Required |
| `campaign_id` | Required — campaigns are tagged; the slug identifies which campaign this tick belongs to. |
| `path` | Default `"A"` (manual grid). Set `"B"` for strategy-driven (Optuna/random-search/PBT). Affects validation strictness — Path B requires `_optuna_trial_number` in kwargs. |
| `allow_warnings` | Default `true` — validate-campaign warnings (e.g., walltime below historical p95) proceed with a note. Set `false` to block on warnings too. |

## Mode

- **`mode: "interview"`** — caller passes user-resolved values; the slash walks the path/slug picker dialog before invoking.
- **`mode: "autonomous"`** (default) — auto-resolve everything. The skill respects `allow_warnings=true` by default; an autonomous caller that wants stricter validation passes `allow_warnings=false`.

## Steps

### 1. Load context

```bash
hpc-agent load-context --experiment-dir <experiment_dir>
```

Examine `data.campaigns[campaign_id].cursor` for the next iteration index. If the campaign doesn't exist, return `spec_invalid: unknown_campaign` with the list of known campaigns.

### 2. Determine the next step

The campaign driver (`hpc-campaign-driver`) is code-orchestrated; it advances exactly one step per invocation. Read its proposed next step:

```bash
hpc-campaign-driver --experiment-dir <dir> --allow-agent-steps --campaign-id <id>
```

It prints `{"delegate": <step>, "plan": <plan>}`. Branch on the step:

| `delegate.step` | Skill behaviour |
|---|---|
| `submit` | Steps 3-5 below (validate → submit). |
| `monitor` | Compose `hpc-status` skill with the campaign's latest in-flight run_id. |
| `aggregate` | Compose `hpc-aggregate` skill with the campaign's latest terminal run_id. |
| `decide` | The driver wants a judgement call on whether to continue. See Step 6. |

### 3. Validate the next iteration

Before any `submit`, run `validate-campaign`:

```bash
hpc-agent validate-campaign --spec spec.json --experiment-dir <dir>
```

Interpret findings by severity (deterministic — same logic as the `/campaign-hpc` slash):

| Severity | Skill behaviour |
|---|---|
| `error` | Return `spec_invalid: validate_campaign_failed` with the findings list. Block the submit. |
| `warning` | If `allow_warnings=true` (default), proceed and record the warnings in `decisions`. If `false`, return `spec_invalid: validate_campaign_warning` and block. |
| `info` | Always proceed; surface in `decisions` for visibility. |

Path B addendum: if path is `"B"` and the kwargs don't include `_optuna_trial_number` (or equivalent unique marker per the campaign's strategy library), `validate-campaign` flags `missing_stochastic_marker` as an `error`. The skill respects this — Path B without the marker silently dedupes iterations.

### 4. Compose hpc-submit for the iteration

Invoke the `hpc-submit` skill via the Skill tool with the campaign-tagged spec:

```json
{
  "experiment_dir": "<dir>",
  "campaign_id": "<id>",
  "mode": "autonomous"
}
```

The submit skill resolves cluster, entry_point, etc. (all the per-iteration HPC mechanics) and hands off to the submit-flow worker. Returns the new `run_id`.

### 5. Record the iteration

Update the campaign cursor:

```bash
hpc-agent campaign advance --campaign-id <id> --run-id <new-run-id> --experiment-dir <dir>
```

Return the new run_id + lifecycle state hint.

### 6. Handle `decide` steps

The driver surfaces a `decide` step when the campaign needs a judgement call — e.g., a budget gate ("we've used 80% of the cluster-hour budget; continue?") or a convergence gate ("the metric has plateaued for 5 iterations; stop?").

- **Interview mode**: return the `decide` envelope to the caller; the slash walks the question with the user.
- **Autonomous mode**: read the decide spec's `default_decision` field (the driver supplies a heuristic default). Honour the default and record in `decisions`. The autonomous caller can inspect the recorded decision and override on the next tick if needed.

If the driver supplies no default, return `spec_invalid: decide_required` with the question + context. (Autonomous mode without a default is a campaign-config oversight, not something the skill should guess.)

### 7. Return the envelope

Surface to the caller:
- `data.report.result` (current step, lifecycle, latest run_id, cursor position, decide outcome if applicable)
- `data.report.decisions` (validation findings handling, decide defaults applied)
- `data.report.anomalies`

## Notes

- **One tick per invocation.** The skill does NOT loop. The driver's design is one step per call; loop driving is the caller's job (cron, `/loop`, MARs's experiment-runner orchestration).
- **Composes hpc-submit, hpc-status, hpc-aggregate.** This skill is a coordinator; each per-step action delegates to the matching workflow skill.
- **Path B `_optuna_trial_number` is load-bearing.** Without a unique marker per iteration, `cmd_sha` is the same across ticks → the submit-flow primitive dedupes → the campaign silently collapses. The `validate-campaign` `missing_stochastic_marker` error is the hard gate.
- **Pause and resume.** State lives in sidecars + the campaign cursor on disk. Re-invoking the skill resumes from the cursor position; the previous tick's progress is preserved.
- **MARs experiment-runner pattern**: invoke per tick with `{experiment_dir, campaign_id, path, mode: "autonomous", allow_warnings: false}` (autonomous campaigns usually want stricter validation). Block on each tick; advance to the next tick when MARs's orchestration decides to.
