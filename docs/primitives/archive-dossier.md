---
name: archive-dossier
verb: mutate
side_effects:
- network-upload: s3://<bucket>/<key>
idempotent: true
idempotency_key: key
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent archive-dossier --spec <path>
  python: hpc_agent.ops.archive_dossier.archive_dossier
---
# archive-dossier

Push an exported dossier archive to an **S3-compatible object store** (AWS S3,
Cloudflare R2, Backblaze B2, MinIO) with a SHA-256 integrity fingerprint and a
client-side immutability posture. The input is a local archive **path** —
typically the [`export-dossier`](export-dossier.md) output at
`<experiment>/_dossier/<run_id>.zip` — which the verb uploads verbatim as
**opaque bytes**. Core does only IDENTITY (hash the file) and COMPARISON (does
the stored object match what we sent); it never inspects what the archive
means (see `docs/internals/engineering-principles.md`, the four-question test).

Requires the **`[s3]` optional extra** (`pip install hpc-agent[s3]`, which
pulls in `boto3`). The import is lazy — a caller who never archives never pays
for it, and the core install stays free of the cloud SDK. When the extra is
absent the verb fails with `spec_invalid` naming the exact install command.

**Credentials come only from the standard AWS chain** (environment,
`~/.aws/config` + `credentials`, instance/role metadata). They are never spec
fields and are never stored or journaled. A scoped, write-only key is
recommended — see [`docs/internals/dossier-archival.md`](../internals/dossier-archival.md).

## Inputs

- `archive_path` (string, required) — local path to the archive to upload.
  Must exist and be a regular file.
- `bucket` (string, required) — destination bucket name.
- `key` (string, optional) — object key. Omit to derive
  `dossiers/<archive-filename>` (a derived default, not agent-authored); the
  resolved key is echoed back.
- `endpoint_url` (string, optional) — S3 API endpoint for an S3-**compatible**
  store (R2/B2/MinIO). Omit for AWS S3.
- `overwrite` (bool, default `false`) — when false, the verb HEADs the key and
  **refuses** to replace an existing object (reporting `already_exists`).

## Outputs

`data` is an `ArchiveDossierResult`:

```
{
  "bucket": "<bucket>",
  "key": "dossiers/<run_id>.zip",
  "etag": "<md5-for-simple-uploads>",
  "sha256": "<64-hex>",
  "size_bytes": <int>,
  "version_id": "<id or null>",
  "already_exists": false
}
```

On a successful upload the object carries the file `sha256` **and** (when the
zip's manifest.json is readable) its `bundle_sha256` as object metadata. The
verb then re-HEADs the object and verifies the stored size + round-tripped
hash (and, for a simple upload, that the ETag equals the body MD5) before
returning. `version_id` is non-null only when the bucket has versioning
enabled.

## Immutability posture

`overwrite=false` (the default) makes the verb **never silently replace** an
existing key: it reports `already_exists: true` — naming the existing object's
stored sha — and leaves the object untouched. This is the *client-side*
complement to **bucket-side** immutability (versioning + S3 Object Lock), which
is the real guarantee and a one-time human setup. See
[`docs/internals/dossier-archival.md`](../internals/dossier-archival.md) for
the exact `aws` CLI setup (and the R2/B2 equivalents).

## Errors

- `spec_invalid` — the archive path is missing / not a regular file; the
  `[s3]` extra (boto3) is not installed (remediation names
  `pip install hpc-agent[s3]`); or the post-upload integrity check fails (the
  stored object's size or hash disagrees with what was sent).

An already-present key under `overwrite=false` is **not** an error — it is a
finding, reported as `already_exists: true`, mirroring how an idempotent
replay reports "already done".

## Idempotency

**Idempotent, keyed on `key`.** A replay with `overwrite=false` finds the key
present and returns `already_exists: true` without touching the object, so
re-invoking is safe.
