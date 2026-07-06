"""Tests for the block terminal-result store (``state/block_terminal.py``) — the
run #7 idempotent-replay record a detached block writes on terminal and reads
back on re-invoke instead of re-spawning a worker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.state.block_terminal import read_terminal, record_terminal, terminal_path

if TYPE_CHECKING:
    from pathlib import Path


def test_record_then_read_round_trip(tmp_path: Path) -> None:
    dump = {"stage_reached": "canary_verified", "needs_decision": True, "n": 1}
    rec = record_terminal(tmp_path, run_id="run-1", block="s2", cmd_sha="sha-A", result_dump=dump)
    got = read_terminal(tmp_path, "run-1", "s2")
    assert got is not None
    assert got["cmd_sha"] == "sha-A"
    assert got["result"] == dump
    assert got["run_id"] == "run-1" and got["block"] == "s2"
    assert rec["result"] == got["result"]


def test_record_overwrites_latest_wins(tmp_path: Path) -> None:
    record_terminal(tmp_path, run_id="r", block="s3", cmd_sha="old", result_dump={"v": 1})
    record_terminal(tmp_path, run_id="r", block="s3", cmd_sha="new", result_dump={"v": 2})
    got = read_terminal(tmp_path, "r", "s3")
    assert got is not None
    assert got["cmd_sha"] == "new" and got["result"] == {"v": 2}
    # A single overwritten object, NOT an append log.
    assert terminal_path(tmp_path, "r", "s3").read_text(encoding="utf-8").count("\n") == 0


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert read_terminal(tmp_path, "never-written", "s2") is None


def test_read_corrupt_returns_none(tmp_path: Path) -> None:
    path = terminal_path(tmp_path, "r", "s2")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json", encoding="utf-8")
    assert read_terminal(tmp_path, "r", "s2") is None


def test_disk_locality_beside_the_run_sidecars(tmp_path: Path) -> None:
    expected = tmp_path / ".hpc" / "runs" / "run-x.s2.terminal.json"
    assert terminal_path(tmp_path, "run-x", "s2") == expected


def test_bad_run_id_or_block_rejected(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        terminal_path(tmp_path, "../escape", "s2")
    with pytest.raises(errors.SpecInvalid):
        terminal_path(tmp_path, "r", "")
    with pytest.raises(errors.SpecInvalid):
        record_terminal(tmp_path, run_id="ok", block="a/b", cmd_sha="s", result_dump={})
