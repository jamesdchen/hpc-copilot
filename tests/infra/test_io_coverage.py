"""Behavior-pinning MUTATION coverage for :mod:`hpc_agent.infra.io`.

These are the durability primitives EVERYTHING rides: a silent bug here is a
torn write a reader sees mid-swap, a lost update under a broken lock, or a
merged JSONL record that corrupts an append-only ledger. This file pins the
exact fault-path branches and boundary conditions each primitive turns on —
each assertion named with the mutation it kills — complementing
``test_io_durability.py`` / ``test_atomic_*.py`` (the end-to-end behaviors)
with the comparison-operator / dropped-guard seams a mutation tester flips:

* :func:`advisory_flock` — exclusion, non-reentrancy, release-on-exception,
  the bounded-wait timeout boundary.
* :func:`atomic_write_json` / :func:`atomic_write_text` — the crash-window
  property (a reader never sees a partial file; the prior good file survives a
  mid-swap fault) and the fsync discipline where claimed.
* :func:`append_jsonl_line` + helpers — the torn-tail detector's exact byte
  boundary, single-newline heal, and ``dedup_key`` request-id replay boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent.infra import io as io_mod
from hpc_agent.infra.io import (
    _find_dedup_record,
    _has_torn_tail,
    advisory_flock,
    append_jsonl_line,
    atomic_locked_update,
    atomic_write_json,
    atomic_write_text,
)


def _read_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ===========================================================================
# advisory_flock: exclusion, non-reentrancy, release-on-exception, timeout
# ===========================================================================


class TestAdvisoryFlock:
    def test_nonblocking_contention_yields_false_then_true_after_release(self, tmp_path: Path):
        """Same-process, two INSTANCES on one path exclude each other (the
        non-reentrant contract): while the first holds it, a ``blocking=False``
        acquire yields False; once released, the same acquire yields True.
        Kills a mutation that makes the lock a no-op (always-True)."""
        lock = tmp_path / "x.lock"
        with advisory_flock(lock) as held:
            assert held is True
            with advisory_flock(lock, blocking=False) as got:
                assert got is False  # contended: the held lock actually excludes
        # Released now: the non-blocking acquire succeeds.
        with advisory_flock(lock, blocking=False) as got:
            assert got is True

    def test_lock_released_after_exception_in_body(self, tmp_path: Path):
        """The ``finally: lock.release()`` must fire on an exception path — a
        wedged holder that raised must not leak the lock (kills a dropped
        release in the finally)."""
        lock = tmp_path / "y.lock"
        with pytest.raises(RuntimeError, match="boom"), advisory_flock(lock):
            raise RuntimeError("boom mid-hold")
        # If the lock leaked, this non-blocking acquire would see False.
        with advisory_flock(lock, blocking=False) as got:
            assert got is True

    def test_bounded_blocking_raises_loud_pathnaming_timeout(self, tmp_path: Path):
        """Kills a mutation that drops the ``timeout_sec`` bound: a wedged holder
        must surface as a LOUD, path-naming TimeoutError, never a silent
        forever-wait (run-#12 finding 16)."""
        lock = tmp_path / "held.lock"
        with (  # noqa: PT012 - contended acquire under a held lock
            advisory_flock(lock),
            pytest.raises(TimeoutError, match="held.lock"),
            advisory_flock(lock, timeout_sec=0.2),
        ):
            raise AssertionError("must not acquire")  # pragma: no cover

    def test_free_lock_with_timeout_still_acquires(self, tmp_path: Path):
        """A ``timeout_sec`` on an UNCONTENDED lock still yields True — the bound
        is a give-up, not a refusal."""
        with advisory_flock(tmp_path / "free.lock", timeout_sec=5.0) as got:
            assert got is True

    def test_sentinel_left_in_place_after_release(self, tmp_path: Path):
        """The historical lingering-sentinel contract: the ``*.lock`` file must
        remain on disk after release (run-dir loaders find and skip it). Kills a
        mutation that unlinks the sentinel on the release path."""
        lock = tmp_path / "sentinel.lock"
        with advisory_flock(lock):
            pass
        assert lock.exists()


# ===========================================================================
# atomic_write_json / atomic_write_text: crash-window + fsync discipline
# ===========================================================================


class TestAtomicWriteCrashWindow:
    def test_json_swap_failure_preserves_prior_file_and_leaves_no_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The crash-window property: a fault at the ``os.replace`` swap leaves
        the PREVIOUS good file intact and cleans up the temp sibling — a reader
        never sees a partial file. Kills a mutation that truncates in place or
        drops the temp cleanup."""
        path = tmp_path / "doc.json"
        atomic_write_json(path, {"v": "old"})

        def boom(src, dst):
            raise RuntimeError("crash mid-swap")

        monkeypatch.setattr(io_mod, "_replace_with_retry", boom)
        with pytest.raises(RuntimeError):
            atomic_write_json(path, {"v": "new"})
        assert json.loads(path.read_text(encoding="utf-8")) == {"v": "old"}
        assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []

    def test_json_serialize_failure_leaves_no_tmp(self, tmp_path: Path):
        """A payload that fails to serialize must not strand a temp file (the
        ``except: unlink(tmp)`` path)."""
        path = tmp_path / "doc.json"

        class Unserializable:
            pass

        with pytest.raises(TypeError):
            atomic_write_json(path, {"bad": Unserializable()})
        assert not path.exists()
        assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []

    def test_json_never_writes_the_target_path_directly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Atomicity mechanism: bytes go to a mkstemp SIBLING, then are swapped
        in by replace — the target is never opened for writing directly (that is
        the whole crash-window guarantee). Pin: the replace source is a distinct
        temp sibling, not the target itself."""
        path = tmp_path / "doc.json"
        seen: dict[str, str] = {}
        real = io_mod._replace_with_retry

        def spy(src, dst):
            seen["src"] = str(src)
            seen["dst"] = str(dst)
            real(src, dst)

        monkeypatch.setattr(io_mod, "_replace_with_retry", spy)
        atomic_write_json(path, {"ok": 1})
        assert seen["dst"] == str(path)
        assert seen["src"] != str(path)  # swapped from a sibling temp, never in place
        assert Path(seen["src"]).name.startswith(path.name + ".")

    def test_fsync_true_syncs_fd_and_parent_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """fsync=True (the durable default) fsyncs the data fd AND the parent
        dir; fsync=False skips BOTH. Kills a mutation that flips the default or
        drops either sync."""
        path = tmp_path / "doc.json"
        fd_syncs = {"n": 0}
        dir_syncs: list[Path] = []

        def _count_fsync(fd: int) -> None:
            fd_syncs["n"] += 1

        monkeypatch.setattr(io_mod.os, "fsync", _count_fsync)
        monkeypatch.setattr(io_mod, "_fsync_dir", dir_syncs.append)

        atomic_write_json(path, {"a": 1}, fsync=True)
        assert fd_syncs["n"] == 1
        assert dir_syncs == [path.parent]

        fd_syncs["n"] = 0
        dir_syncs.clear()
        atomic_write_json(path, {"a": 2}, fsync=False)
        assert fd_syncs["n"] == 0  # data fsync skipped
        assert dir_syncs == []  # parent-dir fsync skipped

    def test_write_text_preserves_exact_bytes_no_newline_translation(self, tmp_path: Path):
        """``newline=""`` disables translation so an embedded ``\\n`` round-trips
        verbatim (kills a mutation that drops ``newline=""`` and lets win32
        rewrite it to ``\\r\\n``)."""
        path = tmp_path / "manifest.txt"
        text = "line1\nline2\n"
        atomic_write_text(path, text)
        assert path.read_bytes() == text.encode("utf-8")


# ===========================================================================
# _has_torn_tail: the exact last-byte boundary
# ===========================================================================


class TestTornTailDetector:
    def test_missing_file_is_not_torn(self, tmp_path: Path):
        assert _has_torn_tail(tmp_path / "absent.jsonl") is False

    def test_empty_file_is_not_torn(self, tmp_path: Path):
        """Kills a mutation that drops the ``tell() == 0`` guard — an empty file
        has no partial record and must read as NOT torn."""
        path = tmp_path / "empty.jsonl"
        path.write_bytes(b"")
        assert _has_torn_tail(path) is False

    def test_trailing_newline_is_not_torn(self, tmp_path: Path):
        path = tmp_path / "clean.jsonl"
        path.write_bytes(b'{"a": 1}\n')
        assert _has_torn_tail(path) is False

    def test_missing_trailing_newline_is_torn(self, tmp_path: Path):
        """Kills ``read(1) != b"\\n"`` -> ``== b"\\n"`` (inverted sense): a file
        whose last byte is not a newline is a torn tail."""
        path = tmp_path / "torn.jsonl"
        path.write_bytes(b'{"a": 1}\n{"b": 2')  # no trailing newline
        assert _has_torn_tail(path) is True


# ===========================================================================
# append_jsonl_line: torn heal writes exactly ONE newline, never merges
# ===========================================================================


class TestAppendTornHeal:
    def test_torn_tail_healed_with_single_newline_boundary(self, tmp_path: Path):
        """The heal closes the torn record's boundary with exactly ONE ``\\n``
        BEFORE the new line, so the two never merge and no blank line is
        injected. Pin the exact resulting bytes."""
        path = tmp_path / "j.jsonl"
        path.write_bytes(b'{"i": 0}\n{"i": 1, "part')  # torn tail
        append_jsonl_line(path, {"i": 2})
        # read_text normalizes win32 CRLF back to \n (universal newlines) so the
        # boundary pin is platform-stable; the point is EXACTLY one newline heals
        # the torn record and the two never merge.
        assert path.read_text(encoding="utf-8") == '{"i": 0}\n{"i": 1, "part\n{"i": 2}\n'

    def test_clean_file_gets_no_extra_newline(self, tmp_path: Path):
        """The self-heal never fires on a well-formed file — exact pin (no
        spurious blank line injected before the second record)."""
        path = tmp_path / "j.jsonl"
        append_jsonl_line(path, {"i": 1})
        append_jsonl_line(path, {"i": 2})
        assert path.read_text(encoding="utf-8") == '{"i": 1}\n{"i": 2}\n'


# ===========================================================================
# _find_dedup_record + dedup_key replay boundary
# ===========================================================================


class TestDedupReplay:
    def test_find_dedup_matches_field_value_and_skips_corrupt_lines(self, tmp_path: Path):
        """The tolerant scan skips blank/corrupt lines and returns the first
        object whose field==value (kills a mutation that matches on the wrong
        field or stops at the first bad line)."""
        path = tmp_path / "j.jsonl"
        path.write_text(
            '\n{not json\n{"request_id": "a", "v": 1}\n{"request_id": "b", "v": 2}\n',
            encoding="utf-8",
        )
        assert _find_dedup_record(path, "request_id", "b") == {"request_id": "b", "v": 2}
        assert _find_dedup_record(path, "request_id", "missing") is None

    def test_replay_hit_writes_nothing_and_returns_original(self, tmp_path: Path):
        """A repeated ``dedup_key`` is a replay no-op: nothing is appended and the
        PRE-EXISTING record dict is returned (the run-#2 duplicate-greenlight
        race). Kills a mutation that appends anyway or returns the new record."""
        path = tmp_path / "j.jsonl"
        first = {"request_id": "r1", "v": "first"}
        assert append_jsonl_line(path, first, dedup_key=("request_id", "r1")) is None
        got = append_jsonl_line(
            path, {"request_id": "r1", "v": "second"}, dedup_key=("request_id", "r1")
        )
        assert got == first  # original returned verbatim
        assert [json.loads(ln) for ln in _read_lines(path)] == [first]  # exactly one line

    def test_distinct_keys_both_append(self, tmp_path: Path):
        path = tmp_path / "j.jsonl"
        append_jsonl_line(path, {"request_id": "r1"}, dedup_key=("request_id", "r1"))
        r2 = append_jsonl_line(path, {"request_id": "r2"}, dedup_key=("request_id", "r2"))
        assert r2 is None
        assert [json.loads(ln)["request_id"] for ln in _read_lines(path)] == ["r1", "r2"]

    def test_dedup_on_absent_file_appends_without_scanning(self, tmp_path: Path):
        """Kills a mutation that drops the ``existed`` guard before the dedup
        scan: a first append with a dedup_key to a NON-existent file must simply
        append (nothing to dedup against), not crash reading a missing file."""
        path = tmp_path / "fresh.jsonl"
        assert not path.exists()
        assert append_jsonl_line(path, {"request_id": "r1"}, dedup_key=("request_id", "r1")) is None
        assert [json.loads(ln) for ln in _read_lines(path)] == [{"request_id": "r1"}]


# ===========================================================================
# append_jsonl_line durability ack + serialization contract
# ===========================================================================


class TestAppendDurabilityAndSerialization:
    def test_default_fsync_required_raises_on_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Default 'no ack without durability': an fsync OSError RAISES so a
        source-of-truth caller never acks a non-durable write."""
        path = tmp_path / "ledger.jsonl"
        monkeypatch.setattr(
            io_mod.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync down"))
        )
        with pytest.raises(OSError, match="fsync down"):
            append_jsonl_line(path, {"k": 1})

    def test_fsync_required_false_suppresses_and_still_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Best-effort marker: fsync_required=False swallows the OSError and the
        line is STILL on disk (kills a mutation that flips the suppression)."""
        path = tmp_path / "marker.jsonl"
        monkeypatch.setattr(
            io_mod.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync down"))
        )
        append_jsonl_line(path, {"k": 1}, fsync_required=False)  # must not raise
        assert [json.loads(ln) for ln in _read_lines(path)] == [{"k": 1}]

    def test_non_json_native_values_serialize_via_default_str(self, tmp_path: Path):
        """``default=str`` is applied unconditionally so a ``Path`` (or datetime)
        serializes rather than raising — kills a mutation that drops it."""
        path = tmp_path / "j.jsonl"
        append_jsonl_line(path, {"p": Path("/x/y")})
        rec = json.loads(_read_lines(path)[0])
        assert rec["p"].replace("\\", "/") == "/x/y"

    def test_first_append_fsyncs_parent_dir_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """First-append durability: the append that CREATES the file fsyncs the
        parent dir (dirent durability); a subsequent append does not."""
        path = tmp_path / "sub" / "ledger.jsonl"
        dir_syncs: list[Path] = []
        monkeypatch.setattr(io_mod, "_fsync_dir", lambda d: dir_syncs.append(d))
        append_jsonl_line(path, {"n": 1})
        append_jsonl_line(path, {"n": 2})
        assert dir_syncs == [path.parent]  # only on the creating append


# ===========================================================================
# atomic_locked_update: read-modify-write under the lock, no-corrupt on fault
# ===========================================================================


class TestAtomicLockedUpdate:
    def test_returns_the_written_doc(self, tmp_path: Path):
        path = tmp_path / "d.json"
        out = atomic_locked_update(path, lambda _cur: {"n": 7})
        assert out == {"n": 7}
        assert json.loads(path.read_text(encoding="utf-8")) == {"n": 7}

    def test_mutate_sees_none_on_missing_and_on_corrupt(self, tmp_path: Path):
        """The read happens inside the lock and hands ``None`` for a missing OR
        unparseable file (kills a mutation that raises on a corrupt doc instead
        of degrading to None)."""
        seen: list[object] = []

        def _record(cur: object) -> dict:
            seen.append(cur)
            return {"ok": 1}

        missing = tmp_path / "missing.json"
        atomic_locked_update(missing, _record)
        assert seen == [None]

        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{not json", encoding="utf-8")
        seen.clear()
        atomic_locked_update(corrupt, _record)
        assert seen == [None]

    def test_mutate_raises_keeps_prior_contents_and_no_tmp(self, tmp_path: Path):
        """A mutate that raises must leave the previous doc intact with no temp
        residue (the crash-window property for the RMW path)."""
        path = tmp_path / "d.json"
        atomic_write_json(path, {"a": 1})

        def boom(_cur):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            atomic_locked_update(path, boom)
        assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
        assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []
