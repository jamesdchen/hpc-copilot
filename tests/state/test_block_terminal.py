"""Tests for the block terminal-result store (``state/block_terminal.py``) — the
run #7 idempotent-replay record a detached block writes on terminal and reads
back on re-invoke instead of re-spawning a worker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.state.block_terminal import (
    legacy_terminal_block_keys,
    read_terminal,
    read_terminal_with_fallback,
    record_terminal,
    terminal_block_key,
    terminal_path,
)

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


# ── the ONE key derivation (2026-07-07 key-mismatch fix) ─────────────────────


def test_terminal_block_key_is_the_one_derivation() -> None:
    """THE enforcement pin: terminal-store keys have ONE derivation.

    The submit block's short literal maps to the canonical detach VERB, and the
    mapping is IDEMPOTENT on verbs (so the doctor / status-watch, which already
    hold the verb, are no-ops). This is the single fact every writer and reader
    routes through — the submit recorder, the submit replay reader, the
    status-watch recorder, and the doctor dead-worker scan.
    """
    # short literal → canonical verb
    assert terminal_block_key("s2") == "submit-s2"
    assert terminal_block_key("s3") == "submit-s3"
    assert terminal_block_key("s4") == "submit-s4"
    # idempotent on verbs (the lease / doctor / status-watch inputs)
    for verb in ("submit-s2", "submit-s3", "submit-s4", "submit-speculate", "status-watch"):
        assert terminal_block_key(verb) == verb


def test_legacy_keys_only_for_the_numbered_submit_blocks() -> None:
    assert legacy_terminal_block_keys("submit-s2") == ("s2",)
    assert legacy_terminal_block_keys("submit-s4") == ("s4",)
    # speculate and status-watch never wrote a short key → no legacy fallback.
    assert legacy_terminal_block_keys("submit-speculate") == ()
    assert legacy_terminal_block_keys("status-watch") == ()


def test_read_with_fallback_finds_the_canonical_verb_key(tmp_path: Path) -> None:
    record_terminal(tmp_path, run_id="r", block="submit-s2", cmd_sha="x", result_dump={"v": 1})
    # A reader holding the short literal OR the verb resolves the same record.
    canonical = read_terminal(tmp_path, "r", "submit-s2")
    assert read_terminal_with_fallback(tmp_path, "r", "s2") == canonical
    got = read_terminal_with_fallback(tmp_path, "r", "submit-s2")
    assert got is not None and got["result"] == {"v": 1}


def test_read_with_fallback_finds_a_legacy_short_key_record(tmp_path: Path) -> None:
    """A run recorded pre-fix under the short "s4" key is still found by a reader
    that canonicalizes to the verb — the deprecation-window fallback."""
    record_terminal(tmp_path, run_id="old", block="s4", cmd_sha="x", result_dump={"legacy": True})
    # No canonical-key record exists; the reader falls back to the short key.
    assert read_terminal(tmp_path, "old", "submit-s4") is None
    got = read_terminal_with_fallback(tmp_path, "old", "submit-s4")
    assert got is not None and got["result"] == {"legacy": True}
