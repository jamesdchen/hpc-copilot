---
name: validate-walltime-against-history
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-mapreduce validate-walltime-against-history --spec <path>
  python: claude_hpc.atoms.validate_walltime_against_history.validate_walltime_against_history
---
# validate-walltime-against-history

Cross-reference requested walltime against runtime priors and project playbook rules. Three rule families: (1) compare requested walltime against historical quantiles (e.g., warn if below p95), (2) check for known-bad GPU/workload combinations from `.hpc/playbook.yaml`, and (3) flag cold-start (no historical samples) with info-level findings so the agent knows the lack of warning is "no data," not "all clear."

## Inputs

- `profile` (string) — Profile key (matches the runtime-prior pool the validator reads).
- `cluster` (string) — Cluster key.
- `requested_walltime_sec` (integer) — Requested wall-time in seconds.
- `gpu_type` (string, optional) — GPU type (e.g., `"a100"`). Required for quantile and known-bad checks.
- `workload_tags` (list of strings, default `[]`) — Project-specific tags (e.g., `"attn-fp32"`, `"mixed-precision"`) looked up against playbook known-bad combos. Empty list disables playbook lookup.

## Outputs

A `ValidateWalltimeAgainstHistoryResult` object with:

- `findings` (list of `ValidatorFinding` objects) — Empty list = pass. Each finding includes:
  - `validator` — `"validate-walltime-against-history"`
  - `severity` — `"error"`, `"warning"`, or `"info"`
  - `code` — Machine-readable error code.
  - `message` — Human-readable description.
  - `suggested_fix` — Actionable hint (increase walltime to X seconds, etc.).
  - `evidence` — Raw values (requested_walltime_sec, quantile_label, quantile_sec, n_samples, gpu_type, workload_tag, etc.).

## Errors

None declared on the primitive. Findings carry the diagnostic code instead; common `code` values:

- `playbook_parse_error` (error) — `.hpc/playbook.yaml` is malformed.
- `cold_start_no_history` (info) — no runtime samples for (profile, cluster, gpu_type); the walltime-quantile check is skipped and submission proceeds. The first run produces baseline samples.
- `walltime_below_quantile` — requested walltime below the configured quantile threshold (default rule: warn below p95). Severity inherited from the playbook rule.
- `known_bad_combination` — (gpu_type, workload_tag) pair matches a recorded "do not use" entry in the playbook. Severity inherited from the rule.

## Idempotency

The validator reads runtime priors and the playbook; calling twice with the same priors and playbook produces the same findings.

## Notes

- **Default rule**: When `.hpc/playbook.yaml` declares no `walltime_rules`, the framework applies a built-in default: warn if `requested_walltime_sec < p95`. This rule mirrors the lesson that walltime below historical p95 is the strongest correlate of in-flight TIMEOUT.
- **Configurable per-project**: Edit `.hpc/playbook.yaml` to adjust quantile thresholds, add/remove known-bad combos, and inherit changes across all campaigns without code changes.
- **Cold-start handling**: On the first submission for a (profile, cluster, gpu) tuple, the validator emits an info-level finding so the agent is aware data is sparse. The submission proceeds; subsequent runs populate the prior.
- **Known-bad combos**: Entries in playbook.yaml can carry severity `"error"` or `"warning"` per-rule; the finding inherits that severity.
