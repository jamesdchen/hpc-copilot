---
name: install-commands
verb: scaffold
side_effects:
- filesystem: ~/.claude/
idempotent: true
idempotency_key: claude_dir
error_codes: []
backed_by:
  cli: hpc-agent install-commands [--dry-run] [--claude-dir <claude_dir>]
  python: hpc_agent.cli.setup.install_commands
exit_codes:
- 0: ok
---

## Purpose

Pip-install entry point: copy the bundled slash commands, skills, and named subagent definitions shipped in the `hpc-agent` wheel into `~/.claude/commands/`, `~/.claude/skills/`, and `~/.claude/agents/` so Claude Code can pick them up. Idempotent — re-running overwrites in place.

The installed agents include **`hpc-worker`** — a subagent pinned to a small, cheap model (`model: haiku` in its own frontmatter) that inline mode (`HPC_AGENT_INVOKER=inline`) dispatches the rendered workflow procedure to. Because the pin rides with the definition, the harness enforces it regardless of the caller's model; a harness without subagent support simply runs the procedure in-context instead.

## Compose with

- **Predecessor:** `pip install hpc-agent` (puts the wheel assets on disk under the package data root).
- **Successor:** `setup` (which calls `install-commands` first and then optionally probes a cluster).

## Notes

- Pass `--claude-dir <path>` to target a non-default Claude config directory (e.g. a per-project sandbox or CI runner home).
- `--dry-run` prints the would-copy list without writing — useful for a "what is this going to change" preview before the first install.
