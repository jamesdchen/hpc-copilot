---
name: trace-diff
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent trace-diff --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.trace_diff_op.trace_diff
---
# trace-diff

Overlay TWO traces from the local trace store and report, per stage and per
atom, where their measurements DIVERGE — projection 5 of the data trace
(`docs/design/data-trace.md`): canary-vs-local, arm-vs-arm,
today-vs-last-known-good. The earliest diverging `(stage, atom)` is
highlighted (FIRST-DIVERGENCE) so a planted defect localizes to exactly the
stage that first alters the data. Read-only and client-side — a pure read of
`.hpc/traces/...`, no SSH, no scheduler.

Every comparison dispatches through the ONE semantics registry
(`state/data_trace.py::comparison_for`) that `trace-render` and the later
fingerprint interlock also consume — there is no second semantics definition
to drift. Differences are stated as **facts** (`row_count rows 100 → 90`),
never verdicts: the render carries no verdict vocabulary, disclosing the
difference and leaving the conclusion to the human (the pointing doctrine
applied to data). Absence is honest — a key the store never held is disclosed
(`present: false`), never fabricated as a match.

## Inputs

`--spec <spec.json>` — a `TraceDiffSpec`:

- `a`, `b` (required) — the two store keys, each `{scope_kind, scope_id,
  task?}`. `scope_kind` ∈ `run` | `audit` | `local`; `task` defaults to `0`
  (a single-task local run stores under `task-0`).
- `tolerance` (optional) — caller-owned tolerance for the tolerance-class
  atoms (`value_sketch`, `duration_ms`, `peak_mb`). `{default_abs_tol?,
  default_rel_tol?, per_key?}`, with `per_key` keyed by `"<atom>"` (scalar
  cost) or `"value_sketch:<column>"`. **Absent — or present with every bound
  absent — means an EXACT comparison** (core never invents an epsilon; the
  caller supplies the tolerance posture or gets exact). Only the
  tolerance-class atoms consult it; exact / set-delta / exact-per-key /
  equality-chain / exact-endpoints atoms are always compared exactly.
- `--experiment-dir` (path, default cwd) — the experiment root the store
  lives under.

## Outputs

`{trace_schema_version, a, b, clean, aligned, tolerance_applied,
first_divergence, stages, structural, render}`.

- `a` / `b` — the endpoint echo `{scope_kind, scope_id, task, present,
  stage_count}`; `present` is `false` when the store held no trace for that
  key (disclosed).
- `clean` — `true` when the two traces have NO divergence at any stage/atom.
- `aligned` — `true` when every stage matched on `(stage, seq)` — no
  structural divergence.
- `first_divergence` — the earliest diverging `(stage, atom)`:
  `{stage, seq, atom, kind, detail}`. `atom` is `null` and `kind` is
  `"structural"` for an unmatched stage; otherwise `kind` is the semantics
  that parted (`exact` / `set-delta` / `tolerance` / `exact-per-key` /
  `equality-chain` / `exact-endpoints`). `null` on a clean diff.
- `stages` — one entry per aligned/structural position, in `seq` order:
  `{stage, seq, side, divergences}` where `side` ∈ `both` | `a_only` |
  `b_only` and each divergence is `{atom, kind, detail}` (`detail` is the
  factual one-liner).
- `structural` — unmatched stages in the T1 flag shape
  `{rule: "stage_unmatched", detail, evidence}`.
- `render` — a deterministic, self-describing markdown overlay (trusted
  display; relayed verbatim): both keys + presence, the first-divergence
  lead, then one line per stage. Byte-stable for a given pair.

## Errors

- `spec_invalid` — a malformed spec (bad `scope_kind`, empty `scope_id`,
  negative `task`), or an atom-registry inconsistency (a semantics token with
  no comparator — a framework bug, refused loudly rather than rendered as a
  match).

A divergence is never an error: two traces that differ produce an exit-0
result with `clean: false` and the localized `first_divergence` — the feature
working. A key the store never held is not an error either; it is disclosed
as `present: false`.

## Idempotency

Idempotent by construction — a pure read of the append-only store. No state
is written; replaying yields the same overlay until the traces themselves
change.

## Notes

Stage alignment is by `(seq, stage)`: seq is monotonic per trace (a T1
invariant), so merging the two key sets seq-first makes the smallest-seq
parting position the first divergence — structural or atomic. The markdown
helper is trace-diff-owned; it shares the self-describing-header and
never-judgment conventions with `trace-render` and is a candidate for later
unification into a shared trace-markdown module.
