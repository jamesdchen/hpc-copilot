---
name: emit-skill-return
verb: mutate
side_effects:
- filesystem: <experiment_dir>/.hpc/_returns/
idempotent: true
idempotency_key: experiment_dir
error_codes: []
backed_by:
  cli: hpc-agent emit-skill-return [--experiment-dir <dir>] --skill <skill>
  python: hpc_agent.cli.skill_returns.emit_skill_return
---
# emit-skill-return

Sub-skill side of the file-based return primitive (WS2 of the
determinism migration). A sub-skill (`hpc-classify-axis`,
`hpc-wrap-entry-point`, `hpc-build-executor`, `hpc-status`,
`hpc-aggregate`) calls this verb as its **final tool call** to hand off
to its parent skill (`hpc-submit`, `hpc-campaign`) without firing the
harness's end-of-turn signal that any chat message would trigger.

## Contract

1. The sub-skill **writes** its return envelope JSON to
   `<experiment_dir>/.hpc/_returns/<skill>.staged.json` using the
   `Write` tool — never via `echo > file` (the auto-mode classifier
   blocks shell redirects).
2. The sub-skill **invokes** `hpc-agent emit-skill-return --skill
   <skill> --experiment-dir <experiment_dir>` as its *last* action.
3. The verb validates the staged envelope against the per-skill schema
   at `hpc_agent/schemas/skill_returns/<skill>.json` and, on success,
   atomically renames `<skill>.staged.json` → `<skill>.json`
   (`os.replace`, cross-platform atomic on the same filesystem).
4. On schema failure the staged file is **preserved** for debugging
   and a `spec_invalid` envelope identifies the failing JSON path +
   the schema's on-disk location. The sub-skill can fix the staged
   envelope and re-invoke without re-running its computation.

## Envelope shapes

Each schema is `oneOf: [Success, Error]`. `Success` required fields
come from the sub-skill's final-step contract (see each `SKILL.md`'s
final step). `Error` inherits the framework-standard `ErrorEnvelope`
shape from `hpc_agent/schemas/envelope.json` — same `error_code`,
`message`, `category`, `retry_safe`, optional `remediation` /
`failure_features` / `escalation` fields the rest of the framework
emits.

## Why a file, not a chat message

Returning via the Skill-tool result (a closing chat message with the
envelope JSON) fires an end-of-turn signal — observed empirically:
the parent skill stalls mid-procedure and the user has to type "keep
going". A file write is a tool call; it does not fire end-of-turn.
The pair is the structural fix; the "no closing chat message" prose
in each sub-skill SKILL.md is the belt-and-suspenders.

## See also

* `fetch-skill-return` — the parent-side reader.
* `hpc_agent/cli/skill_returns.py` — implementation.
* `hpc_agent/schemas/skill_returns/<skill>.json` — per-skill
  envelope schemas.
