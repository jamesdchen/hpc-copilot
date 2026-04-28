---
name: hpc-status
description: "Poll the status of an in-flight HPC run and return a one-shot snapshot."
allowed-tools: Bash Read Write
---

One-shot status poll for a `run_id` produced by `hpc-submit`. Returns lifecycle state, last status snapshot, and combined/failed wave lists. Does not loop — caller decides cadence.

## Steps

1. If `run_id` is unknown, list in-flight runs:
   ```bash
   hpc-mapreduce list-in-flight --experiment-dir <path>
   ```
   Pick the matching `data.runs[].run_id` (filter by `profile`, `cluster`, or `submitted_at`).

2. Poll status:
   ```bash
   hpc-mapreduce status --run-id <rid> --experiment-dir <path>
   ```

3. Parse the envelope. On `ok: true`, read `data`:
   - `lifecycle_state` — one of `in_flight`, `complete`, `failed`, `abandoned`. Drives the next action.
   - `last_status` — most recent scheduler snapshot (per-task counts: `complete`, `running`, `pending`, `failed`).
   - `combined_waves` — list of wave numbers already aggregated.
   - `failed_waves` — list of wave numbers whose combiner failed.

4. Decide next action:
   - `lifecycle_state == "in_flight"` and `last_status.complete < total_tasks` — caller should wait and re-poll later.
   - `lifecycle_state == "in_flight"` and a wave number appears in `last_status` as fully complete but not in `combined_waves` — call `hpc-aggregate` for that wave.
   - `lifecycle_state == "complete"` — terminal; proceed to final aggregation if any waves remain uncombined.
   - `lifecycle_state == "failed"` or `failed_waves` non-empty — surface to caller; consider `hpc-mapreduce resubmit` or `hpc-mapreduce reconcile`.
   - `lifecycle_state == "abandoned"` — recorded jobs no longer exist on the scheduler. Run reconcile to confirm:
     ```bash
     hpc-mapreduce reconcile --run-id <rid> --scheduler {sge|slurm} --experiment-dir <path>
     ```

5. On error envelopes:
   - `journal_corrupt` (internal) — `run_id` not in journal. Re-run `list-in-flight`; verify the caller passed the correct id.
   - `ssh_unreachable` (network, retry_safe: true) — retry after preflight passes.
   - `remote_command_failed` (cluster) — surface stderr; the reporter on the cluster failed.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` in the spawned env or this call hangs on auth. Run `hpc-preflight` first.
- **One-shot only**: this skill returns a single snapshot. Do NOT loop status calls in tight cadence — sleep at least 60s between polls (300s for runs >30 min ETA). Schedulers and SSH multiplexers throttle aggressive polling.
- **No cancel/abort**: claude-hpc has no kill command. Receiving `lifecycle_state == "in_flight"` for a bad experiment means the cluster jobs continue to walltime; the caller can stop monitoring but cannot terminate.
- Idempotent on the journal write side — multiple status calls update `last_status` in place under a flock.
- Exit codes: 0 ok, 2 cluster/network (retry per `retry_safe`), 3 internal (`journal_corrupt`).
