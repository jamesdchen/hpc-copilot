Help me submit HPC jobs via SSH using the project configuration.

All cluster commands run remotely via SSH. Code is synced from the local machine before submission.

## Setup

Read both config files:
- `hpc.yaml` in the current working directory
- `clusters.yaml`: resolve path via `python -c 'from hpc._config import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "config" / "clusters.yaml")'`

Construct `SSH_TARGET` (`user@host`) and `REMOTE_PATH` from hpc.yaml + clusters.yaml. If `$ARGUMENTS` contains `--cluster <name>`, use that cluster instead of the default.

## SSH Quoting

Single-quote the remote command so variables expand on the cluster, not locally:

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && echo $SGE_TASK_ID'
```

## Step 1: Load and Validate

1. Read `hpc.yaml` and validate it
2. If `.hpc/experiments.yaml` exists, read it for experiment context and registries
3. **Profile selection**: if `profiles` key exists, list available profiles. Accept from `$ARGUMENTS` or ask which to run.
4. **Stage selection**: if the selected profile has a `stages` key, list stages. Accept from `$ARGUMENTS` or ask which to submit. For single-stage profiles, the stage is implicit.
5. When CLI argument details are needed, invoke the executor with `--help` directly rather than relying on cached data.

## Step 2: Expand Grid and Show Run Plan

Compute the Cartesian product of the selected stage's `grid` parameters. Display:

```
Profile: ml
Grid: model=[ridge, xgboost] × features=[har, pca] × seed=[1, 2, 3]
Grid points: 12
Chunks per point: 100 (from chunking.total)
Total HPC tasks: 1200

Sample commands:
  Task 0: python3 -m my_experiment.train --model ridge --features har --seed 1 --chunk-id 0 --total-chunks 100
  Task 1: python3 -m my_experiment.train --model ridge --features har --seed 1 --chunk-id 1 --total-chunks 100
  ...
```

If no `chunking` section, each grid point is one task.

If no `grid` section (single job stage in a multi-stage profile), show the single command that will run.

**Multi-stage**: if the stage has `depends_on`, verify the dependency stages completed by checking for their result files on the cluster. If incomplete, report and ask whether to proceed.

Ask the user to confirm the run plan before proceeding.

## Step 3: Generate Dispatch Manifest

Use `hpc.grid.build_task_manifest()` to generate a `_hpc_dispatch.json` file locally. This JSON maps each task ID (0-based) to its full command string and result directory.

Also copy `hpc/dispatch.py` to `_hpc_dispatch.py` in the project root.

## Step 4: Sync to Cluster

Push local code + dispatch files to the cluster:

```bash
rsync -az --delete \
    --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' --exclude='hpc/' \
    # ... add each entry from hpc.yaml rsync_exclude as --exclude='<pattern>' ...
    . $SSH_TARGET:$REMOTE_PATH/
```

Deploy the `hpc` runtime package so `from hpc.chunking import chunk_context` works on the cluster. Resolve `claude-hpc` root via `python -c 'from hpc._config import _PACKAGE_ROOT; print(_PACKAGE_ROOT)'`, then:

```bash
ssh $SSH_TARGET 'mkdir -p '"$REMOTE_PATH"'/hpc && touch '"$REMOTE_PATH"'/hpc/__init__.py'
scp $HPC_ROOT/hpc/chunking.py $SSH_TARGET:$REMOTE_PATH/hpc/chunking.py
```

Verify deployment:
```bash
ssh $SSH_TARGET 'ls '"$REMOTE_PATH"'/_hpc_dispatch.json '"$REMOTE_PATH"'/_hpc_dispatch.py '"$REMOTE_PATH"'/hpc/chunking.py'
```

## Step 5: Submit

Determine the template from resources (GPU present → `gpu_array`, else `cpu_array`).

Resolve environment variables: use profile's `env`, or look up `cluster_envs[cluster][env_group]` if `env_group` is set.

Build env vars:
- `EXECUTOR=python3 _hpc_dispatch.py`
- `HPC_MANIFEST=_hpc_dispatch.json`
- `REPO_DIR=<remote_path>`
- `MODULES=<env.modules>`
- `CONDA_SOURCE=<cluster.conda_source>` (if conda_env set)
- `CONDA_ENV=<env.conda_env>` (if set)
- `TOTAL_CHUNKS=<total_tasks>`

### SGE Submission

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && qsub \
    -t 1-<total_chunks> \
    -N <job_name> \
    -o logs -j y \
    -l <resource_key>=<resource_val> \
    ... \
    -v CONDA_SOURCE=...,CONDA_ENV=...,MODULES=...,EXECUTOR=...,TOTAL_CHUNKS=... \
    <template_path>'
```

For GPU stages: `-l gpu,<gpu_type>,cuda=<count>`.

### SLURM Submission

```bash
ssh $SSH_TARGET 'cd '"$REMOTE_PATH"' && sbatch \
    --array=1-<total_chunks> \
    --job-name=<job_name> \
    --output=logs/%x_%A_%a.out \
    --error=logs/%x_%A_%a.err \
    --mem=<mem> --time=<walltime> --cpus-per-task=<cpus> \
    --export=CONDA_SOURCE=...,CONDA_ENV=...,MODULES=...,EXECUTOR=...,TOTAL_CHUNKS=... \
    <template_path>'
```

For GPU stages: `--gres=gpu:<count>` and appropriate partition.

## Step 6: Report

After submission:
1. Parse the job ID from submission output
2. Report: job ID, profile, stage (if multi-stage), total tasks, grid dimensions, cluster
3. **Multi-stage**: note which stages are now unblocked by this completion
4. Suggest running `/monitor` to track progress

## Common Failure Modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Eqw` state (SGE) | Job error | `qmod -cj <JOBID>` or resubmit |
| `PENDING` (SLURM) for >30min | Resource unavailable | Check `sinfo`, try different partition |
| Memory exceeded | Exceeded mem limit | Resubmit with higher memory |
| Walltime exceeded | Exceeded time limit | Resubmit with longer walltime |
| ModuleNotFoundError | Env not set up | Check modules and conda_env in config |
| rsync failure | SSH key issue | Check `ssh $SSH_TARGET hostname` first |
