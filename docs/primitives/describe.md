---
name: describe
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent describe <name>
  python: hpc_agent.cli.setup.describe
exit_codes:
- 0: ok
- 1: user-error
---

## Purpose

Resolve a name to its content from the installed package data. A delegated worker calls `describe` to fetch a cross-reference it reaches on its branch — a worker-prompt procedure, a skill it is pointed at, a primitive whose contract it needs — instead of the spawn prompt pre-stitching every possible reference.

## Compose with

- **Predecessor:** `capabilities` (the bootstrap primitive — lists every name an agent can `describe`).
- **No fixed successor;** the worker reads the body and continues whatever branch led it here.

## Notes

Resolution order:

1. Worker-prompt procedure (`hpc_agent/worker_prompts/<name>.md`, with plugin overlay) → `kind: "procedure"`.
2. Inline skill (`slash_commands/skills/<name>/SKILL.md`) → `kind: "skill"`.
3. Primitive in the operations catalog → `kind: "primitive"` with its contract dict.

The first match wins; an unknown name returns `error_code: spec_invalid` (`category: user`).
