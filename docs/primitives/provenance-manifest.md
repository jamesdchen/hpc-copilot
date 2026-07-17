---
name: provenance-manifest
verb: mutate
side_effects:
- file_write: <experiment>/.hpc/provenance/<campaign_id>.json
idempotent: true
idempotency_key: campaign_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent provenance-manifest --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.provenance_manifest.provenance_manifest
---
# provenance-manifest

Build and atomically write the per-campaign provenance manifest —
**one signable artifact** pairing every `run_id`/`trial_token` of a
campaign with its full `{code, data, env, params, cluster}`
fingerprint, so that given any result you can reconstruct exactly what
produced it (#222/#312). The manifest is *derived* state: it is
recomputed from the run sidecars on every call, never a second source
of truth that can drift. The per-run fields are an explicit allowlist
(`cmd_sha`, `tasks_py_sha`, `data_sha`, `env_hash`,
`hpc_agent_version`, `cluster`, `profile`, `submitted_at`,
`trial_tokens`); a field a sidecar never recorded is emitted as `null`
so the shape is uniform across sidecar vintages.

`hpc_agent_version` (the wheel sha — the code VERSION) joined the signed
allowlist in **schema v2** (R3): the signature that attests {code, data,
env, params} now also covers the version of the framework that produced
the run. A sidecar with no recorded version projects an explicit signed
`null` marker — never a silent omission. **Read-compat:** a v1 manifest
(no wheel field) already on disk / a cluster still verifies — its
signature was computed over the v1 field-set and stays valid;
`verify_provenance_manifest` re-hashes the on-disk body *as written*, and
the signed `manifest_schema_version` in that body tells the verifier
which field-set was hashed. An unknown/future version is refused, not
silently trusted.

`data_sha` is non-null for runs whose submit spec declared
`input_datasets` (auto-captured at sidecar-write time, like
`env_hash`); a DVC-tracked input contributes the `.dvc` pointer's
recorded md5, so the real bytes never need to be on disk.

## Inputs

- `campaign_id` (string, required) — the campaign tag stamped on each
  run sidecar at submit time (the `HPC_CAMPAIGN_ID` convention). Path
  separators are sanitized (`/` → `_`) for the output filename.

## Outputs

`{"path": "<experiment>/.hpc/provenance/<campaign_id>.json",
"campaign_id": "...", "run_count": N, "signature": "<64-hex>"}`.

The written file is the manifest body plus a top-level `signature`
(a SHA-256 over the canonical-JSON body, excluding the signature
itself) — self-attesting: a reader strips `signature`, re-hashes, and
confirms the match. Record the returned `signature` (commit message,
paper appendix) to attest "these results were produced by exactly
these {code, data, env, params}".

## Errors

- `spec_invalid` — malformed spec (empty `campaign_id`, unknown keys).

An unknown campaign is **not** an error: it yields a well-formed
manifest with `run_count: 0` — the absence of runs is itself a
provenance fact worth recording.

## Idempotency

Idempotent by construction (key: `campaign_id`). The manifest is
recomputed from the sidecars on every call, so replaying after more
submits refreshes the file to match the runs on disk; replaying with
no new runs rewrites identical content (and an identical signature).

## Notes

Client-side only — reads local sidecars, no SSH, no cluster footprint,
DVC optional. The natural call point is end-of-campaign (after the
final `aggregate-flow`), but any moment is valid since the output is
always consistent with the sidecars at call time.
