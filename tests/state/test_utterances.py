"""Tests for the utterance-log store (``state/utterances.py``).

Covers the multi-human MH2 additive locator on top of the frozen 3-field
write API: the per-actor suffixed log, the union read (ts-merged), the
actor-scoped read that EXCLUDES the anonymous log (the anti-laundering
exclusion), the no-scaffold rule for suffixed files, the invalid-slug
fail-open, and the LOAD-BEARING single-actor byte-identity pin (no actor →
the identical file and bytes as before multi-human).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.state.utterances import (
    append_utterance,
    read_utterances,
    utterances_path,
)


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


def _scaffold_namespace(exp: Path) -> None:
    """Make *exp* an hpc repo — the way real state writes do (creates the
    namespace dir the no-scaffold rule requires to already exist)."""
    from hpc_agent.state.run_record import journal_dir

    journal_dir(exp)


# ── locator ───────────────────────────────────────────────────────────────────


def test_actor_none_is_the_unsuffixed_locator(tmp_path: Path) -> None:
    assert utterances_path(tmp_path).name == "utterances.jsonl"


def test_actor_slug_is_the_suffixed_locator(tmp_path: Path) -> None:
    assert utterances_path(tmp_path, "alice").name == "utterances.alice.jsonl"
    # Same namespace directory, sibling file.
    assert utterances_path(tmp_path, "alice").parent == utterances_path(tmp_path).parent


def test_invalid_actor_slug_raises_from_the_locator(tmp_path: Path) -> None:
    # The slug is a path segment — a path-escaping actor is refused by the
    # shared tag class, not silently sanitized.
    for bad in ("../evil", "a/b", "", "has space"):
        with pytest.raises(errors.SpecInvalid):
            utterances_path(tmp_path, bad)


# ── per-actor round-trip ──────────────────────────────────────────────────────


def test_per_actor_round_trip(tmp_path: Path) -> None:
    _scaffold_namespace(tmp_path)
    rec = append_utterance(tmp_path, "alice typed this", actor="alice")
    assert rec is not None
    # Landed in the suffixed file, NOT the unsuffixed one.
    assert utterances_path(tmp_path, "alice").exists()
    assert not utterances_path(tmp_path).exists()
    # Scoped read returns it; the record is the frozen 3-field shape.
    scoped = read_utterances(tmp_path, actor="alice")
    assert [r["text"] for r in scoped] == ["alice typed this"]
    assert set(scoped[0]) == {"ts", "sha256", "text"}
    assert scoped[0]["sha256"] == hashlib.sha256(b"alice typed this").hexdigest()


def test_scoped_read_excludes_the_anonymous_log(tmp_path: Path) -> None:
    """The anti-laundering exclusion: an actor-scoped read never returns
    unsuffixed (anonymous) text — else actor A's agent could quote text typed
    in an unattributed session as A's own evidence."""
    _scaffold_namespace(tmp_path)
    append_utterance(tmp_path, "anonymous line")  # unsuffixed
    append_utterance(tmp_path, "alice line", actor="alice")
    scoped = read_utterances(tmp_path, actor="alice")
    assert [r["text"] for r in scoped] == ["alice line"]
    # The other actor sees only their own (empty) pool, never the anon log.
    assert read_utterances(tmp_path, actor="bob") == []


def test_scoped_read_excludes_a_different_actor(tmp_path: Path) -> None:
    _scaffold_namespace(tmp_path)
    append_utterance(tmp_path, "alice line", actor="alice")
    append_utterance(tmp_path, "bob line", actor="bob")
    assert [r["text"] for r in read_utterances(tmp_path, actor="alice")] == ["alice line"]
    assert [r["text"] for r in read_utterances(tmp_path, actor="bob")] == ["bob line"]


# ── union read ────────────────────────────────────────────────────────────────


def _write_line(path: Path, ts: str, text: str) -> None:
    """Append a frozen-shape record with a controlled ``ts`` (deterministic
    ordering — the real append stamps seconds-resolution and can collide)."""
    record = {
        "ts": ts,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def test_union_read_merges_all_logs_oldest_first_by_ts(tmp_path: Path) -> None:
    _scaffold_namespace(tmp_path)
    # Interleave the wall-clock across three logs so a plain concat would be
    # out of order; the union must sort by ts.
    _write_line(utterances_path(tmp_path), "2026-07-08T00:00:02+00:00", "anon-2")
    _write_line(utterances_path(tmp_path, "alice"), "2026-07-08T00:00:01+00:00", "alice-1")
    _write_line(utterances_path(tmp_path, "alice"), "2026-07-08T00:00:04+00:00", "alice-4")
    _write_line(utterances_path(tmp_path, "bob"), "2026-07-08T00:00:03+00:00", "bob-3")

    merged = read_utterances(tmp_path)  # actor=None, union default
    assert [r["text"] for r in merged] == ["alice-1", "anon-2", "bob-3", "alice-4"]


def test_union_read_in_single_actor_world_is_just_the_unsuffixed_log(tmp_path: Path) -> None:
    _scaffold_namespace(tmp_path)
    append_utterance(tmp_path, "one")
    append_utterance(tmp_path, "two")
    assert [r["text"] for r in read_utterances(tmp_path)] == ["one", "two"]


def test_union_false_reads_only_the_unsuffixed_log(tmp_path: Path) -> None:
    _scaffold_namespace(tmp_path)
    append_utterance(tmp_path, "anon line")
    append_utterance(tmp_path, "alice line", actor="alice")
    only_anon = read_utterances(tmp_path, union=False)
    assert [r["text"] for r in only_anon] == ["anon line"]


# ── invalid-slug fail-open ────────────────────────────────────────────────────


def test_append_invalid_actor_fails_open_to_unsuffixed(tmp_path: Path) -> None:
    _scaffold_namespace(tmp_path)
    rec = append_utterance(tmp_path, "degraded config", actor="../evil")
    assert rec is not None
    # Landed in the unsuffixed log — never wedged, never a path escape.
    assert utterances_path(tmp_path).exists()
    assert [r["text"] for r in read_utterances(tmp_path, union=False)] == ["degraded config"]


def test_scoped_read_invalid_actor_fails_open_to_unsuffixed(tmp_path: Path) -> None:
    _scaffold_namespace(tmp_path)
    append_utterance(tmp_path, "anon line")
    assert [r["text"] for r in read_utterances(tmp_path, actor="../evil")] == ["anon line"]


# ── no-scaffold ───────────────────────────────────────────────────────────────


def test_no_scaffold_for_a_suffixed_write(tmp_path: Path) -> None:
    """A per-actor append into a non-hpc repo must not leak a namespace dir —
    the same no-scaffold rule as the unsuffixed writer."""
    home = tmp_path / "journal"
    out = append_utterance(tmp_path / "somerepo", "hello", actor="alice")
    assert out is None
    assert not home.exists() or not any(home.iterdir())


def test_no_scaffold_reads_return_empty(tmp_path: Path) -> None:
    assert read_utterances(tmp_path / "somerepo") == []
    assert read_utterances(tmp_path / "somerepo", actor="alice") == []


# ── the single-actor byte-identity pin (LOAD-BEARING) ─────────────────────────


def test_single_actor_byte_identity_no_actor_is_unchanged(tmp_path: Path) -> None:
    """No actor → the IDENTICAL file and bytes as before multi-human. The
    per-actor locator is purely additive: an unconfigured session's log is
    byte-for-byte what it always was."""
    _scaffold_namespace(tmp_path)
    append_utterance(tmp_path, "estimate pi via monte carlo")
    append_utterance(tmp_path, "y")

    path = utterances_path(tmp_path)
    assert path.name == "utterances.jsonl"
    raw = path.read_bytes()
    lines = raw.decode("utf-8").splitlines()
    assert len(lines) == 2
    # Each line is exactly the frozen 3-field record, sorted-keys JSON.
    first = json.loads(lines[0])
    assert set(first) == {"ts", "sha256", "text"}
    assert first["text"] == "estimate pi via monte carlo"
    assert json.dumps(first, sort_keys=True) == lines[0]
    # No suffixed file was created by construction.
    assert list(path.parent.glob("utterances.*.jsonl")) == []
