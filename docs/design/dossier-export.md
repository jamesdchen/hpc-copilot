---
status: shipped
---
# Design: export-dossier — a store-typed evidence bundle

Status: **SHIPPED** (dossier-export wave). The bundler is
`hpc_agent.ops.export_dossier::export_dossier`; the wire surface is
`hpc_agent._wire.actions.export_dossier::ExportDossierSpec` /
`ExportDossierResult`; the integrity seal reuses
`hpc_agent.ops.provenance_manifest::manifest_signature`; the boundary is pinned
by `tests/contracts/test_dossier_boundary.py`. This document is the decision
record — WHY the shape is what it is, and the alternatives rejected. Facts
(symbol names, defaults) cite `path::symbol`; where this doc and the code
disagree, the code and its enforcement-mapped tests win.

## Problem

A run leaves a trail across many core-owned stores — the sidecar, the decision
journal, the drafted briefs, the block terminals, the journal record, the scope
journal, the look ledger, the harvested aggregate. A human (or a repo-side
renderer building a review package, a submission appendix, a reproduction
receipt) needs that whole trail as one portable, integrity-sealed unit. The
question the design had to answer was not "how to zip files" — it was **what
core is allowed to know about the things it bundles**.

## Decisions

### Store-typed entries: typed by SOURCE, never by meaning

The ruling that shapes everything else: a bundled entry is typed by the
**source store it came from**, never by what it means. The framework knows
"this file is a run sidecar" or "this line is a decision-journal record"; it
never knows — and the models never encode — that a record is a "greenlight", a
"holdout result", a "control arm", or any other caller-owned role.

This is the four-question boundary test applied verbatim (see
`docs/internals/engineering-principles.md`, Q1 "substrate, not semantics", and
its four-operations formulation). Core's agnostic surface is exactly
**IDENTITY, ORDERING, COMPARISON, and COUNTING** over opaque caller content.
The dossier decomposes cleanly into those: it **identifies** each entry by store
+ path + `sha256`, **counts** entries per store (`entry_count`, the manifest),
and copies bytes. It never **names** the content — the moment a manifest entry
grew a field like `role`, `treatment`, or `metric`, the bundler would have
crossed from substrate into semantics, the leak the test forbids. That crossing
is why every manifest entry carries **exactly** `{source, path, sha256, bytes}`
and the wire models expose no field name from the domain-semantics vocabulary —
both pinned in `tests/contracts/test_dossier_boundary.py`. The closed
store-noun vocabulary lives in code as
`hpc_agent.ops.export_dossier::DOSSIER_SOURCES`, on the ops side of the boundary,
never on the wire — pinning the store names into the wire schema would freeze an
ops contract into the boundary and leak store enumeration to every client.

The `aggregated` store makes the ruling concrete: its content is copied as
**raw bytes, never parsed**. Core does not `json.load` a harvested aggregate to
"understand" it, because understanding it would mean naming the caller's
metrics — exactly what a domain pack above core owns. The no-parse pin
(`test_bundler_copies_bytes_and_never_parses_content`) enforces this by the
strongest cheap means: the bundler contains **no** `json.load`/`json.loads` at
all — it copies bytes for every source — so the opaque store cannot be parsed
even by accident. (`json.dumps`, used to *sign* the manifest of provenance
records, is untouched; the ban is on reading content back into structure.)

### Manifest-hash determinism, not archive-byte determinism

`bundle_sha256` is the **manifest signature**
(`hpc_agent.ops.provenance_manifest::manifest_signature` — canonical sorted-keys
SHA-256 over the manifest of `{source, path, sha256, bytes}` records), and each
entry carries its own `sha256` over its copied bytes. The integrity contract
rides those hashes end to end: a consumer re-derives the manifest and re-checks
every entry hash after transport.

The archive bytes themselves are **not** guaranteed byte-deterministic. A
byte-deterministic zip is achievable (pin mtimes, member order, compression
level) but buys **nothing here**: no part of the integrity contract hashes the
archive container, so two archives with identical manifest signatures and
identical per-entry hashes are already provably equivalent regardless of zip
framing. Paying for reproducible container bytes would add fragility (platform
zip-metadata quirks) for a guarantee the manifest hash already delivers. So the
determinism guarantee is scoped precisely to the manifest hash and the entry
hashes, and the primitive doc says so.

### `_dossier/` landing convention

An omitted `output_path` derives a conventional path under the experiment's
`.hpc/_dossier/<run_id>.zip` tree — a **derived default, not an agent-authored
one** (the papercut class where an agent hand-composes a path string). The
underscore prefix matches the framework's internal-tree convention (`_broker/`,
`_wire/`), marking `_dossier/` as core-owned scratch a renderer reads, not a
user-facing artifact directory. The resolved location is always echoed back as
`archive_path`, so the caller never guesses where it landed.

### SpecInvalid reuse over a new error class

Bad input raises the shared `errors.SpecInvalid` (`category: user`,
`retry_safe: false`) — the same trace-precedent error every spec-validated verb
uses (e.g. `provenance-manifest`) — rather than minting a dossier-specific
class. A new error class would be a new thing for every caller's error handling
to learn for zero added signal: "your spec did not validate" is already the
whole message, and the trace carries the field-level detail. Follow the
precedent; do not grow the taxonomy.

### MCP is NOT curated — and the revisit trigger

`export-dossier` is a CLI/Python verb, **not** exposed as a curated MCP tool.
The curated MCP catalog is the human-amplification block loop (the block verbs,
`block-drive`, `append-decision`) plus the recovery/opt-in verbs; bundling a
run's record trail is an operator/renderer action, not a decision point in a
submit/aggregate loop, so it does not belong in the agent's typed-tool surface.
Exposing it would invite an in-session agent to bundle-and-interpret, which is
the substrate/semantics line this whole feature defends.

**Revisit trigger** (recorded so the next "should this be an MCP tool?" has a
concrete bar): expose it as a curated tool **only** on evidence of an agent
hand-rolling a bundle — improvising a zip of `.hpc/` stores through raw shell
because no verb was reachable. That improvisation is the same class the
block-drive `next_block` curation kills; if it appears, the fix is to surface
the verb, not to keep it hidden. Absent that evidence, MCP curation stays off.

### Extensibility: a new source is a string, not a schema change

The manifest is keyed by source-store name and each entry names its `source`;
adding a new store to a future bundle (the anticipated case: a
**reproduction-receipt** store, per `docs/design/reproduction-receipt.md`) is a
new **string** in `hpc_agent.ops.export_dossier::DOSSIER_SOURCES` and a new
gather branch — **zero wire-schema change**. The wire deliberately carries the
manifest as a bare mapping (not a typed-per-store model) exactly so the store
vocabulary can grow in the ops layer without a boundary edit. The closed-set
equality test forces each addition through review, but the schema never moves.

### Lineage bundling via the one shared walk

`include_lineage` widens the bundle from the single run to its whole
supersession chain using `hpc_agent.state.scopes::lineage_chain` — the **one**
supersession walk the rest of the framework already uses (the look ledger's
`distinct_lineages`, the reduction gate). There is no second lineage definition
here: reusing the single walk means the dossier's notion of "this run's history"
is byte-for-byte the same as every other consumer's, and `run_ids` is reported
in that walk's newest→root order.

## Boundary pins

Enforced by `tests/contracts/test_dossier_boundary.py`:

| Pin | Fires when |
|---|---|
| Entry shape | any manifest-entry construction's key set ≠ `{source, path, sha256, bytes}` (AST scan, builder-name-agnostic) |
| Forbidden vocabulary | either wire model exposes a field NAME in the domain-semantics set, or `DOSSIER_SOURCES` ≠ the closed store-noun set |
| No parse | `export_dossier.py` calls `json.load`/`json.loads` (the bundler copies opaque bytes; parsing would name the content) |
