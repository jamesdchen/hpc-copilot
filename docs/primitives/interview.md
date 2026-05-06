---
name: interview
verb: scaffold
side_effects:
- file_write: <campaign_dir>/{interview.json,meta.json}
idempotent: true
idempotency_key: campaign_dir
error_codes: []
backed_by:
  cli: hpc-mapreduce interview
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
- `produced_by` (`{kind: "mars" | "human", session_sha?, at?, operator?}`).

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
  for human interviews; for MARs interviews this is typically the
  agent's tool-call trace.
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
