---
name: combine-wave
verb: mutate
inputs:
- name: run_id
  type: string
- name: wave
  type: int
  description: Wave index from the per-run sidecar's wave_map.
- name: experiment_dir
  type: path
  description: Repo root. Defaults to cwd.
- name: output_dir
  type: path
  description: Combined-output destination on the cluster.
  default: <experiment_dir>/_aggregated/<run_id>/
- name: force
  type: bool
  description: Re-run combiner even if wave appears in combined_waves.
  default: false
side_effects:
- ssh: <cluster>
- runs: cluster-side combiner (python3 .hpc/_hpc_combiner.py)
- writes-cluster: <output_dir>/_combiner/wave_<N>.json
- writes-journal: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json (combined_waves / failed_waves)
idempotent: true
idempotency_key: (run_id, wave)
error_codes:
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: combiner_failed
  category: cluster
  retry_safe: false
  description: Cluster-side combiner exited non-zero; surface stderr_tail.
- code: journal_corrupt
  category: internal
  retry_safe: false
backed_by:
  cli: hpc-agent aggregate [--experiment-dir <dir>] --run-id <run_id> --wave <wave>
    [--force] [--require-outputs <require_outputs>] [--expect-output <expect_output>]
  python: hpc_agent.ops.aggregate.combine.combine_wave
exit_codes:
- 0: combined successfully
- 1: spec_invalid
- 2: combiner_failed / ssh_unreachable
- 3: journal_corrupt
---

## Purpose

Run the on-cluster combiner for one wave: aggregate per-task partial reduce JSONs into a wave-level partial, ready for final cross-wave aggregation. The wrapper records `combined_waves` / `failed_waves` to the journal atomically — slash commands MUST go through this primitive rather than calling `state.journal.update_run_status` directly for those fields.

## Compose with

- Common predecessors: `poll-run-status` (to discover newly-complete waves).
- Common successors: another `combine-wave` (next wave) or final aggregation when every wave is in `combined_waves`.

## Notes

- 1st failure on a wave: retry on the next monitoring tick with `force=true`.
- 2nd failure: stop retrying; this is the escalation point for `/monitor-hpc` to surface to the user.
- The slash-command surface (`/aggregate-hpc`) wraps this primitive in a "do every uncombined wave, then download summaries" flow — that recipe is surface logic, not part of this primitive.
