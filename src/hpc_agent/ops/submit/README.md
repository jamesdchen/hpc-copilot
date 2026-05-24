# ops/submit/

## What and why

`ops/submit/` owns the path from a typed submit spec to an SSH-pushed cluster job. It packs the atom (`plan_summary.py` — the deterministic pre-submit summary string), the throughput planner (`throughput.py` + the `plan_throughput.py` primitive wrapper — turns a task grid plus cluster constraints into a wave-batched `SubmissionPlan`), the runner (`runner.py` — `submit_and_record` writes the journal sidecar around an SSH-issued qsub), and the flow (`flow.py` — the composite that ties pre-flight, rsync, deploy, optional canary, qsub, and journal write into one envelope).

## Invariant

`ops/submit/` promises: typed `SubmitSpec` in -> SSH-pushed cluster job out, idempotent on `(experiment_dir, cmd_sha)`; never mutates remote state outside of the requested submission.

## Public vs internal

- `flow.py` — agent-facing primitive module (workflow composite: `submit-flow`, `submit-flow-batch`).
- `runner.py` — agent-facing primitive module (`submit-spec`: journal bookkeeping for a submission).
- `plan_summary.py` — agent-facing pure helper (primitive: `summarize-submit-plan`).
- `plan_throughput.py` — agent-facing pure helper (primitive: `plan-throughput`).
- `throughput.py` — agent-facing pure helper (`compute_submission_plan`, `build_wave_map`, `WorkloadSpec`, `SubmissionPlan`, `JobBatch`); marked `# @pure: no-io`.
- No subject-internal `_*.py` files.
