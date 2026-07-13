---
name: setup
verb: scaffold
side_effects:
- filesystem: ~/.claude/
- ssh: <cluster>
idempotent: true
idempotency_key: cluster
error_codes: []
backed_by:
  cli: hpc-agent setup [--dry-run] [--claude-dir <claude_dir>] [--cluster <cluster>]
    [--experiment-dir <experiment_dir>] [--install-cron]
  python: hpc_agent.cli.setup.setup
exit_codes:
- 0: ok
---

## Purpose

One-shot post-install bootstrap: copy the bundled slash commands + skills into `~/.claude/` and, when `--cluster <name>` is supplied, probe the cluster environment (SSH agent has keys, ssh + the file-transfer transport are on `$PATH`, `clusters.yaml` parses, TCP `:22` is reachable). A red probe exits non-zero (cluster-error) so a scripted bootstrap doesn't proceed believing setup succeeded.

## Compose with

- **Composes** `install-commands` (the asset copy) and `check-preflight` (the cluster probe).
- **Successor:** `submit-flow`.

## Notes

- The CLI (`hpc-agent setup --cluster <name>`) exits cluster-error (2) with an `ok:false` envelope on a red probe — the failing checks ride in `failure_features`. The in-process `setup()` primitive returns the verdict under `data.preflight.all_ok` for a caller to inspect.
- The ssh side-effect is opt-in via `--cluster`. The standard `requires_ssh` dispatcher gate is suppressed (`setup` is allow-listed in `tests/contracts/test_requires_ssh_consistency.py`) so the install-only path doesn't require `SSH_AUTH_SOCK`; `check_preflight` self-gates and reports a structured failure when the agent is unset.
