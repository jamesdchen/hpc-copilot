---
name: program-verify
verb: query
side_effects:
- filesystem: <experiment>/.hpc/provenance/program-<program_signature[:12]>.json (write-once)
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent program-verify --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.program_verify.program_verify
---
# program-verify

Project the **recorded** reproduction evidence for a whole *program* — the run-set
behind a citable results table — into one disclosed roll-up. This is
program-level reproduction, **phase 1 (receipts-first)**: it reads the
reproduction evidence that already exists (pair receipts + fingerprint samples)
and names the gaps; fresh re-runs are a later phase's ceremony.

A program's identity is **emergent**, never declared up front: it is the minimal
contributing run-set behind a table (the `extract-recipe` seed), or an explicit
constituent list the caller already holds. The verdict **discloses, never gates** —
a not-fully-reproduced program is a `needs_decision` FINDING (exit-0), exactly as
`verify-reproduction` treats a single mismatch.

It is a **pure projection over recorded judgments**, never a new comparator. It
never re-compares a metric, never names one, and mints no new evidence:

- It resolves the run-set from an explicit `run_ids` list, or by reusing
  `extract-recipe`'s walk as a **library call** (never re-deriving the minimal
  set). When that walk degrades to the G4a harvest-receipt proxy, the disclosure
  is passed through in `gaps` exactly as `extract-recipe` emits it.
- For each constituent it gathers the reproduction receipts reachable via the
  `reproduces` back-link (the READ mirror of `verify-reproduction`'s write:
  `_aggregated/<repro_run_id>/reproduction_receipts.jsonl`, each receipt naming
  its `original`), plus the determinism-fingerprint ledger samples for the run's
  identity.
- It classifies each constituent **from the receipt's own `overall` vocabulary** —
  a `needs_verdict` counts as reproduced only when the fingerprint admission join
  (a recorded HUMAN acceptance) says so.

## Inputs

A `ProgramVerifySpec` (`hpc_agent._wire.queries.program_verify`) — exactly one
program-identity source:

- `run_ids` (list of strings) — an explicit constituent list.
- `campaign_id` (string) — an `extract-recipe` seed; the identity is the
  campaign's minimal contributing run-set.
- `aggregate_path` (path) — an `extract-recipe` seed; a reduced-metrics artifact
  whose contributing run-set is the program identity.
- `tolerance` (optional) — a `verify-reproduction` tolerance passthrough, echoed
  for provenance only. program-verify never re-compares, so the tolerance that
  judged each pair is the one recorded on that pair's receipt.
- `--experiment-dir` (path, default cwd) — the experiment root.

## Outputs

`data` is a `ProgramVerifyResult`:

- `resolved_run_ids` — the resolved constituent run-set.
- `constituents` — one `ConstituentVerdict` per run: its `classification`
  (`reproduced_within_tolerance` / `mismatch_on_record` / `evidence_incomparable`
  / `no_reproduction_on_record`), a code-rendered `reason` read off the driving
  receipt's own keys, the `receipt_count` and `repro_run_ids`, the identity legs
  (`cmd_sha` / `tasks_py_sha` / `executor`), the `fingerprint_samples` count, the
  `driving_receipt` verbatim, and the receipt's `env_identity` / `hw_identity` /
  `data_identity` / `diverged_stage` disclosures echoed read-only.
- `reproduced_count` / `total` — the k/N roll-up.
- `overall` — the program fold: the most severe constituent classification
  (`mismatch_on_record` > `no_reproduction_on_record` > `evidence_incomparable` >
  `reproduced_within_tolerance`). A program is `reproduced_within_tolerance` only
  when **every** constituent is.
- `needs_decision` — true unless every constituent reproduced (a FINDING, exit-0).
- `recipe_signature` — the `extract-recipe` seed's signature (null for an explicit
  list); `program_signature` — this program manifest's own deterministic digest.
- `gaps` — the `extract-recipe` identity-walk gaps passed through (`table-run-set-link-absent`,
  `pack-csv-opaque`, `operator-bypass`); empty for an explicit list.
- `manifest_path` — the write-once program manifest; `manifest_delta` — a
  disclosed content-drift delta when a prior manifest for the same seed had a
  different signature.
- `markdown` — the code-rendered program report (deterministic; LLM-free).

## The write-once program manifest

program-verify materializes `.hpc/provenance/program-<program_signature[:12]>.json`,
mirroring `provenance-manifest`'s signed shape (an identity body plus a top-level
`signature`, reusing its `manifest_signature` helper). The body is **identity
only** — the seed, the resolved run-set, and each constituent's fingerprint — so
the verdict (which changes as new receipts land) never churns the signature.
Re-running with the same seed over the same on-disk identity re-derives the same
signature → the same file (write-once, idempotent). A content drift (a changed
run-set or fingerprint) mints a **new** file and discloses the delta.

## Errors

- `spec_invalid` — not exactly one program-identity source, or an `extract-recipe`
  seed that does not resolve (a missing `aggregate_path`). Not retry-safe; fix the
  seed.

## Idempotency

A read-only projection whose only write is the write-once manifest. Re-running the
same seed over unchanged on-disk state is a no-op on the manifest and re-derives
byte-identical output.

## Usage

```
hpc-agent program-verify --spec spec.json --experiment-dir .
```

where `spec.json` is one of `{"run_ids": ["<id>", ...]}`, `{"campaign_id": "<id>"}`,
or `{"aggregate_path": "<path>"}`. Like `extract-recipe` / `trace` /
`provenance-manifest`, program-verify is an operator/reviewer projection reachable
through the CLI registry.
