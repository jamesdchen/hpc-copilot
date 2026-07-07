"""Pydantic models for the ``archive-dossier`` action.

Wire surface over the object-store archival verb — the step that pushes an
already-exported dossier archive (the ``.zip`` ``export-dossier`` writes under
``<experiment>/_dossier/<run_id>.zip``) to an S3-compatible bucket with
integrity verification and an immutability posture.

Boundary posture (see ``docs/internals/engineering-principles.md``): the
archive is **opaque bytes** to this verb. Core does IDENTITY (a SHA-256 over
the file) and COMPARISON (does the uploaded object's size / stored hash match
what we sent); it never inspects, parses, or names what the bytes *mean* —
that stays caller-owned (``export-dossier`` typed each entry by its source
store, never by a role). The verb reads ONE thing out of the archive — the
``bundle_sha256`` from the manifest.json core itself wrote — and only to carry
it forward as object metadata; that is core reading its own record, not
interpreting caller content.

Credential posture: credentials are NEVER carried on these models and never
stored. They come only from the standard AWS resolution chain (environment,
``~/.aws/config``/``credentials``, instance/role metadata) that boto3 already
implements. A key or secret on the wire would be journaled and logged; the
chain keeps them off every surface the framework touches.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ArchiveDossierSpec(BaseModel):
    """Inputs to ``archive-dossier``.

    ``archive_path`` is the local file to push (the ``export-dossier`` output,
    or any archive); ``bucket`` names the destination S3 (or S3-compatible)
    bucket. ``endpoint_url`` targets a non-AWS S3 API (Cloudflare R2,
    Backblaze B2, MinIO); omit it for AWS S3 proper. Credentials are NOT
    fields here — they resolve only from the standard AWS chain (see the
    module docstring).
    """

    model_config = ConfigDict(extra="forbid", title="archive-dossier input spec")

    archive_path: str = Field(
        min_length=1,
        description=(
            "Local path to the archive to upload — typically the "
            "export-dossier output at <experiment>/_dossier/<run_id>.zip. "
            "Uploaded verbatim as opaque bytes; a SHA-256 over the file is "
            "the integrity fingerprint. Must exist and be a regular file."
        ),
    )
    bucket: str = Field(
        min_length=1,
        description=(
            "Destination bucket name. Real immutability (versioning + S3 "
            "Object Lock) is a one-time bucket-side setup the human performs; "
            "see docs/internals/dossier-archival.md."
        ),
    )
    key: str | None = Field(
        default=None,
        description=(
            "Object key to write. Omit to derive the conventional "
            "dossiers/<archive-filename> — a derived default, not an "
            "agent-authored path; the resolved key is echoed back."
        ),
    )
    endpoint_url: str | None = Field(
        default=None,
        description=(
            "S3 API endpoint for an S3-COMPATIBLE store (Cloudflare R2, "
            "Backblaze B2, MinIO). Omit for AWS S3. Credentials still come "
            "only from the standard AWS chain, never from this spec."
        ),
    )
    overwrite: bool = Field(
        default=False,
        description=(
            "When false (default), the verb HEADs the key first and REFUSES "
            "to replace an existing object (reporting already_exists=true, "
            "naming the existing object's stored sha) — the client-side "
            "immutability posture: an archive is never silently overwritten. "
            "Set true to deliberately replace."
        ),
    )


class ArchiveDossierResult(BaseModel):
    """The upload receipt — provenance and integrity of the stored object.

    Every field describes the object by IDENTITY (which bucket/key, what hash,
    what size, which version) — never by the archive's meaning.
    """

    model_config = ConfigDict(extra="forbid", title="archive-dossier output data")

    bucket: str
    key: str = Field(description="The resolved object key the archive was stored under.")
    # The object's ETag as returned by the store (surrounding quotes stripped).
    # For a simple (non-multipart) PUT this is the MD5 of the body; the verb
    # cross-checks it against the local file for such uploads.
    etag: str = Field(
        description=(
            "The stored object's ETag (quotes stripped). For a simple upload "
            "this equals the body MD5, which the verb verifies against the "
            "local file."
        ),
    )
    # SHA-256 the object carries as metadata: the freshly-computed local hash on
    # an upload, or the EXISTING object's stored hash on an already_exists
    # refusal (so the caller can see WHICH bytes are already parked there).
    sha256: str = Field(
        description=(
            "The archive's SHA-256 stored as object metadata. On an upload "
            "this is the freshly-computed local hash; on an already_exists "
            "refusal it is the existing object's stored hash (empty if that "
            "object carries none)."
        ),
    )
    size_bytes: int = Field(
        ge=0,
        description="The stored object's size in bytes (verified against the local file on upload).",
    )
    # Present only when the bucket has versioning enabled; None otherwise.
    version_id: str | None = Field(
        default=None,
        description=(
            "The object version id when the bucket has versioning enabled "
            "(the durable handle for this exact upload); null on an "
            "unversioned bucket."
        ),
    )
    # True when overwrite=False found the key already present: the upload was
    # REFUSED, not performed. Not an error — a finding, mirroring how an
    # idempotent replay reports "already done" rather than raising.
    already_exists: bool = Field(
        default=False,
        description=(
            "True when overwrite=False and the key already existed: the "
            "upload was refused (immutability posture), the object is "
            "untouched, and the fields describe the EXISTING object. Not an "
            "error — a finding."
        ),
    )
