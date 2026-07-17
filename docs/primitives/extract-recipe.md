---
name: extract-recipe
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent extract-recipe --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.extract_recipe.extract_recipe
---
# extract-recipe

Walk a **citable artifact** back to its **minimal contributing run-set** and emit
one deterministic clean-reproduction recipe. This is the product one-liner —
"what changed since last-known-good, answered mechanically instead of by
archaeology" — applied at *publication time*: given the table a scientist wants
to cite after a messy multi-run exploration (dead ends, retargets, superseded
runs, an operator-bypass reduce), it answers the four mechanical questions —
*which* runs produced the numbers, *from what* provenance, *how* to re-derive,
and *proven how* — without the human grepping a journal of dead ends.

It **composes the shipped walks**, it does not reinvent them: the reduce-time
`contributing_run_ids` provenance (`ops/aggregate_flow`, Task 1), the
supersession `lineage_chain` (`state/scopes`), the canary-family suffix
definition (`sibling_run_ids` / `canary_parent_of`, `ops/monitor/reconcile`), the
harvest-receipt ledger (`harvest_receipt_exists`, `ops/monitor/harvest_guard`),
the campaign run / sidecar finders, and the signable `manifest_signature`
(`ops/provenance_manifest`). Since R3 (manifest schema v2) the wheel sha
(`hpc_agent_version`) is a **signed** field of the provenance manifest; when a
written, signature-verified v2 manifest carries a contributing run, this verb
**prefers** that signed value over the sidecar projection and discloses which
source it used per run (`hpc_agent_version_source` — `signed-manifest` vs
`sidecar`). Absent a signed source, the sidecar projection stands.

Read-only and client-side: no SSH, no scheduler, no write. Derived state,
recomputed from the on-disk records on every call, so it can never drift from a
second source of truth.

## Inputs

An `ExtractRecipeInput` (`hpc_agent._wire.queries.extract_recipe`) — exactly one
seed reference:

- `run_id` (string) — walk back from this run's persisted table (its
  `_aggregated/<run_id>/metrics_aggregate.json` contributing set, or its
  supersession lineage when no table was persisted).
- `campaign_id` (string) — walk back from this campaign — its runs minus the
  canary / superseded / dead-end members.
- `aggregate_path` (path) — a reduced-metrics artifact. A `metrics_aggregate.json`
  is read for its `contributing_run_ids` provenance; a pack `*.csv` is accepted
  only as an **opaque** citation whose content is NEVER parsed (the dossier
  no-parse boundary) and whose provenance is its containing run's.
- `--experiment-dir` (path, default cwd) — the experiment root.

## Outputs

`data` is an `ExtractRecipeResult`:

- `minimal_run_ids` — the minimal contributing run-set, after all exclusions.
- `runs` — one fingerprint per minimal run: `{run_id, cmd_sha, tasks_py_sha,
  data_sha, data_manifest_sha, env_hash, hpc_agent_version, cluster, profile,
  hpc_agent_version_source}` — identity fields only, **no metric value** (the
  wheel sha the directive names is present on every row;
  `hpc_agent_version_source` discloses whether it came from a `signed-manifest`
  or the `sidecar`).
- `excluded` — one `{run_id, reason}` per mechanically-excluded run, where reason
  is `canary` / `superseded` / `dead-end`. Every exclusion is a disclosed,
  countable fact.
- `recipe_signature` — a deterministic 64-hex digest over **only** the minimal
  set's fingerprints (a table-specific attestation, not a whole-campaign one): a
  reviewer re-derives the recipe and re-hashes to confirm the set has not drifted.
- `rederivation_steps` — the runnable re-derivation steps as structured hints (a
  `reproduce-run` + `submit-s2` pair per run, then the aggregate invocation),
  emitted as a runnable artifact, not prose. `extract-recipe` NEVER executes them.
- `receipts` — the receipts chain per run: `{run_id, harvest_receipt (bool),
  reproduction_receipt (bool), greenlights (count)}` — presence / counts only.
- `gaps` — every receipts-chain gap the walk could not bridge, DISCLOSED never
  papered: `table-run-set-link-absent` (G4a — a pre-Task-1 table keeps no
  contributing set), `pack-csv-opaque` (G4b — a non-json pack table cited as
  opaque, provenance is its containing run's), `operator-bypass` (G4d — a table
  reduced outside the sanctioned flow: numbers operator-settled, provenance
  human-asserted).
- `artifact_opaque` — true when the cited artifact was accepted as an opaque
  citation (its content never parsed).
- `markdown` — the code-rendered recipe (deterministic; LLM-free render path).

## Errors

- `spec_invalid` — not exactly one seed was supplied, or `aggregate_path` names a
  file that does not exist. Not retry-safe; fix the seed.

## Idempotency

A pure query with no side effects. Derived state recomputed from disk on every
call — replaying after more submits simply reflects the runs now on disk. The
`recipe_signature` is a FINGERPRINT (verifiable against a re-derive), not an
attestation.

## Boundary posture

The recipe is IDENTITY (which runs, at which shas) + ORDERING (the re-derivation
steps) + COUNTING (exclusion / receipt counts) over opaque records — it never
names a metric, never picks a "best" run, never concludes. Pinned by
`tests/contracts/test_extract_recipe_boundary.py` (the `run_story` / `trace`
precedent).

## Usage

```
hpc-agent extract-recipe --spec spec.json --experiment-dir .
```

where `spec.json` is one of `{"run_id": "<id>"}`, `{"campaign_id": "<id>"}`, or
`{"aggregate_path": "<path>"}`. Like `trace` / `provenance-manifest` /
`run-story`, `extract-recipe` is deliberately **NOT MCP-curated**: it is an
operator/reviewer projection, and the curated catalog is a deliberate
human-amplification allowlist (the MCP-is-projection ruling), so the verb is
reachable through the CLI registry but not advertised as a curated tool.
