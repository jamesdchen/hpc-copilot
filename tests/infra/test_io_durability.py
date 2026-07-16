"""Fire paths for the ``append_jsonl_line`` durability seam (unit D-FSYNC).

Three seam-level correctness properties, each with a kill/fault fire path:

* **fsync_required** — the default raises on an ``fsync`` ``OSError`` (no ack
  without durability); ``fsync_required=False`` suppresses and still writes the
  line (best-effort markers). The parent-dir fsync stays best-effort on both.
* **torn-line self-heal** (Δ3 / state-concurrency F4) — a planted torn tail (a
  final line with no trailing newline, the shape a mid-append kill leaves) is
  isolated on its own line, never merged into the next record; both subsequent
  records parse.
* **replay dedup** (Δ2b) — a ``dedup_key`` that already exists on disk is a
  replay no-op (nothing written, the pre-existing record returned); a new key
  appends.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent.infra import io as io_mod
from hpc_agent.infra.io import append_jsonl_line

if TYPE_CHECKING:
    from pathlib import Path


def _read_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── fsync_required (durability ack) ──────────────────────────────────────────


def test_fsync_required_default_raises_on_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill-pair 'fsync-raises -> no ack': the default seam surfaces an fsync
    OSError as a raise, so no caller ever acks a non-durable write."""
    path = tmp_path / "ledger.jsonl"

    def _boom_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(io_mod.os, "fsync", _boom_fsync)
    with pytest.raises(OSError, match="simulated fsync failure"):
        append_jsonl_line(path, {"k": 1})


def test_fsync_required_false_suppresses_and_still_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort marker: fsync_required=False swallows the fsync OSError and
    STILL writes the line (the guaranteed-harvest / deploy-prune posture)."""
    path = tmp_path / "marker.jsonl"

    def _boom_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(io_mod.os, "fsync", _boom_fsync)
    # Must NOT raise, and the line must be on disk.
    append_jsonl_line(path, {"k": 1}, fsync_required=False)
    assert [json.loads(ln) for ln in _read_lines(path)] == [{"k": 1}]


def test_first_append_fsyncs_parent_dir_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-append durability: the append that CREATES the file fsyncs the
    parent dir (dirent durability); a later append into the same file does not."""
    path = tmp_path / "sub" / "ledger.jsonl"
    dir_fsyncs: list[Path] = []
    real = io_mod._fsync_dir

    def _tracked(directory: Path) -> None:
        dir_fsyncs.append(directory)
        real(directory)

    monkeypatch.setattr(io_mod, "_fsync_dir", _tracked)
    append_jsonl_line(path, {"n": 1})
    append_jsonl_line(path, {"n": 2})
    assert dir_fsyncs == [path.parent]  # exactly once, on the creating append


# ── torn-line self-heal (Δ3 / F4) ────────────────────────────────────────────


def test_planted_torn_tail_both_subsequent_records_parse(tmp_path: Path) -> None:
    """Δ3 fire path: plant a torn tail (a final line with no newline, the shape a
    mid-append kill leaves), then append two records. The torn record is isolated
    on its own line and skipped by a tolerant reader; both new records parse and
    are never merged into the torn tail."""
    path = tmp_path / "journal.jsonl"
    # A complete first record, then a TORN partial line (no trailing newline).
    path.write_text('{"i": 0}\n{"i": 1, "part', encoding="utf-8")

    append_jsonl_line(path, {"i": 2})
    append_jsonl_line(path, {"i": 3})

    raw = path.read_text(encoding="utf-8").splitlines()
    # The torn partial got its own line boundary — not merged with {"i": 2}.
    assert '{"i": 1, "part' in raw
    parsed = []
    for ln in raw:
        with contextlib.suppress(json.JSONDecodeError):
            parsed.append(json.loads(ln))  # the torn record is skipped, never merged
    # Both subsequent records parse (and the intact first one).
    assert {"i": 0} in parsed
    assert {"i": 2} in parsed
    assert {"i": 3} in parsed


def test_torn_tail_reported_not_silently_merged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The torn record is REPORTED (logged), never silently merged."""
    path = tmp_path / "journal.jsonl"
    path.write_text('{"i": 1, "part', encoding="utf-8")
    with caplog.at_level("WARNING", logger="hpc_agent.infra.io"):
        append_jsonl_line(path, {"i": 2})
    assert any("torn line boundary" in r.message for r in caplog.records)


def test_no_torn_tail_no_extra_newline(tmp_path: Path) -> None:
    """A well-formed file (last byte == newline) is appended to verbatim — the
    self-heal never injects a spurious blank line."""
    path = tmp_path / "journal.jsonl"
    append_jsonl_line(path, {"i": 1})
    append_jsonl_line(path, {"i": 2})
    text = path.read_text(encoding="utf-8")
    assert text == '{"i": 1}\n{"i": 2}\n'


# ── replay dedup (Δ2b) ───────────────────────────────────────────────────────


def test_dedup_key_same_value_is_replay_no_op(tmp_path: Path) -> None:
    """Same dedup_key twice -> ONE line; the second call returns the original
    record and writes nothing."""
    path = tmp_path / "journal.jsonl"
    first = {"request_id": "r1", "v": "first"}
    r1 = append_jsonl_line(path, first, dedup_key=("request_id", "r1"))
    assert r1 is None  # appended
    r2 = append_jsonl_line(
        path, {"request_id": "r1", "v": "second"}, dedup_key=("request_id", "r1")
    )
    assert r2 == first  # replay: original returned, nothing written
    lines = [json.loads(ln) for ln in _read_lines(path)]
    assert lines == [first]  # exactly one line, the FIRST


def test_dedup_key_different_values_append_two(tmp_path: Path) -> None:
    """Different dedup_keys -> two lines."""
    path = tmp_path / "journal.jsonl"
    append_jsonl_line(path, {"request_id": "r1"}, dedup_key=("request_id", "r1"))
    append_jsonl_line(path, {"request_id": "r2"}, dedup_key=("request_id", "r2"))
    ids = [json.loads(ln)["request_id"] for ln in _read_lines(path)]
    assert ids == ["r1", "r2"]


def test_dedup_ignored_when_no_key(tmp_path: Path) -> None:
    """No dedup_key -> ordinary append-only (byte-identical to before)."""
    path = tmp_path / "journal.jsonl"
    for _ in range(3):
        assert append_jsonl_line(path, {"k": "v"}) is None
    assert len(_read_lines(path)) == 3
