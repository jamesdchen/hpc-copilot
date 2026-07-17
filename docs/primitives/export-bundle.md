---
name: export-bundle
verb: mutate
side_effects:
- file_write: <output_path> (default <experiment>/_dossier/<seed>.bundle.zip)
idempotent: true
idempotency_key: seed
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent export-bundle --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.publication_bundle.export_bundle
---
# export-bundle

Assemble the **publication bundle** — the single, offline-verifiable `.zip` a
scientist ships with a paper that says *"here is the proof my table is
reproducible: the minimal recipe, the signed provenance, the audit of every
cited number, the sealed evidence"* — checkable by a third party with **no
cluster access** (`docs/design/publication-bundle.md`).

It is a **sibling** of `export-dossier` (the `export-attestations` precedent),
not an extension: it **composes** the shipped verbs and **reinvents nothing**.
The dossier's run-scoped contract, its closed store vocabulary, and its no-parse
posture are all boundary-pinned and stay untouched. A bundle composes, under one
seal:

1. **the sealed dossier evidence + the minimal recipe** — via the ONE dossier
   gather (`hpc_agent.ops.export_dossier::compute_dossier_signature`), which
   already seals the derived clean-reproduction recipe (BR-4) alongside the
   sidecar, journals, harvested aggregates, and the fingerprint ledger;
2. **the signed provenance manifest** (`provenance-manifest` v3) — the
   wheel-sha + resolved-environment lock, signature-attested **inside** the
   sealed artifact (the dossier does not seal it today);
3. **the cite-check report** — `cite-check`'s per-number audit of the
   **manuscript** against the sealed `aggregated_metrics` values (the one member
   sourced from a new input; it closes the last-mile transcription link).
   Absent a manuscript, it is **disclose-skipped**;
4. **the in-toto/DSSE attestations** — the `export-attestations` projection of
   the same dossier signature (the stock-tooling offline-verify layer);
5. **the top-level `VERIFY` manifest** — a **code-emitted** per-link
   `MECHANICAL` / `DISCLOSED` / `ABSENT` classification, the union-of-disclosures
   ledger, the member pointers, and the offline-verify recipe.

Everything is sealed under one `bundle_sha256` (the ONE signable digest,
`manifest_signature`, one level up). It is a proof-of-what-is-mechanical **plus**
an honest ledger-of-what-is-disclosed — **never** a "reproducibility
certificate". It **discloses, never gates**: a missing manuscript, an absent
campaign, an operator-bypass table, an uncitable number — each is a disclosed
gap on the `VERIFY` manifest; the bundle still seals.

The **BUNDLE-MEMBER vocabulary** (`dossier-evidence`, `provenance-manifest`,
`cite-check-report`, `attestations`, `verify`) lives in the ops module and is
**disjoint** from `DOSSIER_SOURCES` — the cite-check report is a bundle member,
never a dossier store noun (R-B3), so there is no dossier-boundary blast radius
and no `export-attestations` pair-edit.

## Inputs

An `ExportBundleSpec` (`hpc_agent._wire.actions.publication_bundle`):

- **seed** — exactly one of `run_id` / `campaign_id` / `aggregate_path` (the
  `extract-recipe` / `cite-check` seed contract). Validated in the op (one clear
  error on zero / two / three seeds). A pack `*.csv` is an OPAQUE citation
  (never parsed).
- **manuscript** (optional) — one of `manuscript_text` / `manuscript_path` (the
  `cite-check` input). ABSENT is legal (disclose-not-gate): the bundle still
  seals the dossier + recipe + signed manifest and records a `cite-check-skipped`
  disclosure. Supplying both is a spec error.
- `include_lineage` (bool, default `false`) — widen the dossier gather to the
  primary run's whole supersession lineage.
- `output_path` (string, optional) — destination for the `.zip`. Omit to derive
  `<experiment>/_dossier/<seed>.bundle.zip`; the resolved location is echoed back
  as `bundle_path`.

The **primary run** seeds the run-scoped dossier gather: the run itself for a run
seed, the head contributing run for a campaign / aggregate seed. The campaign for
the signed manifest is the campaign seed, or the primary run's sidecar
`campaign_id` (absent → the provenance member is disclose-skipped).

## Outputs

`data` is an `ExportBundleResult`. Every field describes the bundle by
**provenance** or by the code-emitted honest verdict — never by the meaning of
any sealed member:

```
{
  "bundle_path": "<resolved path the .zip was written to>",
  "seed_kind": "run" | "campaign" | "aggregate",
  "seed_ref": "<seed identity / path>",
  "primary_run_id": "<the run whose stores seeded the dossier>",
  "run_ids": ["<run_id>", ...],       // lineage order when include_lineage
  "bundle_sha256": "<64-char hex>",   // the ONE top-level seal
  "member_count": <int>,              // sealed members
  "manuscript_present": <bool>,
  "verdict": "<the code-emitted honest verdict, relayed verbatim>",
  "disclosures": [ {"origin": "...", "code"|"note": "...", "detail": "..."} ],
  "verify_manifest": { <the full self-attesting VERIFY manifest> }
}
```

The `verify_manifest` (also written to the archive as `VERIFY.json`) carries
`bundle_schema_version`, the path-sorted member `entries`
(`{member, path, sha256, bytes}`), the per-link classification `links`, the
`disclosures` ledger, the code-emitted `verdict`, `verdict_meta`
(`claims_reproducible` is always `false`), the `offline_verify` recipe, and
`bundle_sha256`.

## The VERIFY manifest — what it PROVES vs DISCLOSES

**PROVES** (a stranger confirms with sha recompute / signature verify alone):
internal consistency (every member's bytes hash to its `sha256`, sealed by
`bundle_sha256`); the minimal-set attestation (the recipe's signature over only
the contributing runs); the signed provenance (`verify_provenance_manifest`
re-hashes the manifest body — a flipped wheel-sha / env-lock breaks it); and the
transcription-fidelity floor (every `matched` cited number equals a sealed
value).

**DISCLOSES** (inherited from the chain, never laundered into a proof): the data
link is `DISCLOSED` when contributing runs did not declare inputs (opt-in),
`MECHANICAL` when they did; environment is `DISCLOSED` (the full-environment
identity is weak, `env_hash` never gated); an `uncitable` number rides the ledger
as context, never a failure; the recipe's own gaps and the dossier's absent
stores ride through. The verdict **never** stamps "reproducible".

## Offline verify (no hpc-agent)

The `VERIFY` manifest is **self-attesting** exactly as the dossier's
`manifest.json` is. A stranger recomputes it with a ~20-line stdlib script: unzip,
sha256 each member and compare to its entry, then recompute the canonical digest
over the path-sorted `entries` and compare to `bundle_sha256`. The exact
canonicalization is documented in the manifest's `offline_verify` block (the
shared `manifest_signature` definition). The `attestations.jsonl` member
additionally round-trips under stock in-toto / DSSE tooling (parse +
subject-digest comparison). A convenience `verify-bundle` verb (Layer 3 — it
re-classifies the reproducibility links stock tooling cannot) is a noted sibling
follow-on.

## Errors

- `spec_invalid` — the spec did not validate; the seed was not exactly one of
  `run_id` / `campaign_id` / `aggregate_path`; both manuscript sources were
  supplied; a `manuscript_path` / `aggregate_path` does not exist; or the seed
  resolved **no** run to seal a dossier for. Not retry-safe (fix the spec).
  Absent individual stores / an absent manuscript / a missing signed manifest are
  **disclosed**, never fatal.

## Idempotency

Keyed on the **seed**. The bundle is derived state, recomputed from disk on every
call: replaying with the same seed re-seals the same members and the same
`bundle_sha256` (`generated_at` / `tool_version` are excluded from the seal
pre-image), overwriting the archive at the resolved path. Re-export after new
records land (the append-only fingerprint ledger especially) reflects the new
state — a measurement that grew is a new bundle, not a silent mutation of the old
one (the registration-kernel R7 posture, shared with `export-dossier`).

Not MCP-curated: like `export-dossier` / `export-attestations` it is a HUMAN-run
publish step, reachable via the CLI registry but kept out of the curated MCP
catalog.

## Usage

```
hpc-agent export-bundle --spec spec.json --experiment-dir .
```

where `spec.json` is `{"run_id": "<id>", "manuscript_path": "paper.tex"}` (or a
`campaign_id` / `aggregate_path` seed; drop the manuscript to disclose-skip the
cite-check report; add `"include_lineage": true` or `"output_path": "<path>"`).
Verify offline: unzip, recompute each member's sha and the `bundle_sha256` over
the path-sorted entries in `VERIFY.json` — no hpc-agent required.
