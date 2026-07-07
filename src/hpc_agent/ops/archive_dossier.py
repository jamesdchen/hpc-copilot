"""``archive-dossier`` — push an exported dossier archive to object storage.

The agent-facing verb that takes an already-exported dossier archive (the
``.zip`` :mod:`hpc_agent.ops.export_dossier` writes) and stores it in an
S3-compatible bucket (AWS S3, Cloudflare R2, Backblaze B2, MinIO) with a
SHA-256 integrity fingerprint and a client-side immutability posture (never
silently overwrite).

Boundary posture (see ``docs/internals/engineering-principles.md``, the
four-question test): the archive is **opaque bytes**. This module does only
IDENTITY (hash the file) and COMPARISON (does the stored object's size / hash
match what we sent); it never inspects what the bytes mean. The single field
it reads out of the zip is the ``bundle_sha256`` from the manifest.json core
itself wrote — carried forward as object metadata, not interpreted.

Optional-dependency posture (Q4): ``boto3`` is an OPTIONAL extra
(``pip install hpc-agent[s3]``). The import is LAZY — a module-level
``import boto3`` is forbidden (it would tax control-plane startup for a verb
most invocations never touch, and the dependency isn't in the core install).
When boto3 is absent the verb raises :class:`errors.SpecInvalid` carrying the
one-line remediation. Core CI exercises the whole surface against ``moto``'s
in-memory S3 — no real cloud, no network.

Credential posture: credentials come ONLY from the standard AWS resolution
chain (environment / ``~/.aws`` / instance-role). They are never spec fields
and never stored — see :mod:`hpc_agent._wire.actions.archive_dossier`.

Real immutability is BUCKET-side (versioning + S3 Object Lock), a one-time
human setup documented in ``docs/internals/dossier-archival.md``. This verb's
overwrite-refusal is the client-side complement, not a substitute.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.archive_dossier import ArchiveDossierResult, ArchiveDossierSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "archive_dossier",
    "compute_file_sha256",
    "read_bundle_sha256",
]

# The remediation shown when boto3 is not installed. Named so the test that
# asserts the missing-dependency path can pin the exact extras string a caller
# needs, rather than a moving message.
_BOTO3_REMEDIATION = (
    "archive-dossier needs the optional S3 client. Install it with "
    "`pip install hpc-agent[s3]` (boto3), then retry. Credentials resolve "
    "from the standard AWS chain (env / ~/.aws / instance role); none are "
    "passed to hpc-agent."
)

# Read/hash the local file in bounded chunks so a large archive never has to
# be resident in memory at once.
_CHUNK = 1024 * 1024


def _import_boto3() -> Any:
    """Return the ``boto3`` module, or raise :class:`errors.SpecInvalid`.

    The single lazy-import seam — module-level ``import boto3`` is forbidden
    (control-plane startup budget + the optional extra), and tests
    monkeypatch THIS function to simulate the dependency being absent. A
    missing boto3 is a user-fixable condition (install the extra), so it maps
    to ``spec_invalid`` carrying :data:`_BOTO3_REMEDIATION`.
    """
    try:
        import boto3
    except ImportError as exc:
        raise errors.SpecInvalid("boto3 is not installed", remediation=_BOTO3_REMEDIATION) from exc
    return boto3


def compute_file_sha256(path: Path) -> str:
    """Return the SHA-256 hex digest over *path*'s bytes (streamed in chunks)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def read_bundle_sha256(path: Path) -> str | None:
    """Best-effort read of ``bundle_sha256`` from the archive's manifest.json.

    ``export-dossier`` writes a ``manifest.json`` inside the bundle carrying
    ``bundle_sha256`` (the fingerprint over the archived stores). This reads
    it via stdlib :mod:`zipfile` so the value can ride along as object
    metadata — reading core's OWN record, never interpreting caller content.

    Returns ``None`` for anything that isn't a readable zip with a
    manifest.json naming a string ``bundle_sha256`` (a non-zip archive, an
    export without a manifest, a manifest without the key). The absence is a
    non-event — it never blocks the upload.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            member = next(
                (n for n in zf.namelist() if n.rsplit("/", 1)[-1] == "manifest.json"),
                None,
            )
            if member is None:
                return None
            manifest = json.loads(zf.read(member))
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    value = manifest.get("bundle_sha256") if isinstance(manifest, dict) else None
    return value if isinstance(value, str) and value else None


def _head_object(client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    """HEAD *key*; return the response dict, or ``None`` when it does not exist.

    A 404/NoSuchKey is the "absent" answer (returns ``None``); every other
    ``ClientError`` (403, a bucket that doesn't exist, a transport failure)
    propagates — those are real conditions the caller must see, not an
    "absent" that would let the verb barrel on.
    """
    from botocore.exceptions import ClientError

    try:
        response = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return None
        raise
    return dict(response)


def _strip_etag(raw: str | None) -> str:
    """Return an ETag with its surrounding quotes stripped (S3 quotes ETags)."""
    return (raw or "").strip('"')


def _default_key(archive_path: Path) -> str:
    """Derive the conventional ``dossiers/<filename>`` key from the archive path."""
    return f"dossiers/{archive_path.name}"


@primitive(
    name="archive-dossier",
    verb="mutate",
    side_effects=[SideEffect("network-upload", "s3://<bucket>/<key>")],
    error_codes=[errors.SpecInvalid],
    # Idempotent by the immutability posture: keyed on the object key. A replay
    # with overwrite=False finds the key present and reports already_exists
    # (the object is untouched), so re-invoking is safe.
    idempotent=True,
    idempotency_key="key",
    cli=CliShape(
        help=(
            "Upload an exported dossier archive to an S3-compatible bucket "
            "(AWS S3 / R2 / B2 / MinIO) with a SHA-256 integrity fingerprint. "
            "Refuses to overwrite an existing key unless overwrite=true "
            "(client-side immutability posture; real immutability is bucket "
            "versioning + Object Lock — see docs/internals/dossier-archival.md). "
            "Needs the [s3] extra (boto3); credentials come from the standard "
            "AWS chain, never the spec."
        ),
        spec_arg=True,
        spec_model=ArchiveDossierSpec,
        schema_ref=SchemaRef(input="archive_dossier"),
    ),
    agent_facing=True,
)
def archive_dossier(*, spec: ArchiveDossierSpec) -> ArchiveDossierResult:
    """Push *spec.archive_path* to ``s3://<bucket>/<key>`` and verify integrity.

    Steps: validate the archive exists; SHA-256 it (and read the manifest's
    ``bundle_sha256`` when present); resolve the key (``dossiers/<filename>``
    by default); then — unless ``overwrite`` — HEAD the key and REFUSE a
    replace (reporting ``already_exists`` with the existing object's stored
    hash). On upload, both hashes ride as object metadata; the verb then
    re-HEADs to verify the stored size matches the local file (and, for a
    simple upload, that the ETag equals the body MD5) and surfaces the
    ``version_id`` when the bucket has versioning.

    Returns an :class:`ArchiveDossierResult`. The already-exists refusal is
    NOT an error — it is a finding (mirroring an idempotent replay), so the
    result carries ``already_exists=True`` rather than raising.

    Raises
    ------
    :class:`errors.SpecInvalid`
        The archive path is missing / not a regular file; boto3 (the ``[s3]``
        extra) is not installed; or the post-upload integrity check fails
        (the stored object's size or hash disagrees with what was sent).
    """
    archive_path = Path(spec.archive_path)
    if not archive_path.is_file():
        raise errors.SpecInvalid(
            f"archive_path {spec.archive_path!r} is not a regular file — "
            "export the dossier first (export-dossier writes "
            "<experiment>/_dossier/<run_id>.zip)."
        )

    sha256 = compute_file_sha256(archive_path)
    size_bytes = archive_path.stat().st_size
    bundle_sha256 = read_bundle_sha256(archive_path)
    key = spec.key or _default_key(archive_path)

    boto3 = _import_boto3()
    client = boto3.client("s3", endpoint_url=spec.endpoint_url)

    # Immutability posture: never silently replace. HEAD first unless the
    # caller explicitly opted into overwrite.
    if not spec.overwrite:
        existing = _head_object(client, spec.bucket, key)
        if existing is not None:
            existing_meta = existing.get("Metadata", {}) or {}
            return ArchiveDossierResult(
                bucket=spec.bucket,
                key=key,
                etag=_strip_etag(existing.get("ETag")),
                sha256=str(existing_meta.get("sha256", "")),
                size_bytes=int(existing.get("ContentLength", 0)),
                version_id=existing.get("VersionId"),
                already_exists=True,
            )

    metadata: dict[str, str] = {"sha256": sha256}
    if bundle_sha256 is not None:
        metadata["bundle_sha256"] = bundle_sha256

    with archive_path.open("rb") as body:
        put = client.put_object(Bucket=spec.bucket, Key=key, Body=body, Metadata=metadata)

    # Verify integrity from the store's own view of the object, not the PUT
    # echo: re-HEAD and compare size + the round-tripped sha; for a simple
    # (non-multipart) upload the ETag is the body MD5, so cross-check it too.
    head = _head_object(client, spec.bucket, key)
    if head is None:
        raise errors.SpecInvalid(
            f"post-upload verification failed: s3://{spec.bucket}/{key} "
            "was not found on re-HEAD immediately after PUT."
        )
    _verify_integrity(archive_path, size_bytes, sha256, head)

    return ArchiveDossierResult(
        bucket=spec.bucket,
        key=key,
        etag=_strip_etag(head.get("ETag") or put.get("ETag")),
        sha256=sha256,
        size_bytes=size_bytes,
        version_id=head.get("VersionId") or put.get("VersionId"),
        already_exists=False,
    )


def _verify_integrity(
    archive_path: Path,
    local_size: int,
    local_sha256: str,
    head: Mapping[str, Any],
) -> None:
    """Refuse loudly when the stored object disagrees with what we sent.

    Compares the stored ``ContentLength`` against the local size and the
    round-tripped ``Metadata.sha256`` against the local hash. For a simple
    upload (an ETag with no ``-<part-count>`` suffix) the ETag is the body's
    MD5, so it is cross-checked against a fresh MD5 of the file — a second,
    store-computed integrity signal independent of the metadata we supplied.
    """
    stored_size = int(head.get("ContentLength", -1))
    if stored_size != local_size:
        raise errors.SpecInvalid(
            f"post-upload integrity check failed: stored size {stored_size} "
            f"!= local size {local_size}."
        )
    stored_meta = head.get("Metadata", {}) or {}
    stored_sha = str(stored_meta.get("sha256", ""))
    if stored_sha and stored_sha != local_sha256:
        raise errors.SpecInvalid(
            "post-upload integrity check failed: stored metadata sha256 "
            f"{stored_sha!r} != local sha256 {local_sha256!r}."
        )
    etag = _strip_etag(head.get("ETag"))
    if etag and "-" not in etag:
        md5 = hashlib.md5()  # noqa: S324 — S3 ETag is MD5; matching it, not a security hash
        with archive_path.open("rb") as fh:
            for block in iter(lambda: fh.read(_CHUNK), b""):
                md5.update(block)
        if etag != md5.hexdigest():
            raise errors.SpecInvalid(
                f"post-upload integrity check failed: simple-upload ETag {etag!r} "
                f"!= local body MD5 {md5.hexdigest()!r}."
            )
