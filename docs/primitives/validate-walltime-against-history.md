---
name: validate-walltime-against-history
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: (none — Python-only primitive)
  python: hpc_agent.ops.validate.walltime_against_history.validate_walltime_against_history
---
# validate-walltime-against-history

> **Internal primitive** — not surfaced in `capabilities --full`.
> No CLI subcommand and no MCP tool exists to invoke it, so no agent can
> call it directly; `validate-campaign` composes it and its findings reach
> the agent folded into that envelope. `validate-campaign` is the
> agent-facing verb.

Cross-reference requested walltime against runtime priors and project playbook rules. Three rule families: (1) compare requested walltime against historical quantiles (e.g., warn if below p95), (2) check for known-bad GPU/workload combinations from `.hpc/playbook.yaml`, and (3) flag cold-start (no historical samples) with info-level findings so the agent knows the lack of warning is "no data," not "all clear."

## Composers

- **`validate-campaign`** — the pre-submit validation composer.
- `_kernel/lifecycle/playbook.py` owns the playbook rules this validator reads.

## Contract surface

Takes `profile` (matches the runtime-prior pool the validator reads),
`cluster`, `requested_walltime_sec`, `gpu_type` (optional, e.g. `"a100"` —
required for the quantile and known-bad checks), and `workload_tags`
(project-specific tags like `"attn-fp32"` looked up against playbook known-bad
combos; an empty list disables the playbook lookup).

Returns a `ValidateWalltimeAgainstHistoryResult` whose `findings` list is empty
on pass. Each `ValidatorFinding` carries `validator` / `severity` / `code` /
`message` / `suggested_fix` (increase walltime to X seconds) / `evidence`
(`requested_walltime_sec`, `quantile_label`, `quantile_sec`, `n_samples`,
`gpu_type`, `workload_tag`).

## Invariants

- **No envelope-level `error_code`.** Diagnostics ride in `findings[].code`:
  `playbook_parse_error` (error — malformed `.hpc/playbook.yaml`),
  `cold_start_no_history` (info), `walltime_below_quantile`, and
  `known_bad_combination`.
- **Cold start is announced, not silent.** On the first submission for a
  (profile, cluster, gpu) tuple the validator emits an info-level finding so
  the agent can tell "no data" from "all clear"; the submission proceeds and
  subsequent runs populate the prior.
- **The built-in rule is p95.** When `.hpc/playbook.yaml` declares no
  `walltime_rules`, the framework warns if `requested_walltime_sec < p95` —
  mirroring the lesson that walltime below historical p95 is the strongest
  correlate of in-flight TIMEOUT.
- **Severity is the playbook's, not the validator's.** Both
  `walltime_below_quantile` and `known_bad_combination` inherit the severity
  declared on the matching rule (`"error"` or `"warning"`).
- **Pure read** of runtime priors and the playbook. Same priors and playbook,
  same findings.

## Coupling

- The quantile semantics and rule schema live in `.hpc/playbook.yaml`, which
  is per-project and editable without code changes; this validator only
  applies what the playbook declares plus the built-in default.
- Reads the runtime-prior pool keyed by (profile, cluster, gpu_type). A change
  to how priors are keyed silently converts every campaign to cold start.
- Shares the `ValidatorFinding` shape with the rest of the
  `validate-campaign` family.

## Failure modes

- **`gpu_type` omitted** → the quantile and known-bad checks do not run, and
  the only remaining signal is the playbook parse check. The result still
  reads as a pass.
- **Cold start reads as approval.** The info-level finding is the only thing
  distinguishing "no history" from "checked and fine"; a caller that filters
  on severity ≥ warning loses that distinction.
- **Malformed playbook** → `playbook_parse_error` at error severity, which
  `validate-campaign` escalates to `overall="fail"`; a project can block its
  own submits with a YAML typo.

**Schemas:** [`validate_walltime_against_history.input.json`](../../src/hpc_agent/schemas/validate_walltime_against_history.input.json), [`validate_walltime_against_history.output.json`](../../src/hpc_agent/schemas/validate_walltime_against_history.output.json).
