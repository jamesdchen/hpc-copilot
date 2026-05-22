`/campaign-hpc` triggers the **campaign** workflow — drive a closed-loop campaign (tagged `submit-flow → monitor-flow → aggregate-flow` iterations whose `tasks.py` adapts to prior results).

This command is a thin trigger over `hpc-campaign-driver`, the code-orchestrated campaign loop. Do not run the `hpc-campaign` skill yourself in this conversation, and do not hand-write a per-step subagent prompt — the driver advances exactly one step per invocation, running deterministic steps directly and spawning a fresh-context worker for judgement steps. The `hpc-campaign` skill (`skills/hpc-campaign/SKILL.md`) stays the canonical SoT for campaign semantics.

Two things this command does in-conversation, because the driver can't:

1. **Pick the path** (first-time setup only). Ask the user: "Do you have a fixed grid to step through — walk-forward windows, ablations, a manual sweep? → Path A. Or do you want an optimizer to choose params adaptively — Optuna, random-search, PBT? → Path B." Walk them through writing `tasks.py` accordingly. For Path B, the `_optuna_trial_number` (or equivalent unique marker) in `tasks.resolve()`'s kwargs is load-bearing — without it the framework silently dedupes repeat-param iterations and the campaign collapses. The skill's mandatory `validate-campaign` gate enforces this; surface the requirement to the user up front.

2. **Tag the campaign**: ask "what should we call this campaign?" and validate the slug against `^[A-Za-z0-9._\-]+$`.

## Driving the loop

Once `tasks.py` and the slug are set, each `/campaign-hpc` invocation advances exactly one step:

1. Run, via the `Bash` tool: `hpc-campaign-driver --experiment-dir . --allow-agent-steps`. It reads the on-disk `delegate` block emitted by `load-context` and executes the next step — a deterministic `monitor` / `aggregate` directly, or a judgement `submit` / `decide` in a fresh-context worker — then prints `{"delegate": ..., "plan": ...}`.
2. Surface the printed `plan` and the step's result to the user: which step ran, the `run_id`, the lifecycle state or reduced metrics, and whether the campaign has more iterations queued.
3. The user kicks the next iteration when ready. For unattended runs, point them at `/loop 30m hpc-campaign-driver --experiment-dir . --allow-agent-steps` or a cron wrapper — each tick is one step, and on-disk state is the only thing carried between ticks.

## When the user asks "show me what landed"

Run `hpc-agent campaign list` first; if more than one campaign exists, ask which. Then `hpc-agent campaign status --campaign-id <slug>` and surface the per-iteration history. Group multiple in-flight runs by `campaign_id` — easier to scan than a flat list.

## Notes

- **Pause and resume is trivial.** There is no driver state to recover — sidecars on disk are the only durable artifact. Re-run `/campaign-hpc` (or `hpc-agent campaign status`) and the driver resumes from where it left off.
- **Concurrency** is opt-in: for K iterations in flight (Optuna's `constant_liar=True` is built for this), the campaign's `tasks.py` and submit cadence control it. Default to sequential when in doubt — walk-forward iteration N+1 depends on N's result.
