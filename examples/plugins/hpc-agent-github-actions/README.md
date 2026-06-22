# hpc-agent-github-actions

An hpc-agent backend plugin that runs task-array fan-outs on **GitHub Actions
runners** instead of an SSH cluster. You orchestrate locally (campaign loop,
`tasks.py`, Optuna ask/tell); each submit fans out as a workflow run whose
matrix has one cell per task; results come back as artifacts.

This is a **pure-API backend** in the sense of
[`docs/proposals/crowd-compute-backend.md`](../../../docs/proposals/crowd-compute-backend.md):
no SSH, no shared filesystem. It plugs into the same registry seam as the
built-in SGE/SLURM backends.

## Install + configure

```bash
pip install -e examples/plugins/hpc-agent-github-actions
```

Copy [`workflow-template/fan-out.yml`](workflow-template/fan-out.yml) into your
experiment repo's `.github/workflows/`, then point the backend at it:

```bash
export HPC_GHA_REPO=owner/your-repo       # where the workflow lives + runs
export HPC_GHA_WORKFLOW=fan-out.yml
export HPC_GHA_REF=main
export GITHUB_TOKEN=ghp_...               # actions:write (dispatch) + actions:read (poll/pull)
```

In `clusters.yaml`, name it like any scheduler (the host's config validator
accepts any plugin-registered backend name):

```yaml
clusters:
  github-actions:
    scheduler: github-actions
```

## Running out of CI compute: account rotation

Set `HPC_GHA_POOL` instead of `HPC_GHA_REPO`/`GITHUB_TOKEN` to spread a campaign
across several accounts. When one returns a quota/billing `403`, the backend
advances to the next entry and re-dispatches — the campaign keeps going on your
other account at the next iteration boundary.

```bash
export HPC_GHA_WORKFLOW=fan-out.yml
export GH_TOKEN_A=ghp_aaa            # tokens stay in their own vars …
export GH_TOKEN_B=ghp_bbb
export HPC_GHA_POOL="me/exp=GH_TOKEN_A,other/exp=GH_TOKEN_B"   # … referenced by name
```

This works because the durable state is **local** (the Optuna study + the
completed-iteration sidecars), so switching accounts loses nothing — the next
batch just lands on the next account. Two things the backend handles for you:

- **Run ids are account-scoped.** `alive_job_ids` / `fetch_results` / `fetch_logs`
  **probe the pool**, so a batch that ran on account B is still polled and pulled
  from B even after rotation.
- **Only a quota/billing `403` rotates** (matched on `minutes` / `spending limit`
  / `billing` / …). A permissions `403` surfaces as a real error instead of
  silently burning through your accounts. Each rotation leaves an stderr
  breadcrumb.

Caveats: an **in-flight** run can't migrate — rotation takes effect on the *next*
dispatch, so switch at an iteration boundary (pull a running batch's results
before it rotates away). And Actions minutes bill to the **repo owner**, so each
pool entry must be a repo that account owns (a fork / separate push), not just a
collaborator on one shared repo.

## How it maps onto hpc-agent

| Scheduler concept | This backend |
|---|---|
| construct backend | `from_build_context` — ignores the SSH fields, reads `$HPC_GHA_*` / `$GITHUB_TOKEN` |
| `qsub`/`sbatch` an array | `_execute_command` POSTs `workflow_dispatch`, resolves the run id (the "job id") |
| per-task kwargs | the workflow resolves `resolve(HPC_TASK_ID)` from your `.hpc/tasks.py` on the runner — same as the SLURM dispatcher node-side |
| `qstat` liveness | `alive_job_ids` → `GET /actions/runs/{id}` status |
| post-submit health | `classify_scheduler_state` (`queued`/`in_progress` → alive; `failure` → error; `cancelled` → held) |
| result pull (rsync) | `fetch_results` → download + unzip the run's artifacts |
| stderr logs | `fetch_logs` → download the run's job-logs zip |

The submit override lives in **`_execute_command`**, not `submit_array_tracked`:
submit-flow's single-array path (`_make_single_array_submission`) calls
`_build_command` + `_execute_command` and parses `JOB_ID_REGEX` from stdout, and
that is the path a real submit takes.

## What works end-to-end vs. what still needs bridging

**Covered by the backend seam** (dispatch on backend hooks, no host edit):

- construction via `from_build_context` (config seam)
- the submit itself (`workflow_dispatch` + run-id resolution)
- liveness polling (`alive_job_ids`) used by status / monitor / reconcile
- post-submit health (`classify_scheduler_state`)

**Not behind a backend hook — submit-flow / monitor / aggregate assume SSH +
a shared filesystem**, so these need wiring on the host side (the same two
assumptions the proposal flags as "do not survive contact"):

- **submit-flow's prelude** — `_validate_ssh_target` → ssh preflight probe →
  `rsync_push` → `deploy_runtime`. There is no login node and no shared mount;
  the runner gets your code via `actions/checkout`. Bypass the prelude with
  `HPC_AGENT_SKIP_PREFLIGHT=1` and `HPC_AGENT_SKIP_RSYNC_DEPLOY=1` (and pass
  placeholder `ssh_target` / `remote_path` / `script` in the spec, which a
  pure-API backend ignores).
- **per-task result reads** — monitor/aggregate read result dirs over the shared
  FS. `fetch_results` is the replacement (download + unzip artifacts); wire it
  where the SSH path rsync-pulls. The `reduce` job in the workflow already
  combines on Actions and emits one small `reduced` artifact, so you usually
  pull that, not the N per-task ones.
- the `build_*_cmd` / `parse_*` / `stderr_log_path` **staticmethods** can't be
  implemented (a `@staticmethod` can't hold the authenticated client); the
  instance methods above replace them.

For most tuning loops the clean path is to **not route through submit-flow** at
all — drive the backend's dispatch/poll/fetch from your own local loop (propose
params → `_execute_command` dispatches → `alive_job_ids` polls → `fetch_results`
pulls → reduce → repeat). The framework's
[`code-driven-orchestration`](../../../docs/workflows/code-driven-orchestration.md)
doc documents this seam.

## Where the input data lives

The runners have no shared filesystem. `fetch_results` solves *data-out* (per-task
metrics → artifacts); this is the *data-in* half — the training set every trial
reads. **Only the compute needs it**: the orchestrator just proposes
hyperparameters and reduces metrics, so the dataset never touches the laptop /
cloud container. It's purely a runner-side staging concern.

### Store it as a GitHub Release asset

A dataset too big to commit (>100 MB) doesn't need a separate cloud account — put
it on a **release asset**. Downloads are free and unmetered (GitHub → runner, no
egress, no credentials beyond the workflow's own token), up to 2 GB per file.

```bash
# once, on the orchestrator — upload + version-tag the dataset:
gh release create data-v1 train.parquet
```

The flow:

```
orchestrator                  backend                        each runner
────────────                  ───────                        ───────────
gh release create data-v1     sends data_tag as a            prefetch: gh release download
  train.parquet               workflow_dispatch input        (once) -> actions/cache key=data-<tag>
HPC_GHA_DATA_TAG=data-v1      (or the workflow default)      task xN: cache restore
                                                             LOCAL_DATA_DIR=$WORKSPACE/data
```

1. **Upload + tag** the dataset as a release asset (`data-v1`).
2. **Pin the version** — set `HPC_GHA_DATA_TAG=data-v1` (the backend forwards it as
   the `data_tag` dispatch input), or rely on the workflow's `data_tag` default.
   Bump the tag (`data-v2`) when the data changes; the cache key changes with it.
3. **Each runner stages once** — the `prefetch` job runs `gh release download`
   **once** and warms `actions/cache`; the matrix `needs: [expand, prefetch]` and
   restores from GitHub's cache. So a 256-cell fan-out is **1 download**, then free
   co-located cache restores — not 256 downloads.

### Big files (e.g. 193 MB)

A 193 MB file is comfortable to *process* — runners have ~14 GB disk, 7 GB RAM
(16 GB on larger runners), 6 h/job, so loading it into pandas and training XGBoost
is routine. The only real constraints:

- **You can't commit it.** GitHub hard-blocks files >100 MB — hence the release
  asset (≤2 GB/file), where the bytes live outside git.
- **Don't re-download it on every cell.** The `prefetch` job + `actions/cache`
  make it 1 download per run; the 10 GB cache holds a 193 MB entry easily, and a
  tag-pinned dataset is reused across iterations (re-fetched only when the tag
  changes).

Sharding the data across cells is *not* an option for tuning — every trial trains
on the full dataset (only the hyperparameters differ), so caching the whole file
is the lever, not splitting it. (If 193 MB ever became painful, downsample into a
smaller pinned asset — an experiment-design choice, not infra.)

### Runner-side: the executor reads `LOCAL_DATA_DIR`

`LOCAL_DATA_DIR` is the dispatcher-contract data root; your executor already keys
off it (no GitHub-specific code):

```python
# .hpc/executor.py
import os, xgboost as xgb, pandas as pd
from hpc_agent.execution.mapreduce.metrics_io import read_kw_env, write_metrics

kw = read_kw_env()                                          # {"max_depth": 6, "eta": 0.3, ...}
df = pd.read_parquet(os.path.join(os.environ["LOCAL_DATA_DIR"], "train.parquet"))
booster = xgb.train({"max_depth": int(kw["max_depth"]), "eta": float(kw["eta"])},
                    xgb.DMatrix(df.drop(columns="y"), label=df["y"]))
rmse = ...                                                  # eval on a holdout
write_metrics(os.environ["RESULT_DIR"], {"objective": rmse})  # → task-<i> artifact → reduce
```

Pin the version: every campaign iteration must train on the *same* data for
metrics to compare — so keep `data_tag` fixed across a campaign, bumping it only
for a deliberate dataset change.

## Limits worth knowing

- A matrix is capped at **256 cells per run** and ~20 concurrent runners by
  default; for larger sweeps chunk into multiple dispatches.
- Standard runners are CPU-only, 6 h/job; results come back only as artifacts
  (default 90-day retention).

## Future: orchestrating from an ephemeral cloud container

A natural extension (recorded here, not yet implemented): run the orchestrator
itself in a Claude Code web container instead of a laptop. It inherits the same
two constraints, both already solved here:

- **Reachability** — the pure-API backend reaches GitHub over HTTPS, so a
  locked-down container that can't SSH a campus cluster can still drive a
  campaign (the network policy must allow `api.github.com`).
- **Ephemeral state** — the container is reclaimed after inactivity and re-cloned
  fresh, so the campaign state (`.hpc/runs/*.json`, `.hpc/campaigns/<id>/`, with
  `HPC_JOURNAL_DIR` pointed into the repo) must be committed back each iteration;
  `prior()` replays it on the next session. It's the account-rotation section's
  "local state is the source of truth" property — here that state just has to live
  in git, because the container doesn't persist.

Pieces to add when this is taken on: a SessionStart hook / `setup.sh` that
installs hpc-agent + this plugin + strategy deps, the config as environment
variables, and the network policy. Then the whole pipeline is HTTPS + git — no
laptop, no SSH, no shared filesystem.
See https://code.claude.com/docs/en/claude-code-on-the-web.

## Live validation (the #269 discipline)

The build sandbox has no `GITHUB_TOKEN` and blocks outbound network, so the REST
calls ship **unvalidated**. Before relying on it, run one real dispatch:

```bash
export HPC_GHA_REPO=owner/your-repo HPC_GHA_WORKFLOW=fan-out.yml GITHUB_TOKEN=ghp_...
python -c "
from hpc_agent_github_actions.backend import GitHubActionsBackend
b = GitHubActionsBackend('$HPC_GHA_REPO', 'fan-out.yml')
cp = b._execute_command(b._build_command('1-4', 'smoke', {'HPC_RUN_ID':'smoke','EXECUTOR':'true'}), {}, None)
print('run id:', cp.stdout, 'exit:', cp.returncode)
print('alive:', b.alive_job_ids([cp.stdout]))
"
```

The pure logic (`classify_scheduler_state`, `_parse_total`) needs no network and
is the part to unit-test.
