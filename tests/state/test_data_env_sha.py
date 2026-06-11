"""Tests for the #222 provenance hashes: ``compute_data_sha`` /
``compute_env_hash``.

These extend run identity past parameter (``cmd_sha``) and code
(``tasks_py_sha``) to the DATA and ENVIRONMENT a result was produced under.
Properties pinned:

* Output shape — 64-char lowercase hex.
* Determinism — same input → same output.
* Sensitivity — a changed input byte / module / runtime changes the hash.
* DVC-pointer precedence — a sibling ``<file>.dvc`` is hashed via its
  recorded md5, NOT by re-hashing the working-tree file (which may be a
  stale placeholder or absent entirely).
* Order semantics — declaration order of data paths is irrelevant (sorted),
  but ``modules`` / ``conda_envs`` order IS significant.
"""

from __future__ import annotations

import re
from pathlib import Path

from hpc_agent.state.run_sha import compute_data_sha, compute_env_hash

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# compute_data_sha
# ---------------------------------------------------------------------------


def test_data_sha_shape_and_determinism(tmp_path: Path) -> None:
    f = tmp_path / "train.parquet"
    f.write_bytes(b"some-bytes")
    a = compute_data_sha([f])
    b = compute_data_sha([f])
    assert _HEX64.fullmatch(a)
    assert a == b


def test_data_sha_changes_with_content(tmp_path: Path) -> None:
    f = tmp_path / "train.csv"
    f.write_bytes(b"v1")
    first = compute_data_sha([f])
    f.write_bytes(b"v2")
    assert compute_data_sha([f]) != first


def test_data_sha_empty_is_empty_string_sha(tmp_path: Path) -> None:
    # No declared data is a well-defined identity (SHA-256 of "").
    import hashlib

    assert compute_data_sha([]) == hashlib.sha256(b"").hexdigest()


def test_data_sha_missing_path_is_absent_not_error(tmp_path: Path) -> None:
    # A declared-but-missing input contributes the ``absent`` sentinel and
    # does not raise — a manifest can still be emitted.
    missing = tmp_path / "nope.parquet"
    digest = compute_data_sha([missing])
    assert _HEX64.fullmatch(digest)
    # Distinct from the empty-data identity.
    assert digest != compute_data_sha([])


def test_data_sha_order_independent(tmp_path: Path) -> None:
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    assert compute_data_sha([a, b]) == compute_data_sha([b, a])


def test_data_sha_distinguishes_swapped_contents(tmp_path: Path) -> None:
    # {a: H, b: K} must differ from {a: K, b: H} — the relpath is part of
    # each per-path line, so swapping which file holds which bytes changes
    # the digest.
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    first = compute_data_sha([a, b], base_dir=tmp_path)
    a.write_bytes(b"bbb")
    b.write_bytes(b"aaa")
    assert compute_data_sha([a, b], base_dir=tmp_path) != first


def test_data_sha_relative_base_dir_stable_across_mount(tmp_path: Path) -> None:
    # The digest keys on the path RELATIVE to base_dir, so the same data at
    # two different absolute mount roots hashes identically.
    root1 = tmp_path / "mount1"
    root2 = tmp_path / "mount2"
    for root in (root1, root2):
        (root / "data").mkdir(parents=True)
        (root / "data" / "x.csv").write_bytes(b"payload")
    h1 = compute_data_sha([Path("data/x.csv")], base_dir=root1)
    h2 = compute_data_sha([Path("data/x.csv")], base_dir=root2)
    assert h1 == h2


def test_data_sha_uses_dvc_pointer_md5(tmp_path: Path) -> None:
    # A DVC-tracked input: the real bytes may be a placeholder / absent; the
    # ``.dvc`` pointer's recorded md5 is the data identity.
    data = tmp_path / "big.parquet"
    data.write_bytes(b"placeholder-working-tree-bytes")
    pointer = tmp_path / "big.parquet.dvc"
    pointer.write_text("outs:\n- md5: abc123def456\n  path: big.parquet\n", encoding="utf-8")
    via_dvc = compute_data_sha([data])

    # Same DVC md5 but different working-tree bytes → SAME data_sha (DVC wins).
    data.write_bytes(b"completely-different-working-tree-bytes")
    assert compute_data_sha([data]) == via_dvc

    # Change the recorded md5 → data_sha changes.
    pointer.write_text("outs:\n- md5: deadbeef0000\n  path: big.parquet\n", encoding="utf-8")
    assert compute_data_sha([data]) != via_dvc


def test_data_sha_dvc_differs_from_content_hash(tmp_path: Path) -> None:
    # The DVC path and the raw-content path are deliberately distinct
    # per-path encodings (``dvc:`` vs ``sha256:``) so they never collide.
    data = tmp_path / "f.csv"
    data.write_bytes(b"x")
    content_only = compute_data_sha([data])
    pointer = tmp_path / "f.csv.dvc"
    pointer.write_text("outs:\n- md5: 0011\n", encoding="utf-8")
    assert compute_data_sha([data]) != content_only


def test_data_sha_malformed_dvc_falls_back_to_content(tmp_path: Path) -> None:
    data = tmp_path / "f.csv"
    data.write_bytes(b"x")
    content_only = compute_data_sha([data])
    pointer = tmp_path / "f.csv.dvc"
    pointer.write_text("this: is not: valid: dvc", encoding="utf-8")
    # Malformed pointer → fall back to content hash (unchanged from no-pointer).
    assert compute_data_sha([data]) == content_only


# ---------------------------------------------------------------------------
# compute_env_hash
# ---------------------------------------------------------------------------


def test_env_hash_shape_and_determinism() -> None:
    a = compute_env_hash(modules=["python/3.11"], conda_source="/c.sh", conda_envs=["ml"])
    b = compute_env_hash(modules=["python/3.11"], conda_source="/c.sh", conda_envs=["ml"])
    assert _HEX64.fullmatch(a)
    assert a == b


def test_env_hash_all_unset_is_stable() -> None:
    assert _HEX64.fullmatch(compute_env_hash())
    assert compute_env_hash() == compute_env_hash(modules=[], conda_envs=[])


def test_env_hash_runtime_changes_hash() -> None:
    base = compute_env_hash(modules=["m"])
    assert compute_env_hash(modules=["m"], runtime="uv") != base


def test_env_hash_module_order_significant() -> None:
    # ``module load`` order changes the resolved env, so it is NOT sorted.
    assert compute_env_hash(modules=["a", "b"]) != compute_env_hash(modules=["b", "a"])


def test_env_hash_conda_env_order_significant() -> None:
    assert compute_env_hash(conda_envs=["a", "b"], conda_source="/c.sh") != compute_env_hash(
        conda_envs=["b", "a"], conda_source="/c.sh"
    )


def test_env_hash_conda_source_changes_hash() -> None:
    a = compute_env_hash(conda_source="/a/conda.sh", conda_envs=["e"])
    b = compute_env_hash(conda_source="/b/conda.sh", conda_envs=["e"])
    assert a != b


def test_env_hash_extra_folds_in_and_is_key_order_invariant() -> None:
    base = compute_env_hash(modules=["m"])
    with_extra = compute_env_hash(modules=["m"], extra={"cuda": "12.1", "python": "3.11"})
    assert with_extra != base
    # Key order within ``extra`` is irrelevant (keys are sorted).
    reordered = compute_env_hash(modules=["m"], extra={"python": "3.11", "cuda": "12.1"})
    assert with_extra == reordered
