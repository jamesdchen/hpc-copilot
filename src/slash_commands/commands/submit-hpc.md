Invoke the `hpc-submit` skill via the Skill tool (`skills/hpc-submit/SKILL.md`) for the workflow: discover executors → plan axis → auto-configure env → throughput plan → smart constraint planner → write tasks.py + sidecar → preflight → validate-campaign → predict-start-time → submit-flow → verify scheduler accepted the array → record. The skill is the canonical SoT for the call sequence.

This slash command is the human-facing entry point. It carries four pieces of content the skill cannot:

## 1. Migration check (legacy `_hpc_dispatch.json`)

Before any of the priority checks, look for a top-level `_hpc_dispatch.json` (or `manifest.<sha8>.json`, or `manifest.json`) in the experiment dir. These are artifacts of the pre-`.hpc/tasks.py` model that no longer drive the framework. If present, surface a one-time migration message:

> "I found a legacy dispatch manifest at `_hpc_dispatch.json`. The framework no longer reads manifests — task definitions live in `.hpc/tasks.py` and per-run state in `.hpc/runs/<run_id>.json`. I'll walk you through writing `.hpc/tasks.py` once at Step 6 (using your existing manifest as a translation hint if helpful), then we can move the old manifest aside. OK to proceed?"

If the user agrees, continue to the suggest-setup-action priority ladder. The manifest's existing `tasks[*].cmd` and `tasks[*].params` are useful translation hints for the scaffolding sub-interview but are not consumed by the framework. Once `.hpc/tasks.py` is committed, suggest `git mv _hpc_dispatch.json .hpc/legacy/` (or simply delete it). Don't proceed silently — a stale `_hpc_dispatch.json` next to a fresh `.hpc/tasks.py` is confusing on inspection.

## 2. Suggest-setup-action user prompts

The skill calls `suggest-setup-action` and gets back `{action, candidates, ...}`. Render to the user per the action:

| `action` | User prompt |
|---|---|
| `monitor` | "Found in-flight runs: <list>. Resume monitoring with `/monitor-hpc`, or start a new submission?" — group by `campaign_id` if >3 runs. |
| `reuse` | "Recent submissions: <(profile, cluster) pairs from `candidates`>. Resubmit same, modify (edit `.hpc/tasks.py`), or start fresh?" |
| `interview` | "Found existing `.hpc/tasks.py` (axis already encoded). Skip the executor-discovery interview and go straight to the planner?" |
| `fresh` | (no prompt — fall through to full interview) |

For `reuse`: list distinct `(profile, cluster)` pairs from recent run sidecars so the user can pick "same as last `ml_ridge` submission" without re-answering interview questions. Each sidecar carries the full v2 config snapshot — resources, env, constraints, runtime — so reuse is a one-line copy.

## 3. Scaffolding sub-interview (when nothing exists yet)

### 3a. Whole-repo scaffolding — `build-template`

Before scaffolding individual executors, check whether the repo is set up as an experiment repo at all. If there is no experiment-template scaffold — no `.hpc/`, no `Makefile` that does `include .hpc/template.mk` — and the user is starting from a notebook (or wants the notebook → executor workflow), offer to bootstrap the whole repo first:

> "This repo isn't set up for hpc-agent experiments yet. Want me to scaffold it? `hpc-agent build-template` drops in a `Makefile`, a CI workflow (notebook-sync + serial-elision gates), a pre-commit re-export hook, a `pyproject.toml`, and the `.hpc/` experiment tooling. Existing root files are never overwritten without `--force`; the framework-owned `.hpc/` assets self-heal on every run."

`build-template` is a human-facing CLI command — `hpc-agent build-template [--repo-dir <dir>] [--force]` — deliberately *not* a wire primitive: the experiment-template flow is built around researcher-authored notebooks, so it is exclusive to this human entry point and absent from the integrator primitive catalog. After it runs: `make new-experiment NAME=<name>` scaffolds `experiments/<name>.ipynb`, the researcher fills in the `@register_run` function, `make export` produces the executor `.py`. Then re-run `discover-executors` and continue. Surface `data.needs_manual_merge` verbatim — when the repo already had a `pyproject.toml` the template fragment is dropped at `.hpc/pyproject-fragment.toml` for a hand-merge rather than clobbering theirs.

### 3b. Executor scaffolding sub-interview

When the skill's `discover-executors` step returns an empty list, pivot to a scaffolding sub-interview right here (this absorbs what `/build-executor-hpc` used to be):

1. Ask: "No executors found in `executors/` / `scripts/` / `src/`. Want me to scaffold one — what should it do?"
2. Walk the user through filling in `compute(args)` based on their description — model fit/predict, simulation step, data transform, etc.
3. Capture the flag set the user wants (this becomes that executor's entry in the FLAGS dict during the Step 6b interview).
4. Hand off to **hpc-build-executor** skill for the actual scaffold call.
5. Once the new file exists, hand back to **hpc-submit** skill which re-runs `discover-executors` and continues.

## 4. Co-tenant exclusion judgment (Step 4c-B `stressed_nodes`)

After `score-submit-plan` returns `stressed_nodes` for the chosen candidate, decide per-node whether to soft-exclude using `co_tenants` context — this is the human-judgment moment that no static threshold captures cleanly:

- Co-tenant has been running >12h **and** holds >50% of CPU/mem on the node ⇒ exclude (long-running heavy job; unlikely to clear before our submit completes).
- Co-tenant is recently-started or holds little of the node's resources ⇒ allow.
- Multiple co-tenants on a node with combined high resource share ⇒ exclude.

Build the resulting `--exclude=<node1>,<node2>,...` flag and pass it through to the skill's submit-flow spec. The slash command makes the call; the skill receives the result.

## 5. Submit-now vs wait dialog (Step 6d `predict-start-time`)

When the skill's `predict-start-time` returns `best_submit_offset_hours > 0`, render to the user:

> "Predicted total time to actual start: 45 min (submit now would be 4h). OK to wait?"

If they decline, proceed anyway (the skill submits now). If they accept, schedule the submit (or pause and let the operator resume manually). Surface uncertainty fields when populated (`predicted_iso_p10` / `predicted_iso_p90`) as "expected 45min, worst-case 4h" rather than a point estimate.

## Common Failure Modes (user-facing troubleshooting)

| Symptom | Cause | Fix |
|---|---|---|
| `Eqw` state (SGE) | Job error | `qmod -cj <JOBID>` or resubmit |
| `PENDING` (SLURM) for >30min | Resource unavailable | Check `sinfo`, try different partition |
| Memory exceeded | Exceeded mem limit | Resubmit with higher memory |
| Walltime exceeded | Exceeded time limit | Resubmit with longer walltime |
| ModuleNotFoundError | Env not set up | Check modules and conda_env |
| file transfer failure (rsync/scp) | SSH key issue | Check `ssh $SSH_TARGET hostname` first |
| `--features` not recognized | Executor doesn't support that arg | Check `--help`, update executor |

When the user mentions CLI arguments that the executor doesn't support (e.g., "sweep features=[har, pca]" but `--features` isn't in `--help`), flag it: "ml_ridge.py doesn't accept --features. Should I add it, or did you mean a different executor?"

## Args

`$ARGUMENTS` formats:

1. **Executor + axis description**: `"run ridge"`, `"all ML models"`, `"sweep horizons 1, 5, 25 on lightgbm"`, `"subgroup analysis with ridge and xgboost"` — the slash parses to `(executor_id, axis_shape)` tuples and hands to the skill.
2. **Flags**:
   - `--no-canary` — skip the 1-task canary submission. Default: canary-on; only skip when the user has already smoke-tested the pipeline within the session.
   - `--cluster <name>` — pin the target cluster (otherwise interactive).
   - `--campaign-id <slug>` — tag this submission as one iteration of a closed-loop campaign. Required when invoked as part of `/campaign-hpc`.
3. **Empty**: full interactive interview.
