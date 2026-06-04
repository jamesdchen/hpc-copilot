---
name: status-preflight
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent status-preflight --experiment-dir <experiment_dir>
  python: hpc_agent.ops.status_preflight.status_preflight
---
# status-preflight

Composite preflight primitive: runs `install-commands` then `load-context`
as one CLI call. The simplest of the `<skill>-preflight` family — no
`reconcile` branch like submit / aggregate have — and the clean prototype
for the pattern.

## Inputs / outputs

See `hpc_agent/schemas/status_preflight.{input,output}.json`. Input requires
only `experiment_dir`. Output carries a `SubResult` per fanned-out sub-call
under `data.install_commands` and `data.load_context`.

## Internal composition

Sequential, plain `subprocess.run`. The order is mandatory:
`install-commands` lays down the bundled SKILL.md / agent files and
`load-context` may resolve paths that depend on them. No asyncio fan-out.

## Failure semantics

`overall: "pass"` iff both sub-calls returned `ok: true`. Any non-skipped
sub-call returning `ok: false` flips `overall: "fail"`. The composite
itself returns `ok: true` at the outer envelope; the failing sub-call's
verbatim envelope is preserved under `data.<subcall>.envelope` so the
caller can read its `error_code` + `remediation` without re-running.

## Why this exists

The agent's prose-discipline at the top of every `hpc-status` invocation
used to be: "Step 0: run install-commands. Step 1: run load-context."
The entire 0.10.2 release was motivated by Step 0 silently being skipped.
Collapsing both into one verb makes the omission structurally impossible.
