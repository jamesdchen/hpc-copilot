---
name: revise-resolved
verb: workflow
side_effects:
- writes-sidecar: <experiment>/.hpc/runs/<run_id>.json (the re-resolved sidecar)
idempotent: true
idempotency_key: scope_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent revise-resolved --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.revise_resolved.revise_resolved
---
# revise-resolved

The nudge-as-delta verb (proving-run-5 wave 5.1). On a spec-changing nudge in the
submit loop, the LLM names a **field delta** `{field: value}` — "use hoffman2
instead" is `{"cluster": "hoffman2"}` — and this verb applies it to the run's
latest greenlit `resolved` and **re-resolves**, re-deriving every field the delta
invalidates (`job_env`/activation from the new cluster, `run_id`/`cmd_sha`, the
`EXECUTOR` dispatcher, the sidecar).

**The load-bearing guard.** The `patch` may name only resolver-owned *input*
fields (cluster, walltime, grid, `goal`, `task_generator`, …). A key naming a
code-derived field (`job_env`, `run_id`, `cmd_sha`, `executor`, `ssh_target`,
`backend`, `remote_path`, …) is refused with `spec_invalid` — hand-authoring a
derived value, the finding-4/10/13/17 bug class, becomes structurally impossible.

**Source of truth is the sidecar.** `job_env`/`executor`/`run_id`/`cmd_sha` are
resolve-leg outputs, so the verb reads the run's on-disk sidecar
(`.hpc/runs/<run_id>.json`, the v2 config snapshot) for the run-owned inputs and
re-derives the cluster-owned fields from `clusters.yaml`. It therefore amends a
**resolved** prior: the pre-resolve S1 boundary (no sidecar) is refused with a
directive to resolve first — and a resolved S1 brief always has a sidecar
(`submit-s1` → `resolve-submit-inputs`), so the retarget nudge is covered.

**It does not bypass the gates.** The amended brief is committed by the human's
re-`y` through `append-decision`, so the authorship and brief-provenance gates
still run on the re-commit. `revise-resolved` only produces the brief.
