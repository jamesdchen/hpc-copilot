---
name: check-preflight
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent preflight [--spec <path>] [--cluster <cluster>]
  python: hpc_agent.ops.preflight.check.check_preflight
exit_codes:
- 0: all checks passed
- 2: one or more checks failed (envelope is still ok=true; failures live in checks[].ok)
---

## Purpose

Verify the local environment can submit HPC jobs: SSH agent reachable, `ssh` plus a file-transfer transport on PATH (`rsync`, or the `scp`+`tar` fallback the runtime uses when rsync is absent), `clusters.yaml` parses cleanly, optionally one cluster's TCP :22 reachable. When a built `submit-flow` spec is passed via `--spec`, it also runs the same `command -v uv` runtime probe `submit-flow` runs (the `runtime_uv` check, #275), so a `runtime: "uv"` spec against a cluster without `uv` is caught here — before any qsub — rather than failing every task cluster-side. Pure read; never mutates anything.

## Compose with

- **No predecessors.** Run this first in any pipeline that touches SSH (every submit-spec / poll-run-status / aggregate-results pipeline).
- Common successors: `discover-executors`, `score-submit-plan`, `submit-spec`.

## Notes

- Failures land as `checks[].ok = false` rather than an error envelope, so callers must inspect `data.all_ok` (or the exit code: 2 means at least one check failed).
- Skill callers can short-circuit: if a previous tick / session already ran `check-preflight` successfully, no need to repeat unless the env changed (new shell, restored backup, etc.).
- The TCP-22 probe is the only network-touching check; omit `--cluster` for an offline-only sanity pass.
- **`--spec` runs the runtime (uv) probe (#275).** Pass the built `submit-flow` spec (the one Step 6d produces) so check-preflight can run `submit-flow`'s `command -v uv` probe against the spec's `ssh_target` + activation `job_env`. It fires only when the spec sets `HPC_RUNTIME=uv`; a non-uv spec (or no `--spec`) skips it and pays no extra ssh round-trip. A missing `uv` surfaces as `runtime_uv` check `ok=false` with a `pip install uv` remediation, not an error envelope. This closes the gap where the SKILL.md flow ran check-preflight without the spec and then `submit-flow` with the (now-removed) `skip_preflight` field, so the uv guard never fired.
