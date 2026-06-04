---
name: fetch-skill-return
verb: query
side_effects:
- filesystem: <experiment_dir>/.hpc/_returns/
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent fetch-skill-return [--experiment-dir <dir>] --skill <skill> [--no-clear]
  python: hpc_agent.cli.skill_returns.fetch_skill_return
---
# fetch-skill-return

Parent-side counterpart to `emit-skill-return` (WS2 of the
determinism migration). A parent skill (`hpc-submit`, `hpc-campaign`)
invokes this verb **immediately after** a composed `Skill(<sub>)`
returns, to read the sub-skill's committed return envelope from
`<experiment_dir>/.hpc/_returns/<skill>.json`.

## Contract

1. Reads `<experiment_dir>/.hpc/_returns/<skill>.json`.
2. Re-validates against the per-skill schema at
   `hpc_agent/schemas/skill_returns/<skill>.json` (defence in depth —
   `emit-skill-return` already validated on the way in, so a fail
   here means hand-edit or schema drift, not a fresh sub-skill bug).
3. Prints the validated envelope JSON to **stdout** verbatim — that
   IS the sub-skill's return value; the parent parses it like any
   other `hpc-agent` envelope.
4. **Deletes** the committed envelope after reading (so a later
   `fetch-skill-return` after the parent's *next* sub-skill
   invocation doesn't see a stale return). Pass `--no-clear` to
   leave it on disk — useful when multiple consumers need the same
   envelope.

## Missing-return envelope (typed)

When no committed envelope exists, the verb emits a
`precondition_failed` envelope with
`failure_features.error_class_raw == "skill_return_missing"`. Parent
skills can branch on that exact key without parsing remediation
prose. The remediation also names the staged sibling
(`<skill>.staged.json`) — if it exists, the sub-skill staged the
envelope but the emit verb either was never called or failed
validation; the parent surfaces the staged file's contents.

## Exit code conventions

* `0` — envelope read + validated; printed to stdout.
* `1` — user-facing problem (missing file, unknown skill).
* `3` — internal problem (a committed envelope on disk no longer
  matches its schema — almost always a hand-edit or schema bump).

## See also

* `emit-skill-return` — the sub-skill-side writer.
* `hpc_agent/cli/skill_returns.py` — implementation.
* `hpc_agent/schemas/skill_returns/<skill>.json` — per-skill
  envelope schemas.
