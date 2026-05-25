# Scheduler job states

Reference for [`/submit-hpc`](../../src/hpc_agent/_kernel/extension/worker_prompts/submit.md) Step 8b — after
`qsub` / `sbatch` returns a job ID, the job ID alone does not mean the array is
running. Classify each returned job ID against the live queue and the
accounting record before reporting success.

```bash
# SLURM
ssh $SSH_TARGET 'squeue -j '"$JOB_IDS"' -h -o "%i %T %r"; sacct -j '"$JOB_IDS"' -n -P -o JobID,State,Reason 2>&1 | head'
# SGE
ssh $SSH_TARGET 'qstat -j '"$JOB_IDS"' 2>&1 | head -40; qstat -u '"$USER"' | awk "NR>2"'
```

## Healthy — proceed

- **SLURM**: `PENDING`, `RUNNING`, `CONFIGURING`, `COMPLETING`. A wave-2+ job
  sitting at `PENDING` with `Reason=Dependency` is healthy — it is waiting on
  its predecessor wave.
- **SGE**: `qw`, `hqw`, `r`, `t`, `Rq`, `Rr`. A wave-2+ job at `hqw` is healthy.

## Failed — abort

Surface the scheduler reason verbatim, name the bad job ID, and stop.

- **SLURM**: `BOOT_FAIL`, `FAILED`, `NODE_FAIL`, `OUT_OF_MEMORY`, `TIMEOUT`,
  `DEADLINE`, `REVOKED`, `SPECIAL_EXIT` — or `CANCELLED` within seconds of
  submit.
- **SGE**: any state beginning with `E` (error) or `d` (deletion).
- **Either scheduler**: a job ID absent from *both* the live queue
  (`squeue` / `qstat`) *and* the accounting record (`sacct` / `qacct`) after one
  retry — the scheduler never registered it.
