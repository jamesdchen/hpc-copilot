---
name: recoveries-list
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent recoveries list
  python: hpc_agent.recovery.cli.recoveries_list
---
# recoveries-list

List every failure ``kind`` known to the recovery registry. Reports
``ported_kinds`` (with menus available via ``recoveries show``) and
``unported_kinds`` (the migration punch list — kinds declared in the
``RecoveryKind`` Literal but not yet ported to ``REGISTRY``).

Used by SKILL.md authors to see what recoveries are available before
referencing one in prose. The shape is intentionally minimal — a caller
that needs option detail goes on to ``recoveries show --kind <name>``.

## Inputs
None.

## Outputs
- ``ported_kinds`` (sorted list of str) — every kind with a registry menu.
- ``unported_kinds`` (sorted list of str) — kinds on the punch list.
- ``n_ported`` (int) / ``n_total`` (int) — convenience counts.

See ``docs/proposals/recovery-registry.md`` for the design + migration
plan.
