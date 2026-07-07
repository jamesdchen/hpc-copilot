# Design: the reproduction receipt — reproduce-run + verify-reproduction

Status: **PROPOSED** (reproduction-receipt wave, task T4 = this decision
record). Two verbs: `reproduce-run` (non-blocking mint + `next_block=submit-s2`
hand-off, the `retarget-run` shape) and `verify-reproduction` (a local
comparator that writes a durable receipt). This document is the decision
record — WHY the shape is what it is, and the alternatives rejected. Facts
(symbol names) cite `path::symbol`; where this doc and the code disagree, the
code and its enforcement-mapped tests win.

## Problem

A caller wants to re-run a finished experiment and ask a single honest
question: *did it reproduce?* — same code, same params, same env, on the same
or a fresh cluster, and does the second run's numbers match the first's within
a tolerance the caller owns. This is the substrate under three higher
questions the framework must NOT answer itself: is a drifted result a bug, a
nondeterminism, or environment decay? The framework cannot judge whether two
numbers are "close enough" (it never learns what a metric means), but it CAN
mint the re-run against a pinned identity, refuse to call a code-drifted re-run
a reproduction, compare the numbers a reducer already computed, and write a
durable verdict. Mechanism where mechanism is possible; the tolerance and the
interpretation left to the human above.

## The boundary this feature sits on

The same IDENTITY / ORDERING / COMPARISON / COUNTING surface the rigor
primitives crystallized (`docs/design/rigor-primitives.md`, "the boundary the
feature crystallized"). A reproduction is IDENTITY (is this the same code +
params + env?) plus COMPARISON (is this number equal-within-tolerance to that
number?) over **opaque caller content**. The comparator never names a metric,
never knows which metric matters, never picks a tolerance — those are the
domain-pack / human layer above core. Core reproduces, compares, and records;
it does not judge.

## Decisions

### Composite over monolith — reproduce-run is non-blocking, S2/S3 own execution

`reproduce-run` mints the reproduction run (a fresh resolve keyed on a new
run_name against the same or a new cluster), best-effort-records the
`reproduces` provenance link, and hands off to `submit-s2` via `next_block` —
returning in seconds. It NEVER runs the re-canary or the 30-minute array
inline; S2's detach-by-contract worker owns staging + canary, S3 owns the main
poll, and `verify-reproduction` runs only after the harvest lands.

WHY: the `retarget-run` precedent (`ops/retarget_run.py::retarget_run`, and its
enforcement row in `docs/internals/engineering-principles.md`). A curated MCP
verb must return promptly — a verb that blocks for ~30 min holding a canary is
the exact wedge that made the run-#8 agent, unable to reach a blocking verb over
MCP, hand-run `kill→confirm→revise` against a throttled hoffman2. The
non-blocking `next_block=submit-s2` contract is what makes the verb safe to
expose as a curated tool. Rejected: **a blocking `reproduce` verb** that mints,
stages, canaries, submits, polls, harvests, and compares in one call.

### `reproduces` is a sidecar provenance field — NOT supersession

The reproduction run carries a `reproduces: <orig_run_id>` field on its per-run
sidecar (the v2 field set beside `data_sha` / `env_hash` / `scopes`, written
verbatim, defaulted `None`). It is a one-directional provenance back-link, and
that is all. `supersede_run` is NEVER called.

WHY: a reproduction **closes nothing**. The original run is still valid, still
the thing being reproduced; the second run does not replace it, retire it, or
re-attach dedup to it. Supersession is the wrong relation — it exists to close a
*failed* attempt (`retarget-run`) or a superseded lineage, and it flips the
original's terminal state and pairs its `-canary`. A reproduction leaves the
original entirely untouched. Rejected: **supersession linkage** (`supersede_run`
on the reproduction, or reading the reproduction as a lineage member of the
original).

### The drift refusal needs the shared code-drift predicate — cmd_sha alone is insufficient

`reproduce-run` refuses to mint a "reproduction" of code that has drifted since
the original. `cmd_sha` alone CANNOT gate this: `cmd_sha` is **parameter
identity only** (`state/run_sha.py::compute_cmd_sha`, the #207 boundary — it
hashes the materialized per-task kwargs and deliberately excludes the executor
body and `tasks.py` bytes). An executor-body edit — a bug fix, a refactor, a
non-swept hyperparameter change — with every `resolve(i)` dict unchanged keeps
the identical `cmd_sha`, so a `cmd_sha`-only gate would happily "reproduce"
**different code** and call the mismatch a nondeterminism.

Code identity is the job of the shared predicate
`state/code_drift.py::detect_code_drift` (`tasks_py_sha` + `executor`, the ONE
definition both dedup layers route through — its enforcement row pins that
neither layer re-inlines the comparison). `reproduce-run` feeds it the
original's recorded `executor` / `tasks_py_sha` against the current values; a
`drifted` outcome refuses with the drift evidence. Rejected: **a cmd_sha-only
refusal** (params match ⇒ reproduce), which silently reproduces edited code.

### Anti-contamination — the reproduction gets a DISJOINT remote_path

The reproduction resolves under a `remote_path` disjoint from the original's —
the convention `<orig_remote_path>-repro` — never a path nested under, or a
sibling within, the original's tree.

WHY: the per-task fallback reduce scans **recursively** under
`record.remote_path` for every `metrics.json`
(`ops/aggregate_flow.py::_per_task_metrics_reduce`), excluding ONLY the
`-canary` suffix sibling and refusing any row **overcount** past
`record.total_tasks` as provable foreign contamination. A reproduction sharing
the original's path subtree would either trip that overcount refusal or — worse,
if the original re-aggregates later — **blend into the original's future
reduce**, corrupting its mean. This is the run-#6 harvest failure generalized:
an 11-row mean for a 10-task run, because a path-sibling's rows got swept in. A
disjoint path keeps each run's recursive scan seeing only its own rows. Rejected:
**run_id-naming-only separation** — a distinct run_id but a shared subtree still
lets the recursive `rglob("metrics.json")` cross-contaminate, because the scan
keys on the path, not the id.

### Partial reproduction compares PER-TASK, never pooled-vs-subset

A partial reproduction (`task_sample` — reproduce a sampled subset of the
original's task ids) compares the sampled tasks' **per-task** metrics against
the original's per-task metrics for those same ids. It NEVER compares the
original's pooled aggregate against a subset aggregate.

WHY the honesty argument: the original's pooled mean is over all N tasks; a
subset's mean is over k<N. Comparing them asks "does the mean of 3 tasks match
the mean of 300?" — a question whose answer is noise, dressed as a reproduction
verdict. Per-task comparison for the sampled ids is the only honest partial
question. This is a severable last task, deferred — v1 verifies the full re-run.
Rejected: **pooled-vs-subset aggregate comparison** (a partial that means
nothing).

### Tree-snapshot storage is OUT of v1

v1 does not snapshot or reconstruct the original's source tree. If the tree has
moved or changed such that the original's recorded code identity cannot be
reproduced, the verb REFUSES with drift evidence (via `detect_code_drift`) —
it does not silently reconstruct-and-pretend.

WHY: a refusal that names the drift is honest and cheap; a reconstruction is a
large mechanism whose failure modes (a partial tree, a stale checkout) would
manufacture false reproductions. A moved tree is a real obstacle the human
resolves, not one the framework papers over. Rejected: **tree-snapshot storage
in v1** (reconstruct the original's tree to force a reproduction).

### The receipt lives experiment-local, beside the metrics it verdicts

The reproduction receipt is appended to
`_aggregated/<repro_run_id>/reproduction_receipts.jsonl` — experiment-local,
append-only, beside the aggregated metrics whose comparison it records.

WHY: the receipt must survive a journal wipe. The decision journal is control
state that gets reset between demo sessions and can be pruned; a reproduction
verdict is a **durable scientific record** that belongs with the results it
judges, not with the transient orchestration log. Append-only means a
re-verification adds a receipt without erasing the prior one — the full history
of every reproduction attempt survives, the same auditability the scope
look-ledger keeps. Rejected: **a journal-home ledger** (the receipt as a
decision record under the journal), which a journal wipe would destroy.

### A mismatch is a FINDING, never an error — and the comparator carries no metric-name special cases

When the numbers do not match within tolerance, `verify-reproduction` returns a
`needs_decision` brief (a FINDING), NOT an error. The receipt records the
mismatch verdict; the run does not fail.

WHY: **discovered nondeterminism is the feature working.** A reproduction that
mismatches has surfaced exactly what the caller asked to find out — a bug, a
nondeterminism, or env decay. Painting it as an error would train the human to
treat a successful discovery as a failure (the same lesson as
`docs/design/rigor-primitives.md`, "harvest-guard treats a locked scope as a
clean skip"). The human above reads the finding and decides which of the three
it is.

And the comparator carries **NO metric-name special cases** — no built-in
knowledge that `accuracy` matters more than `loss`, no per-metric default
tolerance, no metric it privileges. It compares over opaque numbers with a
caller-owned tolerance. This is the boundary rule: COMPARISON over opaque
caller content, naming and judging left above (the fabrication class the
NO-VOCABULARY rule refuses — core must never invent what a metric means).
Rejected: **a mismatch-as-error** verdict, and **any metric-name special-casing**
in the comparator.

### Every reduce path leaves the comparator its input (L2 closed)

`verify-reproduction` reads each run's reduced metrics from
`_aggregated/<run_id>/metrics_aggregate.json` (rung 1 of its artifact ladder).
That artifact must exist regardless of HOW the run was aggregated. Originally
only the SSH combiner-only default persisted it, so a run reduced through the
PURE-API path (`aggregate_flow._pure_api_reduce`) or the CLUSTER-REDUCE path
(`aggregate_flow` → `ops/aggregate/cluster_reduce.cluster_reduce`) left no
artifact and verify-reproduction returned an honest-but-needless `incomparable`
— a coverage hole, not a correctness bug (verifier finding L2, 2026-07-07).

The class fix: `ops/aggregate_flow._persist_local_aggregate` is the ONE
persistence definition, and every local-reducing path routes through it — the
default, the pure-API path, and the cluster-reduce path each call it before
returning, so all three leave the identical `{"aggregated_metrics": ...,
"provenance": {...}}` shape at the canonical path. A cluster-reduce whose
reducer emits a non-dict JSON (a bare scalar/list) persists
`aggregated_metrics: {}` — the honest empty (no keyed metrics to diff), never a
fabricated scalar the comparator would pretend to match. The opt-in
cluster-final path (`HPC_CLUSTER_FINAL_REDUCE`) keeps its RICHER
cluster-produced aggregate (waves/manifest/errors_per_wave) rather than routing
through the leaner seam, but now lands it at the SAME flat
`_aggregated/<run_id>/metrics_aggregate.json` verify reads (it previously nested
the pull one level too deep, where no comparator looked). The enforcement row
"The durable comparator artifact … has ONE persistence definition" pins every
path through the seam and names the per-path test.

## Pipeline seat and extensibility

**The dossier bundle.** The receipt is designed to slot into a future evidence
dossier via a `source` string on each receipt record — a reproduction receipt
is one `source` among others (a scope look-count, a canary verdict), so the
bundle grows by adding a `source`, not by a schema migration. Extensibility
without a schema change.

**Where it sits in the pipeline.** The receipt's job is to distinguish
**decay-vs-bug-vs-env-drift at the live-monitoring stage** — when a
long-running or re-visited experiment's numbers move, the reproduction receipt
is the evidence that separates "the world changed" (env drift / decay) from
"the code broke" (bug). It is the honest second look.

**Re-score over recompute is the cheaper first answer.** When the original run
persisted its primitives (raw per-task outputs, not just the reduced
aggregate), the cheaper first question is not "re-run the whole array" but
"re-score the persisted primitives" — recomputing the verdict metric without
re-executing the compute. That re-score verb is external context here (harxhar's
`idea_to_trade` doc names it); `reproduce-run` is the full recompute path, and a
re-score seam is the cheaper sibling to reach for first when the primitives are
on disk. Named here so the pipeline seat is honest about what it is NOT the
cheapest answer to.

## Alternatives rejected (summary)

- **A blocking `reproduce` verb.** Holds a ~30-min canary/array inline — the
  MCP-wedge `retarget-run` was built to avoid. Rejected for the non-blocking
  `next_block=submit-s2` hand-off.
- **Supersession linkage.** A reproduction closes nothing; the original stays
  valid. Rejected for a one-directional `reproduces` sidecar field.
- **A cmd_sha-only drift refusal.** `cmd_sha` is param identity only
  (`state/run_sha.py`); an executor-body edit keeps it and reproduces different
  code. Rejected for `state/code_drift.py::detect_code_drift`.
- **run_id-naming-only path separation.** A shared subtree lets the recursive
  reduce cross-contaminate the original's future mean (run #6's 11-row mean).
  Rejected for a disjoint `<orig>-repro` remote_path.
- **Pooled-vs-subset aggregate comparison** for a partial. Compares means over
  different N — noise dressed as a verdict. Rejected for per-task comparison of
  the sampled ids (deferred, severable).
- **Tree-snapshot storage in v1.** Reconstruct-and-pretend manufactures false
  reproductions. Rejected — a moved tree refuses with drift evidence.
- **A journal-home ledger.** A journal wipe destroys the verdict. Rejected for
  the experiment-local append-only `reproduction_receipts.jsonl`.
- **A mismatch-as-error verdict, and metric-name special cases.** Nondeterminism
  discovered is the feature; the comparator owns no metric vocabulary. Rejected
  for a `needs_decision` finding over opaque numbers with caller-owned tolerance.
