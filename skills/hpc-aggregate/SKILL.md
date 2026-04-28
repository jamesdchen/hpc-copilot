---
name: hpc-aggregate
description: "Run the on-cluster combiner for a wave and record the outcome to the run journal."
allowed-tools: Bash Read Write
---

Invoke the on-cluster combiner for one wave of a run, pull the combined artifacts to a local output dir, and persist the outcome to the journal. Idempotent on success per wave; failure is retry-safe.

## Steps

1. Verify the wave is ready. Call `hpc-status` first; only aggregate waves that appear fully complete in `last_status` and are NOT already in `data.combined_waves`. If the wave is in `failed_waves`, set `--force` deliberately (Step 3) to retry.

2. Choose an output directory if the default (`<experiment-dir>/_aggregated/<run_id>/`) is not desired. The combiner pulls partials into `<output-dir>`.

3. Run aggregate:
   ```bash
   hpc-mapreduce aggregate --run-id <rid> --wave <N> --experiment-dir <path>
   ```
   Add `--output-dir <dir>` to override the destination. Add `--force` to re-run a wave already in `combined_waves` or `failed_waves`.

4. Parse the envelope. On `ok: true`:
   - `data.combined: true` — wave aggregated successfully. Journal updated; the wave is now in `combined_waves`.
   - `data.combined: false` — combiner ran but reported failure. Journal updated; the wave is in `failed_waves`. Inspect `data.stderr_tail` (last 2000 chars) and `data.stdout_tail`.
   - `data.output_dir` — absolute path where artifacts landed.

5. On `data.combined: false`, decide retry policy:
   - First failure: re-run with `--force` once.
   - Second consecutive failure: surface to caller with the stderr tail; do NOT auto-retry. The combiner script likely has a bug.

6. On error envelopes:
   - `journal_corrupt` — `run_id` not found. Stop.
   - `manifest_invalid` (user) — `--wave` missing or non-int. Fix arguments.
   - `ssh_unreachable` (network, retry_safe: true) — retry after preflight.
   - `remote_command_failed` (cluster) — same as `data.combined: false`; treat as combiner failure.

7. After the last wave is combined (cross-reference with `wave_map` from the manifest, or with `hpc-status` output), the caller may mark the run terminal via `hpc-mapreduce` lifecycle helpers (out of scope for this skill).

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` in the spawned env or this call hangs on auth. Run `hpc-preflight` first.
- **No cancel/abort**: aggregate runs the user's combiner script on the cluster; once started, it cannot be stopped from here. Set sensible walltimes in the combiner job itself.
- Successful aggregation is idempotent per wave: re-running the same `--wave N` after success is a no-op (envelope reports `idempotent: true`). Failure is retry-safe — re-run with `--force`.
- The CLI does NOT choose the combiner script or output schema; the user's repo provides `_hpc_combiner.py`. This skill only orchestrates the call and records outcomes.
- Exit codes: 0 ok (whether combined or not — failure is captured in `data.combined`), 1 user error, 2 cluster/network (`ssh_unreachable`, etc.), 3 internal.
- Aggregating waves out of order is permitted; the journal tracks each wave independently.
