---
name: export-attestations
verb: mutate
side_effects:
- file_write: <output_path> (default <experiment>/_dossier/<run_id>.attestations.jsonl)
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent export-attestations --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.export_attestations.export_attestations
---
# export-attestations

Project a run's **sealed dossier evidence** into portable
[in-toto](https://in-toto.io/) **Statements**, one per sealed store entry, each
wrapped in an **unsigned DSSE envelope**, written as a JSONL stream — so
ecosystem tooling can verify the bundle **without hpc-agent**. This is the
**portability layer** over `export-dossier`'s **sealing layer**: a sibling
verb, not an extension (the dossier's manifest-entry shape, closed store
vocabulary, and no-parse posture are all boundary-pinned and stay untouched —
see `docs/design/conformance-kit.md` D-K4).

The verb **delegates the gather** to the dossier's one signature seam
(`hpc_agent.ops.export_dossier::compute_dossier_signature`) — the stores are
never walked twice — and projects each sealed `{source, path, sha256, bytes}`
entry into a Statement whose:

- `subject` digest is **copied verbatim** from the dossier entry (never
  recomputed here);
- `predicateType` is the entry's **source-store noun** mapped through the
  closed URI vocabulary `https://hpc-agent.dev/attestation/<store-noun>/v1`
  (one URI per `DOSSIER_SOURCES` noun, equality-pinned by
  `tests/contracts/test_attestation_export_boundary.py`);
- `predicate` embeds the store's **raw bytes verbatim** (UTF-8 text, or base64
  for non-UTF-8 content) — the export **never parses** the records it attests
  (the dossier no-parse boundary, extended).

A Statement is typed by the **source store its bytes came from, never by what
it means** — the same substrate-not-semantics line the dossier holds
(`docs/internals/engineering-principles.md` Q1). Signing is a future concern:
v1 envelopes carry `signatures: []`, and the DSSE shape means adding a
signature later changes nothing upstream.

## Inputs

An `ExportAttestationsSpec` (`hpc_agent._wire.actions.export_attestations`):

- `run_id` (string, required) — the run whose sealed evidence is projected
  (`RunIdStrict`, the filesystem-safe run-id slug).
- `output_path` (string, optional) — destination path for the JSONL bundle.
  Omit to let the verb derive the conventional
  `<experiment>/_dossier/<run_id>.attestations.jsonl`; the resolved location is
  echoed back as `output_path`. A derived default, not an agent-authored one.
- `include_lineage` (bool, default `false`) — when `true`, project the run's
  whole supersession lineage (the run plus every run it superseded, back to the
  lineage root), exactly as `export-dossier` resolves it. The projected set and
  its lineage order are reported in `run_ids`.

## Outputs

`data` is an `ExportAttestationsResult`. Every field describes the bundle by
**provenance**, never by the meaning of any Statement:

```
{
  "output_path": "<resolved path the JSONL was written to>",
  "run_ids": ["<run_id>", ...],       // lineage order, newest→root
  "statement_count": <int>,           // one Statement per sealed store entry
  "bundle_sha256": "<64-char hex>",   // the delegated dossier signature
  "gaps": [ {<free-shape record>}, ... ]
}
```

Each **line** of the output file is one DSSE envelope:

```
{
  "payloadType": "application/vnd.in-toto+json",
  "payload": "<base64 of the canonical (sorted-keys) Statement JSON>",
  "signatures": []                     // unsigned v1, DSSE-ready
}
```

and each decoded payload is one in-toto Statement:

```
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [ { "name": "<archive path>", "digest": { "sha256": "<entry sha>" } } ],
  "predicateType": "https://hpc-agent.dev/attestation/<store-noun>/v1",
  "predicate": { "contentType": "<...>", "content": "<the store's bytes verbatim>" }
}
```

`bundle_sha256` is identical to `export-dossier`'s for the same run set and
on-disk state (both route through the one signature seam), so a consumer can
tie the attestations back to the exact sealed bundle. `gaps` carries the
dossier gather's expected-but-absent stores through unchanged: an absent store
yields no Statement, is reported, and is never fatal.

## Errors

- `spec_invalid` — the spec did not validate, or the run has **neither** a
  sidecar **nor** a journal record (nothing to attest — the same missing-run
  guard as `export-dossier`; the guard lives in the shared gather seam). Not
  retry-safe (fix the spec).

## Idempotency

Keyed on `run_id`. Re-running against the same on-disk state re-derives the
same Statements and the same `bundle_sha256`; the JSONL is rewritten at the
resolved `output_path`. Re-export after new records land (the append-only
fingerprint ledger especially) reflects the new state — a measurement that grew
is a new attestation bundle, not a silent mutation of the old one (the
registration-kernel R7 posture, shared with `export-dossier`).

## Usage

```
hpc-agent export-attestations --spec spec.json --experiment-dir .
```

where `spec.json` is `{"run_id": "<id>"}` (add `"include_lineage": true` to
project the whole supersession chain, or `"output_path": "<path>"` to override
the derived landing path). Verify a bundle with stock in-toto tooling: parse
each line's payload as a Statement and compare each subject digest against the
matching entry in the exported dossier's `manifest.json`.
