# Design: the experiment-agnostic DAG kernel

Status: **landed through wiring step 4** (0.10.51). Recursive identity
(`compose_node_sha` in `state.run_sha`, property suite
`tests/state/test_node_sha_properties.py`) and its submit-side wiring —
`parents` on the submit spec → `resolve_node_sha` derives `node_sha`
from the parents' sidecars → `node_sha`/`parent_run_ids` persist on the
v2 sidecar → `find_run_by_cmd_sha` keys dedup on the effective identity —
plus the readiness validator (`validate-parents-ready`) and the lineage
accessor (`parent_records`) are implemented and tested. Step 5 (topology
execution) stays caller-side by design, not deferred work. The 0-parent
degeneracy keeps every parentless submit byte-for-byte unchanged:
identity is its bare `cmd_sha`, the new sidecar keys are omitted, and the
dedup query is the historical one.

## Problem

[`campaign-seam.md`](../design/campaign-seam.md) deliberately excludes
"true DAG pipelines" ("Snakemake/Nextflow's job. A campaign is
*iteration*, not a pipeline"). That exclusion is about scope, not
possibility — but as written it leaves no record of *what* an in-scope
DAG layer would be if the exclusion were ever revisited, which invites
two failure modes:

1. A future feature request ("propagate stage N's outputs into stage
   N+1") gets answered with experiment-specific machinery (a privileged
   "posterior" field, typed stage names) that fails the
   [four-question boundary test](../internals/engineering-principles.md)
   (Q1: substrate, not semantics).
2. The pieces that are *already* agnostic and present — `prior_records()`
   artifact lineage, journal-authoritative terminal lifecycle, canonical
   content-hash identity — get re-invented instead of generalized.

This page records the residue: apply the boundary test to inter-run
dependency and keep exactly what survives. The answer is four pieces,
three of which exist in linear (campaign) form. One — recursive
identity — existed in no form, and landed first,
because without it the other three are unsafe to build: memoized resume
over a run graph that keys nodes by bare `cmd_sha` silently reuses a
stale child when an ancestor's params change.

## The kernel (everything that survives the boundary test)

| Piece | Core knows | Status |
|---|---|---|
| Partial order | node = a submit spec; edge = "before" (`parents` on `SubmitFlowSpec`). Pure graph structure. | edge declaration landed; graph *walking* stays caller-side (step 5) |
| Readiness | "every parent reached an authoritative terminal lifecycle" (journal, not the filesystem `complete` flag) | landed — `validate-parents-ready`, the ∀-parents quantifier over `mark-run-terminal`-style per-run lifecycle |
| Lineage | hand a node its parents' `run_id`s + `result_dirs` as opaque paths | landed — `parent_records()`, the explicit-set sibling of `prior_records()` |
| Recursive identity | `node_sha = H(canonical({node: cmd_sha, parents: sorted(set(parent node_shas))}))` | landed — `compose_node_sha`, wired through `resolve_node_sha` → sidecar → dedup |

Everything outside the table is irreducibly caller-owned and must stay
out of core:

- **Edge meaning.** Core hands paths across an edge; format adaptation
  and validity checks are the experiment's. An edge is a set of opaque
  strings — exactly the `prior_records` discipline ("the framework hands
  back paths; the strategy decides what's inside").
- **Conditional topology.** "Only fan out if upstream converged" needs
  no predicate language: a node's `tasks.py` reads its parents' opaque
  artifacts and materializes `total() == 0` to veto itself. Campaigns
  already converge this way; a DAG node vetoing is the same convention
  at the only place user code already runs.
- **Stage vocabulary.** No stage names, no "objective", no typed
  inter-stage payloads.

## Recursive identity (the landed prototype)

`compute_cmd_sha` is parameter identity for a single run (#207). It does
not compose: if run B consumed run A's outputs and A's params change,
B's `cmd_sha` is unchanged, so a resubmit of B dedups against a result
computed from a *different* A. The Make/Nextflow `-resume` property —
never reuse a node whose ancestry changed — is expressible purely in
hashes, which makes it substrate by the boundary test (Q1: hashing and
key-sorting, no parameter meaning; Q3: stdlib-only; Q4: testable with
synthetic digests).

`compose_node_sha(cmd_sha, parent_node_shas)` is the Merkle step. Pinned
properties (`tests/state/test_node_sha_properties.py`):

- **0-parent degeneracy**: `compose_node_sha(c, []) == c`. Every
  existing run is a 0-parent node; today's dedup keys, sidecars, and
  journal entries need no migration.
- **Parents are a set**: order-invariant, duplicate-insensitive.
- **Ancestor propagation**: a grandparent change propagates through the
  parent digest into the child (tested transitively).
- Parameter identity, not code identity: parents fold in their
  *params*' digests, never executor bytes — the same #207 boundary as
  `cmd_sha`, including the `invalidate_on_code_change` opt-in story.

The campaign-iteration dedup fix (seam piece 2 — landed as the
same-campaign *rejection* in `find_run_by_cmd_sha`, not the salt the seam
doc first sketched) is in hindsight a special case of the same need:
identity must distinguish runs by their position in a dependency
structure, there a linear iteration order, here an ancestry.

## Wiring plan (steps 1–4 landed, in dependency order)

1. **Landed.** `parents: [run_id] | None` on `SubmitFlowSpec` (and
   `parent_run_ids` on `WriteRunSidecarInput`). At sidecar-write,
   `state.runs.resolve_node_sha` reads each parent's recorded identity
   (its `node_sha`, else bare `cmd_sha`) and composes this run's
   `node_sha` via `compose_node_sha`; `node_sha` + `parent_run_ids`
   persist as additive v2 sidecar fields. Identity is always *derived*
   from on-disk sidecars, never caller-asserted — a supplied `node_sha`
   could decouple a child from its real ancestry.
2. **Landed.** `find_run_by_cmd_sha` gained a `node_sha` arg and matches
   on the *effective* identity (`node_sha or cmd_sha`) on both sides: a
   parented query dedups only against the same params AND ancestry; a
   bare query skips parented sidecars. `node_sha=None` (every pre-DAG
   caller) is the historical bare-`cmd_sha` path. Threaded from
   `submit_flow` → `submit_and_record` behind the same opt-in gate as
   the #207 code-drift lever.
3. **Landed.** `validate-parents-ready` (`ops.validate.parents_ready`):
   the ∀-parents quantifier over sidecar presence + journal lifecycle;
   ok iff every parent is `complete`. A pure-local `validate`-verb
   primitive. `submit-pipeline` composes it mechanically when the
   embedded spec declares `parents` (self-skipping otherwise), returning
   a typed `parents_not_ready` refusal on findings — a gate, not a loop:
   wait/fix/drop-the-edge stays caller judgment. Bare `submit-flow`
   callers compose it themselves.
4. **Landed.** `parent_records(experiment_dir, parent_run_ids)` in
   `reduce.history` — same record shape as `prior_records` but resolved
   from an explicit dependency set (ordered, deduped, fails loud on a
   missing parent). The child's `tasks.py` reads it at module load for
   its inputs; callers forward the run_ids cluster-side via `job_env`
   (`HPC_PARENT_RUN_IDS`), same convention as `HPC_CAMPAIGN_ID`.
5. **Caller-side by design (not landed, not deferred work).** Topology
   walking — deciding which node is runnable and firing its submit — is
   the agent surface's job (or an external orchestrator's), consistent
   with the campaign driver's on-disk-state-only design. A framework-side
   graph *runner* is out of scope until repeated mechanical agent walks
   justify a composite, per the `submit-pipeline`/`campaign-run`
   precedent. The walk's *observation* half is code, though:
   `dag-frontier` reconstructs the recorded graph from sidecar
   `parent_run_ids` (per-node lifecycle state, the complete-runs
   frontier, transitive blocking ancestors — pure local read, shares
   `observe_run_state` with the gate). It computes and stops; it also
   instruments hand-walks, producing the uniform evidence "prove
   mechanical" needs before any advance-tick/runner composite is
   considered.

## Non-goals

- No scheduler-native DAG features (`qsub -hold_jid`, SLURM
  dependencies): readiness must consult the journal lifecycle and
  aggregation state, not just scheduler exit — and cross-cluster edges
  exist. Scheduler holds also collide with the no-`scancel` invariant's
  "stop polling and let it expire" abandonment story.
- No retry/backfill policy at the graph level (mirrors the campaign
  loop's deliberate no-auto-retry stance).
- No early-kill interaction (#228 unchanged).
- Nothing in this design privileges any experiment vocabulary — a PR
  adding a typed inter-stage payload fails review by Q1 regardless of
  what this doc says.

## Open questions

1. Should `node_sha` fold in the parent edge's *selection* (which subset
   of a parent's `result_dirs` the child reads)? Current answer: no —
   selection is edge meaning, caller-owned; the child's `tasks.py`
   materializes whatever it selected into its own kwargs, which `cmd_sha`
   already hashes.
2. Does a parent's `tasks_py_sha` participate via
   `invalidate_on_code_change=True` transitively? Deferred with the same
   opt-in default as single-run dedup.

## Related

- [`campaign-seam.md`](../design/campaign-seam.md) — the exclusion this
  page scopes; seam pieces 1–3 (trial_token, iteration salt,
  `prior_records`)
- [`engineering-principles.md`](../internals/engineering-principles.md) —
  the boundary test applied throughout
- #207 — `cmd_sha` param-identity semantics (`node_sha` inherits them)
- #218 — strategy-agnostic campaign seam (tracking)
