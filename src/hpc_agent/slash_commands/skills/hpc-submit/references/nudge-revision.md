# Branch: a spec-changing nudge → `revise-resolved`

Read when the human's answer at a greenlight is a nudge that changes a spec
value (the SKILL's step 3 trigger). The trigger line already told you the
verb; this is the WHY and the boundaries.

`revise-resolved` (MCP-direct) applies the field delta `{field: value}` to the
journaled `resolved` and RE-RESOLVES, re-deriving everything the delta
invalidates — `job_env`/activation from the new cluster, `run_id`/`cmd_sha`,
the `EXECUTOR` dispatcher, the sidecar. A hand-edited spec JSON silently drops
those derivations: findings 4/10/13/17 (`job_env` emptied, `scope_id`
improvised, `supersedes` deleted, `EXECUTOR` mangled) were all children of a
hand-authored spec. The delta names only an INPUT field (cluster, walltime,
grid, `goal`, `task_generator`); it structurally **cannot** express a derived
field, so that corruption class is impossible by construction.

Boundaries:

- Re-present the amended brief the verb returns; loop until `y`. **The
  human-visible loop is unchanged** — propose, `y`/nudge, re-present (design
  §2); only the *authoring* of the amended spec moves off you.
- `revise-resolved` does NOT bypass the gates: your re-`y` still commits
  through `append-decision`, so a `goal`/`task_generator` delta still meets
  the human-authorship gate (ask the human, per the SKILL's authorship
  section).
- A delta whose target field is `cluster` AT AN ANOMALY terminator is the
  RETARGET arm instead — see `references/retarget-anomaly.md`.
