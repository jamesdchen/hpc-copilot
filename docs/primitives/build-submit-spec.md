---
name: build-submit-spec
verb: scaffold
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent build-submit-spec --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.incorporation.build.submit_spec.build_submit_spec
exit_codes:
- 0: ok
- 1: user-error
---

## Purpose

Assemble + schema-validate a `submit_flow.input.json` spec from resolved interview values. Replaces the 200 lines of "set this field, set that field" prose in `/submit-hpc` Step 6d. The agent's job collapses to: run the interview (judgment), call `build-submit-spec` with the resolved values, write the returned dict to a JSON file, then `submit-flow --spec <file>`.

The primitive synthesizes the framework-required `job_env` keys (`EXECUTOR`, `HPC_RUN_ID`, `HPC_CMD_SHA`, `HPC_TASK_COUNT`, `REPO_DIR`, `MODULES`, `CONDA_SOURCE`, `CONDA_ENV`, plus `HPC_RUNTIME=uv` / `HPC_CAMPAIGN_ID` when applicable) automatically. Caller-supplied `extra_env` keys win on collision.

## Compose with

- **Predecessors**: `discover-executors` (pick the executor → profile name), `score-submit-plan` (resource constraints → cluster + backend), `compute-cmd-sha` (`run_id` + `cmd_sha`), `axes-init` / `discover-reducers` as upstream introspection.
- **Successors**: `submit-flow` (single spec) or wrap N specs in `{"specs": [...]}` for the auto-dispatched batch path.

## Notes

- **Default `script` per `(backend, is_gpu)`**: `cpu_array.{sh,slurm}` or `gpu_array.{sh,slurm}` under `.hpc/templates/`. Override with `script="..."` if your repo carries a custom template.
- **No `skip_preflight` field** (#275) — it was an agent-settable bypass that silenced `submit-flow`'s `command -v uv` runtime probe. The preflight skip is operator-only now via `HPC_AGENT_SKIP_PREFLIGHT=1`; build-submit-spec never emits it onto the spec.
- **Validation is mandatory**: the assembled spec is run through `schemas/submit_flow.input.json` before return. A missing required field surfaces as `spec_invalid` with the JSON Pointer path of the offending property.
- **Headless-friendly**: pure function, no filesystem writes. Safe to call from a non-Claude-Code orchestrator (external agent harness, cron-based campaign loop, etc.).
