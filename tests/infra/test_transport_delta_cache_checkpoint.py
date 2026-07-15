"""Push-delta hardening: the local hash quick-check cache (run-13 finding 6)
and incremental manifest checkpointing / batched ship (run-13 finding 3).

Finding 6: the delta scan re-hashed the whole tree (39,374 files / 9.9 GB /
~37 min) on every push. The cache keys ``(size, mtime_ns) -> sha`` so an
unchanged file is not re-hashed.

Finding 3: the push manifest committed only at completion, so a died-mid-push
retry re-paid the whole delta. The ship now runs in bounded batches and
checkpoints the manifest after each batch lands; a retry's delta (live remote
hash) then re-ships only the remainder.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

from hpc_agent.infra import transport
from hpc_agent.infra.manifest import FileEntry, Manifest

if TYPE_CHECKING:
    from pathlib import Path


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")


# --------------------------------------------------------------------------- #
# Finding 6 — local hash quick-check cache
# --------------------------------------------------------------------------- #


def test_cache_hit_reuses_sha_without_rehashing(tmp_path: Path) -> None:
    """A second scan of an unchanged tree reuses every cached sha — no re-hash.

    Fires-test: the second build patches ``_sha256_of`` to explode. If the cache
    missed (or ignored the persisted shas) the scan would call it and raise; a
    clean pass proves every file came from the cache.
    """
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")
    paths = ["a.txt", "b.txt"]

    m1, hashed1, cached1 = transport._build_local_manifest_cached(tmp_path, paths)
    assert hashed1 == 2 and cached1 == 0  # cold: everything hashed

    def _boom(_p: Path) -> str:
        raise AssertionError("re-hashed a cached file")

    with patch("hpc_agent.infra.manifest._sha256_of", _boom):
        m2, hashed2, cached2 = transport._build_local_manifest_cached(tmp_path, paths)
    assert hashed2 == 0 and cached2 == 2
    assert m1.digest == m2.digest  # identical manifest, no re-hash


def test_cache_miss_on_mtime_and_content_change(tmp_path: Path) -> None:
    """A file whose (size, mtime_ns) moved is re-hashed to its NEW sha; the
    unchanged sibling is reused. Fires-test: a stale reuse would keep the old
    sha and the digest would not reflect the edit."""
    f = tmp_path / "changing.txt"
    f.write_text("v1")
    (tmp_path / "stable.txt").write_text("stable")
    paths = ["changing.txt", "stable.txt"]

    m1, _, _ = transport._build_local_manifest_cached(tmp_path, paths)
    old_sha = {e.path: e.sha256 for e in m1.entries}["changing.txt"]

    # Rewrite with different content AND bump mtime so the quick-check misses.
    f.write_text("v2 is longer")
    import os

    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    m2, hashed2, cached2 = transport._build_local_manifest_cached(tmp_path, paths)
    new_sha = {e.path: e.sha256 for e in m2.entries}["changing.txt"]
    assert hashed2 == 1  # only the changed file
    assert cached2 == 1  # stable reused
    assert new_sha != old_sha
    assert new_sha == hashlib.sha256(b"v2 is longer").hexdigest()


def test_cache_miss_on_size_change_even_if_mtime_equal(tmp_path: Path) -> None:
    """Size is part of the quick-check key: a same-mtime, different-size file is
    re-hashed (guards the truncate-in-place / restore-mtime corner)."""
    f = tmp_path / "f.txt"
    f.write_text("original")
    paths = ["f.txt"]
    transport._build_local_manifest_cached(tmp_path, paths)  # seed cache
    st = f.stat()
    f.write_text("a much longer body than before")
    import os

    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # force mtime back to seeded value

    _, hashed, cached = transport._build_local_manifest_cached(tmp_path, paths)
    assert hashed == 1 and cached == 0


def test_corrupt_cache_is_discarded_silently(tmp_path: Path) -> None:
    """An unreadable/garbage cache never raises — it is discarded and the scan
    full-re-hashes (fail-open on a pure optimization)."""
    (tmp_path / "a.txt").write_text("alpha")
    cache_path = tmp_path / transport._PUSH_HASH_CACHE_REL
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{ this is not json ]")

    _, hashed, cached = transport._build_local_manifest_cached(tmp_path, ["a.txt"])
    assert hashed == 1 and cached == 0
    # And the corrupt cache was replaced with a valid one.
    doc = json.loads(cache_path.read_text())
    assert doc["version"] == transport._delta._HASH_CACHE_VERSION
    assert "a.txt" in doc["entries"]


def test_cache_wrong_version_is_discarded(tmp_path: Path) -> None:
    """A schema-version bump invalidates every prior entry (full re-hash)."""
    (tmp_path / "a.txt").write_text("alpha")
    cache_path = tmp_path / transport._PUSH_HASH_CACHE_REL
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"version": 999, "entries": {"a.txt": {"size": 5}}}))
    _, hashed, cached = transport._build_local_manifest_cached(tmp_path, ["a.txt"])
    assert hashed == 1 and cached == 0


def test_cache_written_and_prunes_vanished_entries(tmp_path: Path) -> None:
    """The rebuilt cache holds ONLY the current path set — a file dropped from the
    scan drops from the cache too (no unbounded growth)."""
    (tmp_path / "keep.txt").write_text("keep")
    (tmp_path / "gone.txt").write_text("gone")
    transport._build_local_manifest_cached(tmp_path, ["keep.txt", "gone.txt"])
    transport._build_local_manifest_cached(tmp_path, ["keep.txt"])  # gone.txt no longer scanned
    doc = json.loads((tmp_path / transport._PUSH_HASH_CACHE_REL).read_text())
    assert set(doc["entries"]) == {"keep.txt"}


def test_cache_file_is_excluded_from_the_push(tmp_path: Path) -> None:
    """rsync_push must union the cache file into the exclude set so it is never
    hashed, shipped, or seen as a delta ``missing`` (which would re-ship it every
    push and churn forever). The local manifest never lists it."""
    (tmp_path / "real.txt").write_text("payload")
    # A pre-existing cache file on disk (as a real re-push would have).
    cache_path = tmp_path / transport._PUSH_HASH_CACHE_REL
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"version": 1, "entries": {}}))

    captured: dict[str, list[str]] = {}

    def _fake_remote(*, exclude, **_kw):  # noqa: ANN001
        captured["exclude"] = list(exclude)
        return None  # route to full-copy; we only care about the exclude set

    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport._remote_push_manifest", side_effect=_fake_remote),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        tar_proc = popen_mock.return_value
        tar_proc.stdout.read.return_value = b""
        tar_proc.stderr.read.return_value = b""
        tar_proc.returncode = 0
        tar_proc.wait.return_value = 0
        transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
    assert transport._PUSH_HASH_CACHE_REL in captured["exclude"]
    # And the local delta manifest (built with the same exclude) omits it.
    manifest = transport._local_push_manifest(tmp_path, captured["exclude"])
    assert transport._PUSH_HASH_CACHE_REL not in manifest.paths


# --------------------------------------------------------------------------- #
# Finding 3 — batched ship + incremental checkpointing
# --------------------------------------------------------------------------- #


def test_ship_batches_honor_both_caps() -> None:
    """The pure partitioner closes a batch on whichever cap trips first, and an
    oversized single file still ships alone."""
    sizes = {"a": 10, "b": 10, "c": 10, "d": 10}
    # File-count cap of 2.
    by_files = list(
        transport._delta_ship_batches(["a", "b", "c", "d"], sizes, max_files=2, max_bytes=10_000)
    )
    assert by_files == [["a", "b"], ["c", "d"]]
    # Byte cap of 25 -> 2 files per batch (30 would overflow), remainder alone.
    by_bytes = list(
        transport._delta_ship_batches(["a", "b", "c", "d"], sizes, max_files=99, max_bytes=25)
    )
    assert by_bytes == [["a", "b"], ["c", "d"]]
    # An oversized single file is never split.
    big = {"huge": 1_000, "x": 5}
    solo = list(transport._delta_ship_batches(["huge", "x"], big, max_files=99, max_bytes=100))
    assert solo == [["huge"], ["x"]]


def _remote_from(state: dict[str, bytes]) -> Manifest:
    """Build a REMOTE Manifest from a fake remote tree (path -> content bytes)."""
    entries = tuple(
        FileEntry(path=p, size=len(c), sha256=hashlib.sha256(c).hexdigest())
        for p, c in state.items()
    )
    return Manifest(entries=tuple(sorted(entries, key=lambda e: e.path)))


def test_checkpoint_after_each_batch_except_the_last(tmp_path: Path, monkeypatch) -> None:
    """With N batches, the manifest is checkpointed N-1 times mid-ship (the final
    seal covers the last batch), and each checkpoint's path set GROWS as batches
    land — the per-batch cadence the fix promises."""
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text(name * 3)
    monkeypatch.setenv("HPC_DELTA_BATCH_MAX_FILES", "1")  # one file per batch -> 3 batches
    remote_manifest = _remote_from({})  # empty remote -> ship all three

    checkpoints: list[list[str]] = []

    def _record_manifest(*, paths, **_kw):  # noqa: ANN001
        checkpoints.append(list(paths))

    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport._remote_push_manifest", return_value=remote_manifest),
        patch("hpc_agent.infra.transport.guarded_call", side_effect=lambda _t, fn: fn()),
        patch("hpc_agent.infra.transport._tar_ssh_push", return_value=_ok()),
        patch("hpc_agent.infra.transport._prune_manifest_known_extras", return_value=set()),
        patch("hpc_agent.infra.transport._write_push_manifest", side_effect=_record_manifest),
    ):
        result = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
    assert result.returncode == 0
    # 3 batches -> 2 mid-ship checkpoints + 1 final seal = 3 total writes.
    assert len(checkpoints) == 3
    mid = checkpoints[:-1]  # the per-batch checkpoints
    assert [len(p) for p in mid] == [1, 2]  # path set grows batch by batch
    assert set(mid[0]).issubset(set(mid[1]))


def test_died_mid_push_retry_ships_only_the_remainder(tmp_path: Path, monkeypatch) -> None:
    """The finding-3 crash-resume pin: a push that dies after k batches lands
    those k durably; the retry's delta (live remote hash) re-derives them and
    ships ONLY the remainder — never the whole delta again."""
    names = ["a", "b", "c", "d", "e"]
    for n in names:
        (tmp_path / n).write_text(f"body-of-{n}")
    monkeypatch.setenv("HPC_DELTA_BATCH_MAX_FILES", "1")  # 1 file/batch

    fake_remote: dict[str, bytes] = {}
    state = {"fail_path": "c"}  # attempt 1 dies shipping 'c' (after a, b land)
    shipped: list[str] = []

    def _fake_tar(*, only_paths, **_kw):  # noqa: ANN001
        for p in only_paths:
            if p == state["fail_path"]:
                state["fail_path"] = None  # fail exactly once
                return _fail()
            fake_remote[p] = (tmp_path / p).read_bytes()  # batch landed durably
            shipped.append(p)
        return _ok()

    common = [
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.guarded_call", side_effect=lambda _t, fn: fn()),
        patch("hpc_agent.infra.transport._tar_ssh_push", side_effect=_fake_tar),
        patch("hpc_agent.infra.transport._prune_manifest_known_extras", return_value=set()),
        patch("hpc_agent.infra.transport._write_push_manifest"),
        # The retry's delta reads the LIVE remote hash — reflect the fake remote.
        patch(
            "hpc_agent.infra.transport._remote_push_manifest",
            side_effect=lambda **_kw: _remote_from(fake_remote),
        ),
    ]

    # Attempt 1: dies at batch 3 ('c'); a and b land.
    with common[0], common[1], common[2], common[3], common[4], common[5]:
        r1 = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
    assert r1.returncode != 0
    assert set(fake_remote) == {"a", "b"}  # only the confirmed-landed batches
    assert shipped == ["a", "b"]

    shipped.clear()
    # Attempt 2 (retry): the live remote hash now shows a, b -> delta = c, d, e.
    with common[0], common[1], common[2], common[3], common[4], common[5]:
        r2 = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
    assert r2.returncode == 0
    assert shipped == ["c", "d", "e"]  # ONLY the remainder, never a, b again
    assert set(fake_remote) == set(names)  # tree now complete


def test_push_manifest_write_is_crash_safe_temp_then_mv(tmp_path: Path) -> None:
    """The remote manifest write lands in a temp sibling then atomically ``mv``-s
    into place, so a torn checkpoint can never corrupt the live manifest."""
    seen: dict[str, str] = {}

    def _capture(_target, remote_cmd, **_kw):  # noqa: ANN001
        seen["cmd"] = remote_cmd
        return _ok()

    with patch("hpc_agent.infra.transport._ssh_bounded", side_effect=_capture):
        transport._write_push_manifest(
            ssh_target="u@h", remote_path="/r", paths=["x", "y"], timeout=5.0
        )
    cmd = seen["cmd"]
    assert f"> {transport._PUSH_MANIFEST_TMP_REL}" in cmd
    assert f"mv -f {transport._PUSH_MANIFEST_TMP_REL} {transport._PUSH_MANIFEST_REL}" in cmd
    # The live manifest is never a direct redirect target — it appears ONLY as
    # the mv destination (a bare ``> <live> `` redirect, distinct from the
    # ``> <live>.tmp`` one, must be absent).
    assert f"> {transport._PUSH_MANIFEST_REL} " not in cmd
