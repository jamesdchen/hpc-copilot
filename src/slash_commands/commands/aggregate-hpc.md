Use the **hpc-aggregate** skill (`skills/hpc-aggregate/SKILL.md`) for the workflow: which mode to pick (auto / cluster-reduce / combiner-only), how to handle partial aggregation, the `verify-aggregation-complete` invariant check, error envelope branching. The skill is the canonical SoT.

This slash command is the human-facing entry point. It exists for two reasons the skill alone doesn't cover.

## Core principle (human advice): Reduce Where the Data Lives

**Never move bulk result files to reach a Python env.** If the reduction is trivial (pandas concat, `optuna.tell()`, JSON dump) but the host with the data lacks the deps, install the deps on that host — a 30s `pip install` beats minutes of small-file scp/rsync.

Decision rule before any `scp`/`rsync` of results:

1. **Is the compute genuinely HPC-scale?** (GPU, >1 node, hours of CPU) → run on cluster, aggregate on cluster, pull summaries.
2. **Is the compute trivial?** (pandas, sqlite, scalar output) → run it wherever the data already sits. Install missing deps in place.
3. **Must data actually move?** → move the *small* side (params/code down, reduced output up). Never bulk-push raw chunks between clusters to reach an env.

Anti-pattern: `scp -r results/tune/*_chunk_*.csv cluster-B:...` because cluster-B has the conda env and cluster-A doesn't. Fix the env, not the data location.

Small-file scp/rsync over SSH is especially slow (per-file TCP/SSH handshake). If bulk movement is truly unavoidable, `tar` first.

The skill's `mode: "auto"` default is what routes around this — it picks `cluster-reduce` when the sidecar declares an `aggregate_cmd` (small JSON output) and only pulls summaries when explicitly asked. Stay on the default unless a specific debug case requires the raw files locally.

## Post-flight spot-checks (human-driven)

`aggregate-flow` returning `ok=true` is necessary but not sufficient. The "file count lies" failure mode: `summary.complete == total_tasks` says every task wrote SOMETHING, but doesn't verify the file is non-trivial. Three checks the human should run after the skill returns:

### 4a.1 — Non-empty rows

Re-invoke the [poll-run-status](../../docs/primitives/poll-run-status.md) primitive's underlying cluster-side reporter with `--min-rows N` (a flag of the on-cluster `python -m claude_hpc.mapreduce.reduce.status` script that the primitive wraps; see `docs/reference/python-api-contract.md` for the cluster-side script's args). `N` is a profile-appropriate floor (1 minimum, more if the profile knows the expected row count). Any task that previously read `complete` but flips to `failed` here had an empty/short result file. Report which task IDs failed.

### 4a.2 — Spot-check 3 tasks

Pick the first, middle, and last task IDs (`0`, `task_count // 2`, `task_count - 1`). For each, read the head of its result file and verify:

- The file exists and is non-empty.
- Expected columns are present (use `results.summary_pattern` and the executor's known schema).
- Key metric column has at least one non-NaN value.

### 4a.3 — Sanity-check the aggregated metrics

`aggregated_metrics` is a dict keyed by run_id or grid-point. Confirm the keys match what the user submitted (no missing grid points; no unexpected ones). Keys present in the dict but absent from `tasks.resolve(i)` for any `i ∈ [0, total_tasks)` are a contamination red flag — escalate.

If any of 4a.1 / 4a.2 / 4a.3 fail, do NOT report success. The fix cost is tractable; reporting bad numbers is not.

## Args

`$ARGUMENTS` formats:

1. **Profile + stage**: `<profile_name>` or `<profile_name>/<stage_name>`
2. **Empty**: auto-discover which profiles/stages have completed results ready for aggregation

## Notes

- The skill handles the orchestration; this slash command's value is the **human advice** above. If the chat session is short and the user trusts the framework, "use the hpc-aggregate skill" alone is sufficient. The anti-pattern + post-flight sections are for the cases where the user needs to understand *why* the default flow is shaped the way it is.
