---
name: capabilities
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: (none — Python-only primitive)
  python: hpc_agent._kernel.extension.capabilities.capabilities
exit_codes:
- 0: ok
---

## Purpose

Machine-readable feature flags. Lets external orchestrators discover what subcommands this `hpc-agent` install supports, where its schemas live, and which env vars it needs. Pure introspection; no side effects.

## Compose with

- **No predecessors.** Run this first when an agent encounters an unfamiliar `hpc-agent` install.
- Common successors: any other primitive — `capabilities` is the bootstrap primitive. To fetch a specific named primitive's contract, a skill's body, or a worker-prompt procedure's body, follow up with `hpc-agent describe <name>`.

## Notes

- Content for named primitives, skills, or worker-prompt procedures is fetched via `hpc-agent describe <name>` (returns the body in a JSON envelope). An earlier `skill_paths` field that exposed package-data filesystem paths was removed in favor of `describe`.
- `required_env` lists env vars the framework expects to be set in the calling shell — agents can use this to validate their environment before invoking other primitives.
