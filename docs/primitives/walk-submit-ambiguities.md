---
name: walk-submit-ambiguities
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent walk-submit-ambiguities --spec <path>
  python: hpc_agent.ops.walk_submit_ambiguities.walk_submit_ambiguities
---
# walk-submit-ambiguities

Runs the `hpc-submit` SKILL Steps 2-6 resolution as deterministic CODE
branches instead of LLM-walks-and-fills prose, and returns the same
`needs_resolution`-shaped envelope the SKILL produced:
`{resolved, ambiguities}`. The resolution rules and the field partition
(which fields may carry a `safe_default`) live in code, so the LLM only
resolves the genuine ambiguities the walk surfaces.

This is a **new sibling** of
[`resolve-submit-inputs`](resolve-submit-inputs.md), which is left
byte-for-byte unchanged (the campaign delegates to it). The two are
different rings: `resolve-submit-inputs` is the post-decision
input-resolution spine (scaffold `tasks.py` → run_id → spec);
`walk-submit-ambiguities` is the pre-decision ambiguity walk that decides
which fields still need the caller.

## Inputs / outputs

See `hpc_agent/schemas/walk_submit_ambiguities.{input,output}.json`. Each
caller-supplied field short-circuits its resolution step (the
"caller-supplied is authoritative" contract). The output is
`{resolved, ambiguities, provenance}`; the matching envelope `error_code`
is `needs_resolution` when `ambiguities` is non-empty.

## The walk (never early-returns on the first miss)

- **Step 2 — cluster:** caller, else the single configured cluster, else
  an ambiguity (`safe_default` = first lexicographically).
- **goal / task_generator (REQUIRED_CALLER_FIELDS):** surfaced as
  ambiguities **without** a `safe_default` — constructing the `Ambiguity`
  with one would raise (the partition guard). This is the point: the
  resolution path *structurally cannot* express a fabricated sweep
  (incident 1b). `task_generator` is only an ambiguity when no
  hand-written `tasks.py` exists (the sanctioned hand-written path).
- **Step 3 — entry_point:** ambiguity with the first candidate as default
  when unresolved.
- **Step 3b — uncovered_param (#195):** dict-shaped `safe_default`,
  `{param: <argparse default if any, else null>}`. The `{param: null}`
  slot is *present* (not absent) — the guard tests `is not None`, so a
  null default is correctly treated as a present, allowed default.
- **Step 4 — data_axis:** ambiguity (`safe_default` = `{kind: sequential}`,
  `depends_on: [entry_point]`).
- **Step 5 — homogeneous_axes:** ambiguity (`safe_default` = `[]`).
- **Step 6 — resources:** **reuses**
  [`resolve-resources`](resolve-resources.md) for `walltime_sec` /
  `gpu_type` / `partition` / `mpi_pe`. These always auto-resolve (a
  missing runtime prior is cold-start, not an ambiguity), so they land in
  `resolved`, never `ambiguities`.

## The partition is the lock

The two-class field partition
(`hpc_agent.ops.submit.field_partition`) decides which fields may carry a
`safe_default`. AUTO_RESOLVABLE_FIELDS may; REQUIRED_CALLER_FIELDS may
not, and the `Ambiguity.__post_init__` guard fires (raises) if asked to
attach one. So this walk cannot emit a `task_generator` ambiguity with a
fabricated recipe in its `safe_default` slot — the object refuses to
exist.

## requires_ssh: False

Pure-local: cluster connectivity, prior store, and `clusters.yaml` reads
flow through the composed `resolve-resources`, which is itself local-only.
