"""Tests for the content manifest + verify-against-manifest (#232).

Pins the irreducible property: verification catches a truncated/corrupt
"completed" transfer that an exit-code (or size-or-existence) check would
miss, and a manifest is a stable content identity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.ops.transfer.manifest import (
    Manifest,
    build_manifest,
    verify_manifest,
)


def _tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


# ── manifest identity ───────────────────────────────────────────────────────


def test_digest_is_content_identity(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _tree(a, {"x.txt": b"hello", "sub/y.bin": b"\x00\x01"})
    _tree(b, {"x.txt": b"hello", "sub/y.bin": b"\x00\x01"})
    # identical content (even built in different dir-walk order) → same digest
    assert build_manifest(a).digest == build_manifest(b).digest


def test_digest_changes_on_content_change(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _tree(a, {"x.txt": b"hello"})
    _tree(b, {"x.txt": b"hellp"})  # one byte differs
    assert build_manifest(a).digest != build_manifest(b).digest


def test_manifest_round_trips_through_dict(tmp_path: Path) -> None:
    _tree(tmp_path, {"x.txt": b"hello", "y.txt": b"world"})
    m = build_manifest(tmp_path)
    assert Manifest.from_dict(m.to_dict()) == m


def test_build_with_declared_paths_only(tmp_path: Path) -> None:
    _tree(tmp_path, {"keep.txt": b"a", "ignore.txt": b"b"})
    m = build_manifest(tmp_path, paths=["keep.txt"])
    assert m.paths == ("keep.txt",)


def test_build_missing_declared_path_is_hard_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_manifest(tmp_path, paths=["nope.txt"])


# ── verification catches what exit-code / size checks miss ──────────────────


def test_verify_clean_transfer_ok(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _tree(src, {"x.txt": b"hello", "sub/y.bin": b"\x00\x01\x02"})
    m = build_manifest(src)
    dst = tmp_path / "dst"
    _tree(dst, {"x.txt": b"hello", "sub/y.bin": b"\x00\x01\x02"})
    assert verify_manifest(dst, m).ok is True


def test_verify_detects_corruption_same_size(tmp_path: Path) -> None:
    """The case exit-code AND size checks both miss: a same-length corruption."""
    src = tmp_path / "src"
    _tree(src, {"x.txt": b"hello"})
    m = build_manifest(src)
    dst = tmp_path / "dst"
    _tree(dst, {"x.txt": b"hellp"})  # same size, different content
    report = verify_manifest(dst, m)
    assert report.ok is False
    assert report.hash_mismatch == ("x.txt",)


def test_verify_detects_truncation(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _tree(src, {"x.txt": b"hello world"})
    m = build_manifest(src)
    dst = tmp_path / "dst"
    _tree(dst, {"x.txt": b"hello"})  # truncated
    report = verify_manifest(dst, m)
    assert report.ok is False
    assert report.size_mismatch == ("x.txt",)


def test_verify_detects_missing(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _tree(src, {"x.txt": b"a", "y.txt": b"b"})
    m = build_manifest(src)
    dst = tmp_path / "dst"
    _tree(dst, {"x.txt": b"a"})  # y.txt never landed
    report = verify_manifest(dst, m)
    assert report.ok is False
    assert report.missing == ("y.txt",)


def test_check_hash_false_is_size_only(tmp_path: Path) -> None:
    """The stage-out-heavy escape hatch: skip hashing, same-size corruption
    passes (weaker, cheap) — but truncation is still caught."""
    src = tmp_path / "src"
    _tree(src, {"x.txt": b"hello"})
    m = build_manifest(src)
    dst = tmp_path / "dst"
    _tree(dst, {"x.txt": b"hellp"})  # same-size corruption
    assert verify_manifest(dst, m, check_hash=False).ok is True
    assert verify_manifest(dst, m, check_hash=True).ok is False


# ── failure routes into the #230/#231 escalation path ───────────────────────


def test_missing_projects_to_structural_failure_features(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _tree(src, {"x.txt": b"a", "y.txt": b"b"})
    m = build_manifest(src)
    _tree(tmp_path / "dst", {"x.txt": b"a"})
    report = verify_manifest(tmp_path / "dst", m)
    features = report.failure_features()
    assert features.error_class_raw == "outputs_missing"
    assert features.resource_spec["missing"] == 1


def test_corruption_projects_to_corrupt_transfer(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _tree(src, {"x.txt": b"hello"})
    m = build_manifest(src)
    _tree(tmp_path / "dst", {"x.txt": b"hellp"})
    features = verify_manifest(tmp_path / "dst", m).failure_features()
    assert features.error_class_raw == "corrupt_transfer"
