---
name: capabilities
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent capabilities
  python: hpc_agent.atoms.capabilities.capabilities
exit_codes:
- 0: ok
---

## Purpose

Machine-readable feature flags. Lets external orchestrators discover what subcommands this `hpc-agent` install supports, where its schemas live, which env vars it needs, and where its skill files are on disk. Pure introspection; no side effects.

## Compose with

- **No predecessors.** Run this first when an agent encounters an unfamiliar `hpc-agent` install.
- Common successors: any other primitive — `capabilities` is the bootstrap primitive.

## Notes

- `skill_paths` returns absolute paths to the SKILL.md files. Skills ship as package data (`slash_commands/skills/hpc-*/SKILL.md`), so the paths resolve identically for a wheel install and a source-tree checkout.
- `required_env` lists env vars the framework expects to be set in the calling shell — agents can use this to validate their environment before invoking other primitives.
