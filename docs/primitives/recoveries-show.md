---
name: recoveries-show
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent recoveries show --kind <kind> [--placeholders <placeholders>]
  python: hpc_agent.recovery.cli.recoveries_show
---
# recoveries-show

Print the canonical recovery menu for one failure kind. Use this from
SKILL.md prose to reference the menu by ``kind`` name instead of
re-embedding the literal options — the single chokepoint that keeps
``ErrorEnvelope.remediation`` strings byte-stable across emit sites.

## Inputs
- ``--kind <name>`` — one of the values returned by
  ``hpc-agent recoveries list`` (``ported_kinds``). An un-known or
  un-ported kind raises ``spec_invalid`` whose message lists the
  available kinds.

## Outputs
- ``kind`` (str) — echoes the requested kind.
- ``summary`` (str) — the framework's one-sentence diagnosis of what
  this kind means.
- ``options`` (list of ``{cli_command, when_to_use, safety_rank}``) —
  ordered by ``safety_rank`` ascending (primary recommendation first).
- ``references`` (list of str | empty) — issue / commit refs that
  motivated each option, for audit when an option's wording drifts.
- ``rendered_remediation`` (str) — the canonical envelope ``remediation``
  string, as ``remediation_for(<kind>)`` would return.

## Errors
- ``spec_invalid`` — kind is not in the registry. The message lists
  the available kinds so the caller can recover.

See ``docs/proposals/recovery-registry.md`` for the design + migration
plan.
