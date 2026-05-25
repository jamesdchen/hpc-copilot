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
    [--experiment-dir <experiment_dir>]
  python: hpc_agent.cli.setup.setup
exit_codes:
- 0: ok
---

## Purpose

One-shot post-install bootstrap: copy the bundled slash commands + skills into `~/.claude/` and, when `--cluster <name>` is supplied, probe the cluster environment (SSH agent has keys, ssh + the file-transfer transport are on `$PATH`, `clusters.yaml` parses, TCP `:22` is reachable) and — on a green probe — write the 24-hour cache marker that `/submit-hpc`'s Step 6b gate reads. The first submit in this experiment then skips the probe.

## Compose with

- **Composes** `install-commands` (the asset copy) and `check-preflight` (the cluster probe).
- **Successor:** `submit-flow` — the cached preflight marker means the first `/submit-hpc` invocation doesn't re-pay the SSH probe latency.

## Notes

- The preflight marker is scoped to `--experiment-dir` (default: cwd) because the Step 6b gate reads from `JournalLayout(experiment_dir)`. Run setup from your experiment directory or pass `--experiment-dir` explicitly.
- Always exits `0` on a successful primitive call — callers branch on `data.preflight.all_ok` to detect a red probe.
- The ssh side-effect is opt-in via `--cluster`. The standard `requires_ssh` dispatcher gate is suppressed (`setup` is allow-listed in `tests/contracts/test_requires_ssh_consistency.py`) so the install-only path doesn't require `SSH_AUTH_SOCK`; `check_preflight` self-gates and reports a structured failure when the agent is unset.
