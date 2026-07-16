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

import base64
import hashlib
import json
import os
import subprocess
import sys
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


def test_push_manifest_write_is_crash_safe_temp_then_replace(tmp_path: Path) -> None:
    """The remote manifest write is a read-modify-write that lands in a temp
    sibling then atomically ``os.replace``-s into place, so a torn checkpoint can
    never corrupt the live manifest. The payload carries paths + pkg_version +
    schema; the merger preserves any existing ``entries`` cache."""
    seen: dict[str, str] = {}

    def _capture(_target, remote_cmd, **_kw):  # noqa: ANN001
        seen["cmd"] = remote_cmd
        return _ok()

    with patch("hpc_agent.infra.transport._ssh_bounded", side_effect=_capture):
        transport._write_push_manifest(
            ssh_target="u@h", remote_path="/r", paths=["x", "y"], timeout=5.0
        )
    cmd = seen["cmd"]
    # The merger is base64-piped into python3 with the payload in HPC_PM_PAYLOAD;
    # no path is a raw shell token, and the live manifest is never a direct
    # redirect target (only the temp is, then os.replace swaps it).
    assert "base64 -d" in cmd
    assert "HPC_PM_PAYLOAD=" in cmd
    assert cmd.rstrip().endswith("python3")
    assert f"> {transport._PUSH_MANIFEST_REL}" not in cmd
    # The merger source itself is crash-safe (temp + os.replace) and preserves entries.
    merger = transport._prune._PUSH_MANIFEST_MERGE_PY
    assert "os.replace(t,d)" in merger
    assert "t=d+'.tmp'" in merger
    assert "new['entries']=cur['entries']" in merger


# --------------------------------------------------------------------------- #
# Rank 5 — remote-side quick-check cache (snippet reads/writes .push_manifest.json)
# --------------------------------------------------------------------------- #


def _run_snippet(tree: Path, exclude: list[str]) -> dict:
    """Execute the REAL deployed hash snippet under this interpreter, cwd=*tree*
    (as the cluster runs it), and return its parsed stdout manifest+telemetry."""
    snippet_file = tree.parent / "snippet.py"
    snippet_file.write_text(transport._REMOTE_MANIFEST_SNIPPET, encoding="utf-8")
    env = {
        **os.environ,
        "HPC_DELTA_EXCLUDES": json.dumps([p.rstrip("/") for p in exclude]),
        "HPC_DELTA_CAP": str(transport._DELTA_MANIFEST_FILE_CAP),
    }
    proc = subprocess.run(
        [sys.executable, str(snippet_file)],
        cwd=str(tree),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert isinstance(result, dict)
    return result


def test_snippet_unchanged_tree_hashes_zero_files(tmp_path: Path) -> None:
    """Fires-and-passes pin: a re-run over an UNCHANGED remote tree re-hashes ZERO
    files — every sha comes from the cache the first run persisted. The cache lives
    in ``.hpc/.push_manifest.json`` (schema 2, per-entry size+mtime_ns+sha256)."""
    tree = tmp_path / "tree"
    (tree / ".hpc").mkdir(parents=True)
    for rel, content in {"a.txt": "alpha", "sub/b.txt": "beta"}.items():
        f = tree / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    exclude = [".hpc/"]  # .hpc bookkeeping is never part of the hashed tree

    cold = _run_snippet(tree, exclude)
    assert cold["hashed"] == 2 and cold["cached"] == 0  # cold: everything hashed

    # The snippet persisted a v2 cache alongside the tree.
    doc = json.loads((tree / ".hpc" / ".push_manifest.json").read_text())
    assert doc["manifest_schema"] == transport._prune._PUSH_MANIFEST_SCHEMA
    assert {e["path"] for e in doc["entries"]} == {"a.txt", "sub/b.txt"}
    assert all("mtime_ns" in e and "sha256" in e for e in doc["entries"])

    warm = _run_snippet(tree, exclude)
    assert warm["hashed"] == 0 and warm["cached"] == 2  # unchanged -> zero re-hash
    # Same content identity either way.
    assert {f["path"]: f["sha256"] for f in cold["files"]} == {
        f["path"]: f["sha256"] for f in warm["files"]
    }


def test_snippet_rehashes_only_the_changed_file(tmp_path: Path) -> None:
    """After a cold pass, editing ONE file re-hashes exactly it; the siblings are
    reused from the cache (the whole point of the remote quick-check)."""
    tree = tmp_path / "tree"
    (tree / ".hpc").mkdir(parents=True)
    for name in ("a.txt", "b.txt", "c.txt"):
        (tree / name).write_text(name)
    exclude = [".hpc/"]
    _run_snippet(tree, exclude)  # seed the cache

    changed = tree / "b.txt"
    changed.write_text("b changed and longer")
    st = changed.stat()
    os.utime(changed, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))  # bump mtime

    warm = _run_snippet(tree, exclude)
    assert warm["hashed"] == 1  # only b.txt
    assert warm["cached"] == 2  # a.txt + c.txt reused
    sha = {f["path"]: f["sha256"] for f in warm["files"]}
    assert sha["b.txt"] == hashlib.sha256(b"b changed and longer").hexdigest()


def test_snippet_full_hashes_on_old_schema_manifest(tmp_path: Path) -> None:
    """Fires-and-passes pin (mandatory back-compat): a v1 manifest an OLDER wheel
    wrote (``{paths, pkg_version}`` — no ``manifest_schema``/``entries``) yields a
    full re-hash with NO crash, and the snippet upgrades it to v2 while PRESERVING
    the ``paths``/``pkg_version`` prune bookkeeping."""
    tree = tmp_path / "tree"
    (tree / ".hpc").mkdir(parents=True)
    (tree / "a.txt").write_text("alpha")
    (tree / "b.txt").write_text("beta")
    (tree / ".hpc" / ".push_manifest.json").write_text(
        json.dumps({"paths": ["a.txt"], "pkg_version": "0.0.1"})
    )
    exclude = [".hpc/"]

    out = _run_snippet(tree, exclude)
    assert out["hashed"] == 2 and out["cached"] == 0  # old schema -> full hash

    doc = json.loads((tree / ".hpc" / ".push_manifest.json").read_text())
    assert doc["manifest_schema"] == transport._prune._PUSH_MANIFEST_SCHEMA  # upgraded
    assert doc["paths"] == ["a.txt"]  # prune bookkeeping preserved
    assert doc["pkg_version"] == "0.0.1"
    assert {e["path"] for e in doc["entries"]} == {"a.txt", "b.txt"}


def test_snippet_full_hashes_on_corrupt_manifest(tmp_path: Path) -> None:
    """A corrupt/garbage manifest never crashes the snippet — empty cache, full
    re-hash, and it is rewritten as a valid v2 doc (fail-open on an optimization)."""
    tree = tmp_path / "tree"
    (tree / ".hpc").mkdir(parents=True)
    (tree / "a.txt").write_text("alpha")
    (tree / ".hpc" / ".push_manifest.json").write_text("{ this is not json ]")
    exclude = [".hpc/"]

    out = _run_snippet(tree, exclude)
    assert out["hashed"] == 1 and out["cached"] == 0
    doc = json.loads((tree / ".hpc" / ".push_manifest.json").read_text())
    assert doc["manifest_schema"] == transport._prune._PUSH_MANIFEST_SCHEMA


def test_snippet_does_not_write_cache_without_hpc_dir(tmp_path: Path) -> None:
    """No ``.hpc/`` (a bare / test tree) -> the snippet emits its manifest but
    writes NO cache file, so it never pollutes a tree that is not a real deploy."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("alpha")
    out = _run_snippet(tree, [])
    assert out["hashed"] == 1
    assert not (tree / ".hpc" / ".push_manifest.json").exists()


def test_remote_cache_survives_the_write_push_manifest_rewrite(tmp_path: Path) -> None:
    """End-to-end money pin for rank 5: the snippet writes the ``entries`` cache in
    step 1; ``_write_push_manifest``'s merger (step 3, AFTER the snippet) updates
    ``paths`` WITHOUT clobbering ``entries``; so the NEXT snippet still reuses the
    cache. Proves the two writers of ``.push_manifest.json`` coexist."""
    tree = tmp_path / "tree"
    (tree / ".hpc").mkdir(parents=True)
    for name in ("a.txt", "b.txt"):
        (tree / name).write_text(name)
    exclude = [".hpc/"]

    _run_snippet(tree, exclude)  # step 1: entries persisted, no paths yet

    # step 3: run the REAL merger (as the ssh command would), updating paths.
    merger = tmp_path / "merge.py"
    merger.write_text(transport._prune._PUSH_MANIFEST_MERGE_PY)
    new_fields = {"paths": ["a.txt", "b.txt"], "pkg_version": "9.9", "manifest_schema": 2}
    payload = base64.b64encode(json.dumps(new_fields).encode()).decode()
    subprocess.run(
        [sys.executable, str(merger)],
        cwd=str(tree),
        env={**os.environ, "HPC_PM_PAYLOAD": payload},
        check=True,
        timeout=30,
    )
    doc = json.loads((tree / ".hpc" / ".push_manifest.json").read_text())
    assert doc["paths"] == ["a.txt", "b.txt"]  # merger updated paths
    assert doc["pkg_version"] == "9.9"
    assert {e["path"] for e in doc["entries"]} == {"a.txt", "b.txt"}  # cache survived

    # next push's snippet still reuses the surviving cache -> zero re-hash.
    warm = _run_snippet(tree, exclude)
    assert warm["hashed"] == 0 and warm["cached"] == 2


def test_write_manifest_merger_preserves_entries_and_is_atomic(tmp_path: Path) -> None:
    """Unit pin on the merger: it updates paths/pkg_version, PRESERVES an existing
    ``entries`` list byte-for-byte, and leaves no ``.tmp`` behind (os.replace)."""
    (tmp_path / ".hpc").mkdir()
    entries = [{"path": "a.txt", "size": 5, "mtime_ns": 123, "sha256": "deadbeef"}]
    (tmp_path / ".hpc" / ".push_manifest.json").write_text(
        json.dumps(
            {"paths": ["old"], "pkg_version": "0.0.1", "manifest_schema": 2, "entries": entries}
        )
    )
    merger = tmp_path / "merge.py"
    merger.write_text(transport._prune._PUSH_MANIFEST_MERGE_PY)
    payload = base64.b64encode(
        json.dumps({"paths": ["n1", "n2"], "pkg_version": "0.9", "manifest_schema": 2}).encode()
    ).decode()
    subprocess.run(
        [sys.executable, str(merger)],
        cwd=str(tmp_path),
        env={**os.environ, "HPC_PM_PAYLOAD": payload},
        check=True,
        timeout=30,
    )
    doc = json.loads((tmp_path / ".hpc" / ".push_manifest.json").read_text())
    assert doc["paths"] == ["n1", "n2"]
    assert doc["pkg_version"] == "0.9"
    assert doc["entries"] == entries  # preserved verbatim
    assert not (tmp_path / ".hpc" / ".push_manifest.json.tmp").exists()  # atomic swap


def test_write_manifest_merger_no_prior_file(tmp_path: Path) -> None:
    """The merger on a fresh tree (no prior manifest) writes the new fields with no
    ``entries`` key and no crash."""
    (tmp_path / ".hpc").mkdir()
    merger = tmp_path / "merge.py"
    merger.write_text(transport._prune._PUSH_MANIFEST_MERGE_PY)
    payload = base64.b64encode(
        json.dumps({"paths": ["x"], "pkg_version": "1.0", "manifest_schema": 2}).encode()
    ).decode()
    subprocess.run(
        [sys.executable, str(merger)],
        cwd=str(tmp_path),
        env={**os.environ, "HPC_PM_PAYLOAD": payload},
        check=True,
        timeout=30,
    )
    doc = json.loads((tmp_path / ".hpc" / ".push_manifest.json").read_text())
    assert doc["paths"] == ["x"]
    assert "entries" not in doc


def test_snippet_schema_matches_prune_constant() -> None:
    """The snippet hardcodes the schema int (it runs stdlib-only cluster-side and
    cannot import); pin the lockstep with the authoritative constant."""
    assert transport._prune._PUSH_MANIFEST_SCHEMA == 2
    assert (
        f"_SCHEMA = {transport._prune._PUSH_MANIFEST_SCHEMA}" in transport._REMOTE_MANIFEST_SNIPPET
    )


# --------------------------------------------------------------------------- #
# Rank 20 — parallel cold local hash (ThreadPool in _build_local_manifest_cached)
# --------------------------------------------------------------------------- #


def test_parallel_and_serial_hash_are_byte_identical(tmp_path: Path, monkeypatch) -> None:
    """Determinism pin: the parallel cold hash produces a manifest AND a cache file
    byte-for-byte identical to the serial path — the reassembly is input-ordered,
    so worker-completion order never leaks into the output."""
    for i in range(12):
        (tmp_path / f"f{i:02d}.bin").write_bytes(bytes([i]) * (50 + i))
    paths = [f"f{i:02d}.bin" for i in range(12)]

    monkeypatch.setenv("HPC_DELTA_HASH_WORKERS", "1")  # serial
    m_ser, hashed_ser, cached_ser = transport._build_local_manifest_cached(tmp_path, paths)
    cache_ser = (tmp_path / transport._PUSH_HASH_CACHE_REL).read_bytes()

    (tmp_path / transport._PUSH_HASH_CACHE_REL).unlink()  # force a cold parallel run

    monkeypatch.setenv("HPC_DELTA_HASH_WORKERS", "8")  # parallel
    m_par, hashed_par, cached_par = transport._build_local_manifest_cached(tmp_path, paths)
    cache_par = (tmp_path / transport._PUSH_HASH_CACHE_REL).read_bytes()

    assert hashed_ser == hashed_par == 12 and cached_ser == cached_par == 0
    assert m_ser.digest == m_par.digest
    assert [e.path for e in m_ser.entries] == [e.path for e in m_par.entries]
    assert cache_ser == cache_par  # byte-identical cache write


def test_hash_workers_bounds(monkeypatch) -> None:
    """The pool is bounded 1..8 and never exceeds the miss count; the env override
    is clamped on both ends."""
    monkeypatch.delenv("HPC_DELTA_HASH_WORKERS", raising=False)
    assert transport._delta._hash_workers(100) == 8  # default cap
    assert transport._delta._hash_workers(3) == 3  # never above the miss count
    assert transport._delta._hash_workers(0) == 1  # empty -> harmless 1
    monkeypatch.setenv("HPC_DELTA_HASH_WORKERS", "2")
    assert transport._delta._hash_workers(100) == 2
    monkeypatch.setenv("HPC_DELTA_HASH_WORKERS", "999")
    assert transport._delta._hash_workers(100) == 8  # clamped to max
    monkeypatch.setenv("HPC_DELTA_HASH_WORKERS", "0")
    assert transport._delta._hash_workers(100) == 1  # floored to 1


def test_parallel_hash_still_reuses_cache_second_pass(tmp_path: Path, monkeypatch) -> None:
    """A parallel cold pass seeds the cache; the second parallel pass over the
    unchanged tree reuses every sha (the pool is only for MISSES)."""
    monkeypatch.setenv("HPC_DELTA_HASH_WORKERS", "6")
    for i in range(6):
        (tmp_path / f"g{i}.txt").write_text(f"body-{i}")
    paths = [f"g{i}.txt" for i in range(6)]
    _, hashed1, cached1 = transport._build_local_manifest_cached(tmp_path, paths)
    assert hashed1 == 6 and cached1 == 0

    def _boom(_p: Path) -> str:
        raise AssertionError("re-hashed a cached file under the parallel path")

    with patch("hpc_agent.infra.manifest._sha256_of", _boom):
        _, hashed2, cached2 = transport._build_local_manifest_cached(tmp_path, paths)
    assert hashed2 == 0 and cached2 == 6
