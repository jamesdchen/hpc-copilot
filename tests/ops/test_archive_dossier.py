"""Tests for ``archive-dossier`` — dossier archival to an S3-compatible store.

Everything runs against moto's in-memory S3 (``@mock_aws``) — NO network, no
real cloud, per the four-question test's Q4 (core CI verifies the surface
without a real dependency). Properties pinned:

* Happy path: the archive uploads; the result's etag/sha/size match the file;
  the stored object carries BOTH the file sha256 and the manifest's
  bundle_sha256 as metadata.
* Immutability posture: overwrite=False refuses an existing key and REPORTS
  already_exists (naming the existing object's stored sha) rather than raising;
  overwrite=True replaces.
* A versioned bucket surfaces the version_id.
* Missing boto3 (the [s3] extra) → spec_invalid carrying the pip-install
  remediation, via the monkeypatched import seam.
* A missing / non-file archive_path → spec_invalid.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.archive_dossier import ArchiveDossierSpec
from hpc_agent.ops import archive_dossier as mod
from hpc_agent.ops.archive_dossier import archive_dossier

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402 — after importorskip guards the dependency

_REGION = "us-east-1"


def _make_archive(tmp_path: Path, *, bundle_sha256: str | None = "b" * 64) -> Path:
    """Write a minimal dossier .zip; embed a manifest.json when a sha is given."""
    path = tmp_path / "_dossier" / "20260101-000001-aaaaaaa.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("journal/records.jsonl", '{"a": 1}\n')
        if bundle_sha256 is not None:
            zf.writestr("manifest.json", f'{{"bundle_sha256": "{bundle_sha256}"}}')
    return path


def _client():
    return boto3.client("s3", region_name=_REGION)


@mock_aws
def test_upload_happy_path_verifies_etag_sha_size(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path)
    client = _client()
    client.create_bucket(Bucket="dossiers")

    result = archive_dossier(spec=ArchiveDossierSpec(archive_path=str(archive), bucket="dossiers"))

    assert result.already_exists is False
    assert result.bucket == "dossiers"
    assert result.key == "dossiers/20260101-000001-aaaaaaa.zip"  # derived default
    assert result.size_bytes == archive.stat().st_size
    assert result.sha256 == mod.compute_file_sha256(archive)
    # Simple upload → ETag is the body MD5 (quotes stripped, no multipart dash).
    assert result.etag and "-" not in result.etag

    # The stored object carries BOTH hashes as metadata.
    head = client.head_object(Bucket="dossiers", Key=result.key)
    assert head["Metadata"]["sha256"] == result.sha256
    assert head["Metadata"]["bundle_sha256"] == "b" * 64


@mock_aws
def test_explicit_key_and_manifestless_archive(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, bundle_sha256=None)
    client = _client()
    client.create_bucket(Bucket="dossiers")

    result = archive_dossier(
        spec=ArchiveDossierSpec(archive_path=str(archive), bucket="dossiers", key="custom/path.zip")
    )
    assert result.key == "custom/path.zip"
    head = client.head_object(Bucket="dossiers", Key="custom/path.zip")
    # No manifest → no bundle_sha256 metadata, but the file sha is still stored.
    assert head["Metadata"]["sha256"] == result.sha256
    assert "bundle_sha256" not in head["Metadata"]


@mock_aws
def test_overwrite_false_refuses_existing_and_reports_already_exists(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path)
    client = _client()
    client.create_bucket(Bucket="dossiers")

    first = archive_dossier(spec=ArchiveDossierSpec(archive_path=str(archive), bucket="dossiers"))
    assert first.already_exists is False

    # Second call, overwrite=False: refuses, reports already_exists — no raise.
    second = archive_dossier(spec=ArchiveDossierSpec(archive_path=str(archive), bucket="dossiers"))
    assert second.already_exists is True
    assert second.key == first.key
    # The refusal names the EXISTING object's stored sha.
    assert second.sha256 == first.sha256


@mock_aws
def test_overwrite_true_replaces(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path)
    client = _client()
    client.create_bucket(Bucket="dossiers")

    archive_dossier(spec=ArchiveDossierSpec(archive_path=str(archive), bucket="dossiers"))
    result = archive_dossier(
        spec=ArchiveDossierSpec(archive_path=str(archive), bucket="dossiers", overwrite=True)
    )
    assert result.already_exists is False
    assert result.sha256 == mod.compute_file_sha256(archive)


@mock_aws
def test_versioned_bucket_returns_version_id(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path)
    client = _client()
    client.create_bucket(Bucket="dossiers")
    client.put_bucket_versioning(Bucket="dossiers", VersioningConfiguration={"Status": "Enabled"})

    result = archive_dossier(spec=ArchiveDossierSpec(archive_path=str(archive), bucket="dossiers"))
    assert result.version_id is not None
    assert result.version_id != ""


def test_missing_boto3_raises_spec_invalid_with_extras_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _make_archive(tmp_path)

    def _no_boto3() -> object:
        raise errors.SpecInvalid("boto3 is not installed", remediation=mod._BOTO3_REMEDIATION)

    monkeypatch.setattr(mod, "_import_boto3", _no_boto3)

    with pytest.raises(errors.SpecInvalid) as exc:
        archive_dossier(spec=ArchiveDossierSpec(archive_path=str(archive), bucket="dossiers"))
    assert "hpc-agent[s3]" in (exc.value.remediation or "")


def test_missing_boto3_seam_maps_importerror_to_spec_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Directly exercise the import seam: an ImportError becomes spec_invalid
    # with the extras remediation (the real absent-dependency path).
    import builtins

    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(errors.SpecInvalid) as exc:
        mod._import_boto3()
    assert "pip install hpc-agent[s3]" in (exc.value.remediation or "")


def test_absent_archive_path_raises_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid) as exc:
        archive_dossier(
            spec=ArchiveDossierSpec(archive_path=str(tmp_path / "nope.zip"), bucket="dossiers")
        )
    assert "regular file" in str(exc.value)


def test_directory_archive_path_raises_spec_invalid(tmp_path: Path) -> None:
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(errors.SpecInvalid):
        archive_dossier(spec=ArchiveDossierSpec(archive_path=str(d), bucket="dossiers"))


def test_read_bundle_sha256_from_archive(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, bundle_sha256="c" * 64)
    assert mod.read_bundle_sha256(archive) == "c" * 64
    # A non-zip file reads as None (best-effort, never fatal).
    plain = tmp_path / "plain.txt"
    plain.write_text("not a zip", encoding="utf-8")
    assert mod.read_bundle_sha256(plain) is None


def test_primitive_is_registered_and_agent_facing() -> None:
    from hpc_agent._kernel.registry.primitive import get_meta, register_primitives

    register_primitives()
    meta = get_meta("archive-dossier")
    assert meta.verb == "mutate"
    assert meta.agent_facing is True
    assert meta.idempotent is True
    assert meta.idempotency_key == "key"
    assert any(se.kind == "network-upload" for se in meta.side_effects)
