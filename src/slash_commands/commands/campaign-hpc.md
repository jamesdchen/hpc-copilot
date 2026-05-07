Invoke the `hpc-campaign` skill via the Skill tool (`skills/hpc-campaign/SKILL.md`) for the workflow: campaign tagging, the per-iteration `submit-flow → monitor-flow → aggregate-flow` triplet, the stochastic-marker requirement for Path B (strategy-driven) campaigns, the resume-after-drop semantics. The skill is the canonical SoT.

This slash command is the human-facing entry point. It exists for two reasons the skill alone doesn't cover:

1. **Pick the path** in conversation with the user (Path A: manual params, vs Path B: Optuna/random-search/PBT strategy). The skill describes both; the slash command's job is to ask "is your search space small and known, or large and adaptive?" and route accordingly.

2. **Drive the per-iteration loop** as a Claude Code chat (the alternative is a `bash .hpc/campaigns/<slug>/iterate.sh` cron loop, which is what the skill's "headless" pointer covers). The slash command is the chat-driven path: each `/campaign-hpc` invocation is one iteration; the user kicks the next one when they want.

## When the user asks "start a campaign"

1. Ask which path:
   - "Do you have a fixed grid you want to step through (walk-forward windows, ablations, manual hyperparam sweep)? → Path A."
   - "Do you want an optimizer to choose params adaptively (Optuna, random-search, PBT)? → Path B."

2. **Path A**: walk the user through writing `tasks.py` with the manual grid. `resolve(task_id)` enumerates the grid; `total()` returns its size. Each iteration submits a fixed slice. No stochastic marker needed (param tuple itself is unique per iteration).

3. **Path B**: walk the user through writing `tasks.py` with the strategy library. Inside `total()` / `resolve()`, the user calls:
   - `study.tell(prev_trial, prev_metric)` for each prior iteration (loaded via `prior(experiment_dir, campaign_id)`)
   - `study.ask()` to get the next batch
   - **Add `_optuna_trial_number` (or equivalent unique field) into the kwargs dict** so each iteration's `cmd_sha` differs even when the strategy picks repeat params. Without this, the framework dedupes the second iteration silently and the campaign collapses.

4. Tag the slug: ask "what should we call this campaign?" and validate against `^[A-Za-z0-9._\-]+$`.

5. Call into `hpc-submit` with `campaign_id=<slug>` set. The skill takes over from there.

## When the user asks "show me what landed"

Invoke `hpc-agent campaign list` first; if more than one campaign exists, ask which. Then `hpc-agent campaign status --campaign-id <slug>` and surface the per-iteration history. Group multiple in-flight runs by `campaign_id` when displaying — easier to scan than a flat list.

## Notes

- **The `_optuna_trial_number` requirement is load-bearing for Path B.** Surface it explicitly to the user when their `tasks.py` doesn't have one — silently-deduped iterations are a notorious debugging nightmare.
- **Concurrency**: ask "do you want one iteration in flight at a time, or multiple?" Default to sequential. Multiple is the right answer for Optuna with `constant_liar=True`; sequential is right for walk-forward where iteration N+1 depends on N's result.
- **Pause and resume**: closing the chat or hitting a network drop doesn't lose state — sidecars on disk are the only durable artifact. Re-run `/campaign-hpc` and the skill's `campaign-status` call shows where you left off.
