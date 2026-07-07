# Dossier archival: object-store posture, integrity, and immutability

`archive-dossier` (`hpc_agent.ops.archive_dossier`) pushes an exported dossier
archive to an S3-compatible object store. This page records the posture the
verb implements client-side and — crucially — the parts of immutability that
the verb **cannot** provide and that a human must configure once on the bucket.

## Boundary posture

The archive is **opaque bytes**. Following the four-question test
(`engineering-principles.md`), the verb does only:

- **IDENTITY** — a SHA-256 over the file, stored as object metadata.
- **COMPARISON** — after upload, re-HEAD the object and confirm its size and
  round-tripped hash (and, for a simple upload, its ETag == body MD5) match
  what was sent.

It never inspects, parses, or names what the archive *means*. The one field it
reads out of the zip is `bundle_sha256` from the `manifest.json` that
`export-dossier` itself wrote — carried forward as metadata, i.e. core reading
its own record, never interpreting caller content. `boto3` is an **optional
extra** (Q4): the import is lazy, and core CI verifies the entire surface
against `moto`'s in-memory S3 — no real cloud, no network.

## Two layers of immutability

Immutability has a client-side complement and a bucket-side guarantee. Only the
second is real; the first is a courtesy that a determined caller (or a bug) can
bypass.

### 1. Client-side (what the verb does)

With `overwrite=false` (the default), the verb HEADs the key before writing and
**refuses to replace** an existing object — it returns `already_exists: true`,
names the existing object's stored sha, and leaves the object untouched. This
stops an accidental re-archive from clobbering a prior dossier. It is **not** a
security control: anyone with write credentials can pass `overwrite=true`, or
write the key with another tool.

### 2. Bucket-side (the real guarantee — human one-time setup)

Real immutability is **bucket versioning + S3 Object Lock** in compliance or
governance mode. Object Lock can only be enabled at bucket-creation time and
requires versioning. Once an object is written under a retention period, not
even the account root can delete or overwrite it until the period expires
(compliance mode) — the guarantee an archival/audit posture actually needs.

#### AWS S3 (exact one-time setup)

```sh
# Object Lock MUST be enabled at creation; it implies versioning.
aws s3api create-bucket \
  --bucket my-dossiers \
  --object-lock-enabled-for-bucket \
  --region us-east-1

# Versioning is required and is enabled implicitly, but set it explicitly
# so the intent is auditable.
aws s3api put-bucket-versioning \
  --bucket my-dossiers \
  --versioning-configuration Status=Enabled

# A default retention rule so every uploaded object is locked automatically.
aws s3api put-object-lock-configuration \
  --bucket my-dossiers \
  --object-lock-configuration '{
    "ObjectLockEnabled": "Enabled",
    "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 3650}}
  }'
```

`COMPLIANCE` mode cannot be shortened or removed by anyone; `GOVERNANCE` mode
allows a specifically-privileged principal (`s3:BypassGovernanceRetention`) to
override — pick per your audit requirements. After this, `archive-dossier`
uploads land as locked, versioned objects and the result's `version_id` is the
durable handle to the exact bytes archived.

#### S3-compatible equivalents

Pass the store's endpoint via the spec's `endpoint_url`.

- **Cloudflare R2** — versioning via `aws s3api put-bucket-versioning` against
  the R2 endpoint; R2 supports **Object Lock** (enable at bucket creation in
  the dashboard or via the S3 API `--object-lock-enabled-for-bucket`).
- **Backblaze B2** — set **Object Lock** at bucket creation
  (`b2 create-bucket --defaultRetentionMode compliance
  --defaultRetentionPeriod "3650 days"`), or via the S3-compatible API; B2's
  file-lock is the same WORM guarantee.
- **MinIO** — `mc mb --with-lock myminio/my-dossiers` then
  `mc retention set --default COMPLIANCE 3650d myminio/my-dossiers`.

## Credential model

Credentials come **only** from the standard AWS resolution chain: environment
(`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`),
`~/.aws/config` + `~/.aws/credentials`, or instance/role metadata. They are
**never** spec fields and are never stored, logged, or journaled — a secret on
the wire would end up in the decision journal and process logs.

Recommended: a **scoped, write-only** key for the archival bucket — the archival
principal needs `s3:PutObject` (and `s3:GetObject`/`s3:ListBucket` for the
pre-write HEAD and integrity re-HEAD), but not `s3:DeleteObject`. Combined with
Object Lock, a leaked archival key cannot destroy history.

## Integrity chain

1. **Local** — SHA-256 over the file, computed before upload.
2. **In-transit metadata** — that sha256 (and the manifest's `bundle_sha256`,
   when present) ride as object metadata on the PUT.
3. **Post-upload verification** — a re-HEAD confirms the store's own view: size
   matches the local file, the round-tripped `sha256` metadata matches, and for
   a simple (non-multipart) upload the ETag equals the body MD5 — a
   store-computed signal independent of the metadata we supplied. A mismatch is
   refused loudly (`spec_invalid`), never reported as success.
