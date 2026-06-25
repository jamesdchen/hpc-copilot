---
name: apply-safe-defaults
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent apply-safe-defaults --spec <path>
  python: hpc_agent.ops.submit.apply_safe_defaults.apply_safe_defaults
---
# apply-safe-defaults

The autonomous-caller counterpart to a human walking the
`needs_resolution` dialog. Consumes the `{resolved, ambiguities}`
envelope [`walk-submit-ambiguities`](walk-submit-ambiguities.md)
produced and fills each ambiguity from its own `safe_default`. Replaces
the `hpc-submit` SKILL's "the autonomous caller applies safe_defaults"
prose with code.

The two outputs chain without reshaping: pipe the walk's `data` straight
into this verb's `--spec`.

## Inputs / outputs

See `hpc_agent/schemas/apply_safe_defaults.{input,output}.json`. Input is
the `{resolved, ambiguities}` envelope. Output is the merged `resolved`
plus `applied` (field → value for each auto-filled ambiguity) and
`still_unresolved` (fields left for the caller — chiefly `goal` /
`task_generator`). `all_resolved` is true iff `still_unresolved` is empty.

## It structurally cannot fill task_generator

Because the field partition never attaches a `safe_default` to a
REQUIRED_CALLER_FIELDS member (the `Ambiguity` guard refuses one at
construction), a `task_generator` ambiguity arriving here carries no
default to apply — so this verb leaves it in `still_unresolved`. It is
*structurally* unfillable, not merely policy-blocked.

## Defense-in-depth

Even so, the verb re-checks
`field_partition.may_have_safe_default` for **every** ambiguity it would
fill. A `task_generator` (or `goal`) ambiguity that somehow carried a
`safe_default` — a hand-tampered envelope — raises `spec_invalid` here
rather than silently fabricating a sweep. The partition makes the
structure unfillable; this check makes a *tampered* structure loud. This
is the "verify a guard can actually fire" discipline: the lock is real,
and the test suite exercises its fire path
(`test_apply_safe_defaults.py::test_refuses_tampered_task_generator_safe_default`).

## Present-slot semantics

The fill test is `safe_default is not None`, **not** truthiness:

- `uncovered_param`'s `{param: null}` is a *present* slot and is applied;
- a falsy-but-present default on an auto-resolvable field (`0`, `""`,
  `[]`, `{}` — e.g. `homogeneous_axes: []`) is a real default and is
  applied;
- an auto-resolvable field whose `safe_default` is genuinely absent (e.g.
  a `cluster` ambiguity with no configured candidates) stays for the
  caller.

## requires_ssh: False

Pure data transformation over the envelope; no cluster, no disk, no
network.
