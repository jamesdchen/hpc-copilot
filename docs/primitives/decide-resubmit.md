---
name: decide-resubmit
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent decide-resubmit --failed-count <failed_count> --total-tasks <total_tasks>
    [--resubmit-failed-threshold <resubmit_failed_threshold>]
  python: hpc_agent.ops.decide_resubmit.decide_resubmit
---
# decide-resubmit

Decide whether a terminal-with-failures wave should be marked complete,
auto-resubmitted, or escalated — from observable evidence rather than
prose. Backs the hpc-status `terminal_with_failures` decision point.

## Purpose

This lifts hpc-status Step 6's resubmit policy out of `SKILL.md` prose and
into code. The policy lived **only** in the skill's Markdown — there was no
implementation — so every status poll re-derived the same
`failed_fraction = failed / total` arithmetic and the same threshold branch
by hand. Now the agent calls one verb instead.

The split is part deterministic, part decision-as-data:

- `failed_fraction == 0` → nothing failed; the lifecycle is actually
  `complete`.
- `failed_fraction <= threshold` → `resubmit`. Only reachable when the
  caller opted in by passing a threshold > 0 — they declared how much loss
  an automatic re-run may absorb.
- `failed_fraction > threshold` → `escalate`. Under the default threshold
  of `0.0` this is every failure: auto-resubmitting can silently re-run
  the same bug. Rather than resubmit silently, the primitive surfaces the
  choice as data, carrying `safe_default: "investigate"`.

The threshold boundary is **inclusive**: `failed_fraction == threshold`
resubmits.

## Output

`{action, failed_count, total_tasks, failed_fraction, threshold,
safe_default, rationale}`:

- `action` — `complete` (nothing failed) / `resubmit`
  (`failed_fraction <= threshold`) / `escalate`
  (`failed_fraction > threshold`).
- `failed_fraction` — `failed_count / total_tasks`, rounded to 4 places —
  the evidence the threshold decision turns on (e.g. `5/100 = 0.05`).
- `threshold` — the `resubmit_failed_threshold` the decision was taken
  against.
- `safe_default` — `"investigate"` on `escalate` (don't auto-resubmit at a
  high failure rate); `null` on `complete` / `resubmit` (no judgement
  needed).
- `rationale` — human-readable explanation of the chosen action.

Pure function over supplied evidence; raises `SpecInvalid` only when
`total_tasks < 1` (a failed fraction over zero tasks is undefined).
