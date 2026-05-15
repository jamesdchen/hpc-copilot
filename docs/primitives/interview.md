---
name: interview
verb: scaffold
side_effects:
- file_write: <campaign_dir>/{interview.json,meta.json}
idempotent: true
idempotency_key: campaign_dir
error_codes: []
backed_by:
  cli: hpc-agent interview
  python: claude_hpc.atoms.interview.record_interview
---
# interview

Persist a structured campaign intent (goal, task_count, task_kind,
budget, abort_if, cluster_target, transcript) alongside a
`tasks.py` the calling agent already authored. Validates that
`tasks.total() == intent.task_count` and computes the `cmd_sha`
fingerprint so future `recall` queries can detect when an old
recipe has drifted.

## Inputs

The full input schema is at `claude_hpc/schemas/interview.input.json`
(Pydantic-emitted from `_schema_models/interview.py:InterviewSpec`).
Required:

- `goal` (non-empty str) — campaign goal in ~one sentence
  ("find LR for vit-b on imagenet-1k @ 8 GPUs").
- `task_count` (int ≥ 1) — expected `tasks.total()`. Mismatch
  raises `spec_invalid` *before* any disk write — catches off-by-one
  bugs at the interview stage instead of after burning compute.
- `produced_by` (`{kind: "agent" | "human", session_sha?, at?, operator?}`). Use `"agent"` for any non-human orchestrator (Claude Code, external harness, cron job).

Optional:

- `task_kind` — free-text family tag (`ml-hparam-sweep`,
  `rl-rollout`, `llm-prompt-eval`). No enum; `recall` groups by
  this tag.
- `budget` — opaque dict; units chosen by the interviewer
  (gpu_hours, cpu_hours, credits). `campaign-flow` surfaces these
  in its progress envelope; never enforces them.
- `abort_if` — `{metric, after_tasks, ...}` early-stop criterion;
  consumed by `campaign-flow`.
- `cluster_target` — `{cluster, profile, constraint?}`. When
  present, `submit-flow` uses these directly; otherwise the
  planner is invoked.
- `transcript` — Q/A turns (role + text + at). Strongly recommended
  for human interviews; for agent-driven interviews this is typically
  the agent's tool-call trace.
- `task_generator` — discriminated union over five recipe shapes
  (`enumerated`, `cartesian_product`, `items_x_seeds`,
  `numeric_logspace`, `numeric_linspace`). When present, the
  materializer regenerates `tasks.py` from the recipe; when absent,
  the agent's hand-written `tasks.py` is the source of truth.

## Outputs

`{ok: true, data: {campaign_dir, artifacts, total_tasks, cmd_sha,
preview}}`. `preview` carries `tasks.resolve(0)` /
`resolve(total_tasks // 2)` / `resolve(total_tasks - 1)` so the
calling agent can echo "sweep starts here / midpoint / ends here"
to the operator before submit.

## Side effects

- Writes `<campaign_dir>/interview.json` (the validated intent
  plus `_materialized.{at, agent}`).
- Writes/updates `<campaign_dir>/meta.json` when `cluster_target`
  or `budget` is supplied (so `submit-flow` and `campaign-flow`
  pick them up without re-reading interview.json).

## Errors

- `spec_invalid` — `tasks.total() != intent.task_count`,
  `cluster_target.cluster` not in `clusters.yaml`, or any field
  fails Pydantic validation.

## Notes

The interview deliberately does NOT typed-encode the search space;
the existing `tasks.py` contract (`total()` + `resolve(i) →
dict[str, Any]`) is experiment-agnostic and the dict shape is
enforced downstream by `compute_cmd_sha` (kwargs get
`**`-unpacked into the user's task function and must be
JSON-serializable). The `task_generator` field is opt-in; an
exotic campaign that doesn't fit the five recipes drops it from
intent and the agent writes `tasks.py` by hand.

### When to use this vs your caller's own memory model

Integrators frequently already maintain their own experiment journal
(an LLM-orchestrator's `experiments/<id>/meta.json`, a campaign-loop
runner's per-campaign log, etc.). `interview` / `recall` are
scoped specifically at the *interview-time* leak: the conversation
that produced one `tasks.py`, frozen alongside the file that
materialized from it.

Use this primitive when the calling agent wants:

- A structured persistence of the *why* (goal, range, budget, abort
  criterion) next to the `tasks.py` it produced, so subsequent
  campaigns can ground in observed envelopes.
- Cross-campaign queryability without re-deriving from chat logs
  (the calling agent's session context is transient; `recall`'s
  filesystem walk is durable).
- A `cmd_sha` fingerprint of the materialized task list at interview
  time, captured before submit, so drift is visible the next time the
  campaign re-runs.

Stick with the caller's own journal when:

- The artifact you want to preserve is broader than one
  campaign (cross-experiment provenance, paper-level metadata,
  cross-agent project state). `interview.json` is per-campaign by
  construction; layering experiment-level context on top is the
  caller's job.
- The calling agent maintains a richer wire schema (typed metric
  histories, vector embeddings, structured rejection reasons). The
  interview primitive's schema is bare-bones on purpose — five
  recipe shapes for `task_generator` and free-text `goal` /
  `transcript`. It doesn't replace a domain-specific store.

The two layers coexist: an integrator's experiment-level journal
keys on `experiment_id`; claude-hpc's interview keys on
`campaign_dir`. Different scopes, no overlap.
