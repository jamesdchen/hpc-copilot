---
name: export-dossier
verb: mutate
side_effects:
- file_write: <output_path> (default <experiment>/_dossier/<run_id>.zip)
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent export-dossier --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.export_dossier.export_dossier
---
# export-dossier

Bundle a run's **core-owned record trail** into one integrity-sealed archive.
The verb walks the run's concrete on-disk **source stores** — the sidecar, the
decision journal, the drafted briefs, the block terminals, the journal record,
the scope journal, the look ledger, the harvested aggregate, the
determinism-fingerprint ledger, and — when the run was submitted from an audited
source (notebook-audit T14) — the audited source `.py` + template `.py` and the
notebook attestation journal — copies each store's entries **verbatim as
bytes**, and writes them into a single portable archive with a manifest and a
bundle fingerprint.

When the run's sidecar echoes an `audited_source` block, the bundle also seals
the **audit trail** — the "dossier is sealed attestations" formulation: the
source `.py` and its template `.py` (both under the `audited-source` store noun,
distinguished by archive path), the `.hpc/notebooks/<audit_id>.decisions.jsonl`
attestation journal (the `notebook-journal` noun), and the trusted-display
render files under `.hpc/renders/<audit_id>/` (the `renders` noun — the
content-addressed *what-the-human-saw* the T8 sign-off gate required, so the
dossier can reproduce it). A missing declared file (or an audit with no renders
on disk) is a recorded gap; a run submitted without an audit is byte-identical to
the pre-T14 dossier (no audit stores, no audit gap). The opaque `audit_id` slug enters the
run's identity projection (which audit sealed it) — the section-level semantics
inside the audit stay opaque.

The bundle also seals the run's **determinism-fingerprint ledger** (the
`cmd_sha`-addressed `_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl`, the
`determinism-fingerprint` store noun) as **raw bytes** — the disclosure-at-
graduation surface for the experiment's measured run-to-run spread. The bundler
seals the FILE, never the derived envelope (the envelope lives in the code-
rendered briefs) and never parses the JSONL. A resolvable identity whose ledger
was never minted records a gap; a run with no `cmd_sha` seals nothing. Because
the ledger is append-only, **every new sample moves the sealed bytes** — a
registration's dossier leg reads stale once fresh evidence accrues, and
re-export + re-register is the deliberate remedy (registration-kernel R7): a
measurement that grew is a new dossier, not a silent mutation of the old one.

An entry in the bundle is typed by the **source store it came from, never by
what it means**. The framework knows "this file is a run sidecar" or "this line
is a decision-journal record"; it never knows — and this verb never encodes —
that a record is a "greenlight", a "holdout result", or any other caller-owned
role. Repo-side renderers build evidence packages (a review PDF, a submission
appendix) *from* the dossier; **core never knows those renderers exist**. The
dossier is the substrate; meaning is applied above it. See the boundary ruling
in [`docs/design/dossier-export.md`](../design/dossier-export.md) and the
four-question test in
[`docs/internals/engineering-principles.md`](../internals/engineering-principles.md)
(Q1, "substrate, not semantics").

## Inputs

An `ExportDossierSpec` (`hpc_agent._wire.actions.export_dossier`):

- `run_id` (string, required) — the run whose stores are bundled
  (`RunIdStrict`, the filesystem-safe run-id slug).
- `output_path` (string, optional) — destination path for the archive. Omit to
  let the verb derive a conventional path under the experiment's `.hpc/_dossier/`
  tree; the resolved location is echoed back as `archive_path`. A derived
  default, not an agent-authored one.
- `include_lineage` (bool, default `false`) — when `true`, bundle the run's
  whole supersession lineage (the run plus every run it superseded, back to the
  lineage root) instead of the single run. The bundled set and its lineage order
  are reported in `run_ids`. The chain is the one walk shared with the rest of
  the framework, `hpc_agent.state.scopes::lineage_chain`.

## Outputs

`data` is an `ExportDossierResult`. Every field describes the bundle by
**provenance** (which stores, how many entries, what identities), never by the
meaning of any entry:

```
{
  "archive_path": "<resolved path the archive was written to>",
  "run_ids": ["<run_id>", ...],        // lineage order, newest→root
  "bundle_sha256": "<64-char hex>",    // manifest signature (see determinism)
  "entry_count": <int>,                // entries copied across every store
  "gaps": [ {<free-shape record>}, ... ],
  "manifest": { ... }                  // keyed by source-store name
}
```

The `manifest` is keyed by source-store name; the closed store-name vocabulary
is `hpc_agent.ops.export_dossier.DOSSIER_SOURCES` (owned by the ops bundler, not
the wire). Each manifest **entry** is a store-provenance record with **exactly**
these keys:

```
{
  "source": "<one of DOSSIER_SOURCES>",  // which store this entry came from
  "path":   "<path inside the archive>",
  "sha256": "<64-char hex over the entry bytes>",
  "bytes":  <int>                         // size of the copied content
}
```

There is no fifth key. An entry names *where content came from and proves its
integrity* — it never carries a field for what the content means.

## Gaps semantics

A store the bundler **expected but did not find** — a run in the lineage with no
journal record, an absent sidecar, a run that never opened a scope journal — is
recorded in `gaps` (a free-shape record naming the missing source store and the
run it belonged to) and **excluded from the manifest**. Gaps are **reported,
never silently dropped, and never fatal**: a bundle with gaps is still written,
and the reader sees exactly which stores were absent. Absence is a provenance
fact worth recording, not an error.

## Determinism

The **`bundle_sha256` is stable**: it is the manifest signature
(`hpc_agent.ops.provenance_manifest::manifest_signature`) — a canonical
sorted-keys SHA-256 over the manifest of store-provenance records. The same run
in the same on-disk state yields the same `bundle_sha256`, and each entry's
`sha256` re-verifies its copied bytes after transport.

The **archive bytes are NOT guaranteed byte-deterministic** — zip container
metadata (mtimes, ordering, compression framing) varies across runs and
platforms. This is deliberate: the integrity contract rides the manifest hash
and the per-entry hashes, so byte-identical archives would buy nothing. Consumers
verify the manifest signature and each entry's `sha256`, never the archive's raw
bytes. The reasoning is recorded in the design record.

## Errors

- `spec_invalid` — the spec did not validate (bad/absent `run_id`, malformed
  `output_path`). Reuses the shared trace-precedent error class rather than
  minting a dossier-specific one; not retry-safe (fix the spec).

## Idempotency

Keyed on `run_id`. Re-running against the same on-disk state re-derives the same
manifest and the same `bundle_sha256`; the archive is rewritten at the resolved
`archive_path` (the derived `.hpc/_dossier/<run_id>.zip` default, or the caller's
`output_path`). Re-export after new records land reflects the new state.

## Usage

```
hpc-agent export-dossier --spec spec.json --experiment-dir .
```

where `spec.json` is `{"run_id": "<id>"}` (add `"include_lineage": true` to
bundle the whole supersession chain, or `"output_path": "<path>"` to override
the derived landing path). The archive is the hand-off unit a repo-side renderer
consumes; core hands over the sealed substrate and stops there.
