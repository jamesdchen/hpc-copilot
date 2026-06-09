"""Tests for the format-aware checkpoint seam (checkpoint_formats)."""

from __future__ import annotations

import struct
import time
from pathlib import Path

import pytest

from hpc_agent.experiment_kit import checkpoint as ck
from hpc_agent.experiment_kit.checkpoint_formats import (
    checkpoint_formats,
    describe_latest_checkpoint,
)
from hpc_agent.experiment_kit.solver_adapters import petsc

_VEC_CLASSID = 1211214


def _vec_block(n: int = 3, scalar_size: int = 8) -> bytes:
    return struct.pack(">ii", _VEC_CLASSID, n) + b"\x01" * (n * scalar_size)


def test_missing_when_no_artifacts(tmp_path: Path) -> None:
    assert describe_latest_checkpoint(tmp_path) == {"status": "missing"}


def test_pickle_ok_preserves_loadable_semantics(tmp_path: Path) -> None:
    ck.write_checkpoint({"w": 1}, iteration=4, result_dir=tmp_path)
    out = describe_latest_checkpoint(tmp_path)
    assert out["status"] == "ok"
    assert out["format"] == "pickle"
    assert out["level"] == "loadable"
    assert out["next_iteration"] == 5
    assert out["path"].endswith("checkpoint-4.pkl")


def test_pickle_corrupt_newest_still_ok_via_older(tmp_path: Path) -> None:
    """Mirrors read_latest_checkpoint: one corrupt newest file does not fail
    the verdict when an older checkpoint loads — the historical probe
    semantics, preserved by the seam."""
    ck.write_checkpoint({"w": 1}, iteration=1, result_dir=tmp_path)
    (ck.checkpoint_dir(tmp_path) / "checkpoint-9.pkl").write_bytes(b"garbage")
    out = describe_latest_checkpoint(tmp_path)
    assert out["status"] == "ok" and out["next_iteration"] == 2
    # The newest file is what gets reported, exactly like the old snippet.
    assert out["path"].endswith("checkpoint-9.pkl")


def test_pickle_unloadable_when_nothing_deserializes(tmp_path: Path) -> None:
    d = ck.checkpoint_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "checkpoint-0.pkl").write_bytes(b"not a pickle")
    out = describe_latest_checkpoint(tmp_path)
    assert out["status"] == "unloadable"
    assert out["format"] == "pickle" and out["level"] == "loadable"


def test_petsc_monitor_dump_verifies_structurally(tmp_path: Path) -> None:
    d = ck.checkpoint_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "checkpoint-7.petscbin").write_bytes(_vec_block())
    out = describe_latest_checkpoint(tmp_path)
    assert out["status"] == "ok"
    assert out["format"] == "petsc_binary"
    assert out["level"] == "structural"
    assert out["path"].endswith("checkpoint-7.petscbin")
    # No reload happened — no next_iteration claim is made.
    assert "next_iteration" not in out


def test_petsc_wrapper_solution_discovered(tmp_path: Path) -> None:
    """The wrapper path's single appended solution file is also a checkpoint
    artifact the canary verdict must see."""
    d = ck.checkpoint_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "petsc-solution.bin").write_bytes(_vec_block() + _vec_block())
    out = describe_latest_checkpoint(tmp_path)
    assert out["status"] == "ok" and out["format"] == "petsc_binary"
    assert out["path"].endswith("petsc-solution.bin")


def test_petsc_garbage_is_unloadable(tmp_path: Path) -> None:
    d = ck.checkpoint_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "checkpoint-0.petscbin").write_bytes(b"\x00" * 64)
    out = describe_latest_checkpoint(tmp_path)
    assert out["status"] == "unloadable"
    assert out["format"] == "petsc_binary" and out["level"] == "structural"


def test_newest_format_wins_by_mtime(tmp_path: Path) -> None:
    """Both formats present (rare): the newer artifact decides the verdict."""
    ck.write_checkpoint({"w": 1}, iteration=0, result_dir=tmp_path)
    petsc_file = ck.checkpoint_dir(tmp_path) / "checkpoint-3.petscbin"
    petsc_file.write_bytes(_vec_block())
    now = time.time()
    import os

    os.utime(ck.latest_checkpoint(tmp_path), (now - 100, now - 100))
    os.utime(petsc_file, (now, now))
    assert describe_latest_checkpoint(tmp_path)["format"] == "petsc_binary"

    os.utime(ck.latest_checkpoint(tmp_path), (now + 100, now + 100))
    assert describe_latest_checkpoint(tmp_path)["format"] == "pickle"


def test_output_is_json_serializable(tmp_path: Path) -> None:
    """The remote probe json.dumps the verdict verbatim — keep it that way."""
    import json

    ck.write_checkpoint({"w": 1}, iteration=0, result_dir=tmp_path)
    json.dumps(describe_latest_checkpoint(tmp_path))


def test_format_registry_names_are_stable() -> None:
    """The format names are wire-visible (probe verdicts); lock them."""
    assert [f.name for f in checkpoint_formats()] == ["pickle", "petsc_binary"]


# ─── verify_petsc_binary structural rules (adapter-owned) ──────────────────


def test_verify_accepts_multi_block_append(tmp_path: Path) -> None:
    p = tmp_path / "sol.bin"
    p.write_bytes(_vec_block(3) + _vec_block(3) + _vec_block(3))
    out = petsc.verify_petsc_binary(p)
    assert out["status"] == "ok" and "3 complete Vec block(s)" in out["detail"]


def test_verify_accepts_truncated_tail_after_complete_block(tmp_path: Path) -> None:
    """A preemption kill mid-append leaves a partial trailing block; the
    complete prefix is restorable, so the verdict is ok (with the truncation
    noted)."""
    p = tmp_path / "sol.bin"
    p.write_bytes(_vec_block(3) + _vec_block(3)[:10])
    out = petsc.verify_petsc_binary(p)
    assert out["status"] == "ok" and "truncated" in out["detail"]


@pytest.mark.parametrize("scalar_size", [4, 8, 16], ids=["single", "double", "complex"])
def test_verify_handles_all_scalar_flavors(tmp_path: Path, scalar_size: int) -> None:
    p = tmp_path / "sol.bin"
    p.write_bytes(_vec_block(5, scalar_size=scalar_size))
    assert petsc.verify_petsc_binary(p)["status"] == "ok"


@pytest.mark.parametrize(
    "payload",
    [b"", b"\x00" * 4, struct.pack(">ii", 999, 3) + b"\x01" * 24],
    ids=["empty", "short", "wrong-classid"],
)
def test_verify_rejects_non_petsc_bytes(tmp_path: Path, payload: bytes) -> None:
    p = tmp_path / "sol.bin"
    p.write_bytes(payload)
    assert petsc.verify_petsc_binary(p)["status"] == "unloadable"


def test_verify_unreadable_path_is_unloadable(tmp_path: Path) -> None:
    out = petsc.verify_petsc_binary(tmp_path / "absent.bin")
    assert out["status"] == "unloadable" and "unreadable" in out["detail"]


def test_latest_petsc_artifact_prefers_stepped_then_solution_then_restart(
    tmp_path: Path,
) -> None:
    d = tmp_path / "_checkpoints"
    d.mkdir()
    assert petsc.latest_petsc_artifact(tmp_path) is None
    (d / "petsc-restart.bin").write_bytes(b"r")
    assert petsc.latest_petsc_artifact(tmp_path) == (d / "petsc-restart.bin", None)
    (d / "petsc-solution.bin").write_bytes(b"s")
    assert petsc.latest_petsc_artifact(tmp_path) == (d / "petsc-solution.bin", None)
    (d / "checkpoint-2.petscbin").write_bytes(b"c")
    assert petsc.latest_petsc_artifact(tmp_path) == (d / "checkpoint-2.petscbin", 2)
