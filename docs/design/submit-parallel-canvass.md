# Design: parallel canvassing during worker startup

> **Status:** implemented. Tracks
> [#286](https://github.com/jamesdchen/hpc-agent/issues/286).
> Shipped: `src/slash_commands/commands/submit-hpc.md` (the parallel-startup
> flow). Slash-side only — the `hpc-submit` skill and the worker contract
> are unchanged.

## Problem

`/submit-hpc` dispatched the `hpc-submit` skill synchronously and the main
agent blocked until the skill (and its handed-off worker) returned. Worker
startup is slow — `load-context`, the round-trip ssh probes, the rsync
deploy, the cluster-side env-activation probe. A live 2026-06-05 submit spent
**2m22s** in worker startup with the main loop idle.

The main thread has no work that *reads* worker output during that window,
but it does have work that runs **ahead of** any worker output:

- **User-facing canvassing.** The runtime-behaviour questions
  (`overwrite_prior_run`, `on_task_generator_mismatch`, the `data_axis`
  confirmation when the classifier is `unclassifiable`, `k_in_flight`) are
  *predictable* — you know you'll likely ask them before the skill runs.
- **Local config validation.** `clusters.yaml` coherence, `.hpc/axes.yaml`
  freshness, working-tree dirtiness — all laptop-only.
- **Recent history surfacing.** Last N journal entries / prior `metrics.json`.

The old slash either pre-elicited everything (a long pause *before* the worker
started) or surfaced the questions one-by-one *after* the worker failed. Both
serialise human-thinking time behind worker-startup time. This design overlaps
them.

## Mechanism

```
              ┌─ background: Skill(hpc-submit) autonomous ─ preflight → rsync → canary ─┐
parse args ──►┤                                                                         ├─► join → surface
              └─ foreground: canvass runtime Qs + validate local config + history ──────┘
```

1. **Fork.** Dispatch the `hpc-submit` skill in the background (Claude Code's
   `Agent` tool, `run_in_background: true`) in **autonomous mode** — it applies
   each ambiguity's `safe_default` instead of returning `needs_resolution`,
   exactly as a non-interactive caller (MARs) would. Simultaneously, in the
   foreground, canvass the predictable runtime questions and run the local
   probes.
2. **Join.** Await the background task. On the fast paths (preflight cached,
   deploy cache hit) it has already returned; the await is immediate.
3. **Cascade.** Any field the autonomous path could not `safe_default` (a
   greenfield `entry_point`, a missing `task_generator`) comes back as a
   `needs_resolution` ambiguity; walk the matching dialog and re-invoke. The
   parallel flow is a *superset* of the old `needs_resolution` dialog walk, not
   a replacement.

## The speculative-dispatch bet

The background dispatch builds the spec and starts the deploy from the safe
defaults *before* the user has answered. This is safe because **most canvassed
questions are runtime-behaviour knobs, not spec-build inputs**:

| Question | Affects the built spec? | Join outcome |
|---|---|---|
| `k_in_flight` (concurrency cap) | no — scheduler throttle, not the task set | fold in |
| `overwrite_prior_run=overwrite` | no — the dispatch already assumed it could claim the `cmd_sha` | fold in |
| `on_task_generator_mismatch=prefer-caller` | no — submits the caller generator as-is | fold in |
| `data_axis` = the classifier's `Sequential` safe_default | no — already what the dispatch used | fold in |
| `on_task_generator_mismatch=refresh` | **yes** — rewrites `tasks.py` | **conflict** → cancel + re-dispatch |
| `data_axis` overridden to a non-`Sequential` kind | **yes** — changes the array decomposition | **conflict** → cancel + re-dispatch |
| `overwrite_prior_run=keep` | **yes** — no submit at all | **conflict** → cancel + route to monitor/aggregate |

On a conflict the slash cancels the background task and re-invokes the skill
(foreground) with the corrected, now-fully-resolved spec. **The cancel is
cheap by construction:** a cancelled dispatch has done preflight + maybe started
rsync, but not the main-array `qsub` — the canvassing (seconds of human
thinking) finishes well inside the deploy window, so the cancel lands before
the irreversible commit.

## Why slash-side only

The worker contract (`hpc-agent run --workflow submit`) was designed assuming a
fully-resolved spec on entry, with the Step-3 questions resolved upstream by the
slash. Moving the canvassing parallel-to-worker changes *when* the slash asks,
not *what* the worker is handed. So the `hpc-submit` skill, the worker prompt,
and `scripts/count_llm_touchpoints.py`'s baseline (which measures
`worker_prompts/`, not the slashes) are all untouched. The lint that pairs each
slash with its skill (`scripts/lint_skill_command_sync.py`) still passes: the
slash keeps the `Invoke the \`hpc-submit\` skill` directive and routes through
the inline skill, never shelling `hpc-agent run` itself.

## When the overlap is skipped

The fork only pays when worker startup is actually slow. When there is nothing
to overlap — no questions to ask (the user pre-stated everything) **and** a warm
fast path (preflight cached, deploy cache hit) — the slash runs the simple
synchronous path. Backgrounding a sub-second dispatch just adds a join.

## Scope and ports

Piloted on `/submit-hpc`, then ported (same shape) to:

- **`/aggregate-hpc`** — `load-context` + reconcile + the cluster pull (worker)
  ∥ the local results-tree summary + the `allow_partial` canvass (main thread).
- **`/monitor-hpc`** — the poll loop (worker) ∥ the journal-snapshot summary +
  the `high_failure_rate_action` canvass (main thread).

## See also

- [`submit-sequence.md`](../internals/submit-sequence.md) — the end-to-end
  slash → skill → worker walkthrough (the *Variant: parallel startup* note).
- [`skill-policy.md`](../internals/skill-policy.md) — the interview / decision /
  execution layering the parallelism respects.
