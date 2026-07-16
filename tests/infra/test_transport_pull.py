"""Content-hash PULL engine: server-side filtering + delta + resumable batches.

The pull analogue of the batched push (latency ranks 2 + 7 + 25). Covers the
pure batching/command builders, the delta orchestration (mocking the ssh
transfer), resumability, the manifest-less fallback, tar-stream compression, the
connect-failure retry, and the ``rsync_pull`` reroute onto the engine.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hpc_agent.infra import transport
from hpc_agent.infra.manifest import FileEntry, Manifest
from hpc_agent.infra.transport import _pull


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=stderr)


def _fail(returncode: int, stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def _manifest(entries: dict[str, str]) -> Manifest:
    """Build a Manifest from {relpath: content-sha-stand-in}; size = len(sha)."""
    return Manifest(
        entries=tuple(
            sorted(
                (FileEntry(path=p, size=len(sha), sha256=sha) for p, sha in entries.items()),
                key=lambda e: e.path,
            )
        )
    )


# --- pure: batching -----------------------------------------------------------


def test_ship_batches_respect_file_cap():
    pull = ["a", "b", "c", "d", "e"]
    sizes = dict.fromkeys(pull, 1)
    batches = list(
        _pull._pull_ship_batches(pull, sizes, max_files=2, max_bytes=10**9, max_name_bytes=10**9)
    )
    assert batches == [["a", "b"], ["c", "d"], ["e"]]


def test_ship_batches_respect_byte_cap():
    pull = ["a", "b", "c"]
    sizes = {"a": 100, "b": 100, "c": 100}
    batches = list(
        _pull._pull_ship_batches(pull, sizes, max_files=10**9, max_bytes=250, max_name_bytes=10**9)
    )
    assert batches == [["a", "b"], ["c"]]


def test_ship_batches_respect_name_cap():
    # The pull-specific cap: the member list rides in the ssh command string.
    pull = ["aaaa", "bbbb", "cccc"]
    sizes = dict.fromkeys(pull, 1)
    # each name costs len(name)+3 = 7 bytes; cap 10 => one name per batch.
    batches = list(
        _pull._pull_ship_batches(pull, sizes, max_files=99, max_bytes=10**9, max_name_bytes=10)
    )
    assert batches == [["aaaa"], ["bbbb"], ["cccc"]]


def test_oversized_single_file_still_ships_alone():
    pull = ["big", "small"]
    sizes = {"big": 10**9, "small": 1}
    batches = list(
        _pull._pull_ship_batches(pull, sizes, max_files=99, max_bytes=100, max_name_bytes=10**9)
    )
    assert batches == [["big"], ["small"]]


# --- pure: remote command builders --------------------------------------------


def test_find_predicate_bare_include_uses_name():
    pred = _pull._find_filter_predicate(["metrics.json"], None)
    assert "-name metrics.json" in pred
    assert "\\(" in pred and "\\)" in pred


def test_find_predicate_slashed_include_uses_path():
    pred = _pull._find_filter_predicate(["sub/summary.json"], None)
    assert "-path ./sub/summary.json" in pred


def test_find_predicate_exclude_negates():
    pred = _pull._find_filter_predicate(["*.json"], ["skip.json"])
    assert "! -name skip.json" in pred
    assert "-name '*.json'" in pred  # glob is quoted so the login shell can't expand it


def test_find_predicate_empty_when_no_filter():
    assert _pull._find_filter_predicate(None, None) == ""


def test_batch_remote_cmd_ships_names_via_base64_and_tempfile():
    cmd = _pull._batch_remote_cmd("/r/results", ["a.json", "sub/b.json"], "-z")
    assert cmd.startswith("cd /r/results && ")
    assert 'T="$(mktemp)"' in cmd
    assert "base64 -d" in cmd
    assert "tar c -z -C /r/results -T" in cmd
    assert 'rm -f "$T"' in cmd  # temp removed regardless of tar exit
    # The names are NOT in cleartext (they ride as base64).
    assert "a.json" not in cmd


def test_batch_remote_cmd_decodes_to_expected_names(tmp_path):
    # Decode the base64 blob out of the command and confirm the member list.
    import base64
    import re

    cmd = _pull._batch_remote_cmd("/r", ["x/one.txt", "two.txt"], None)
    m = re.search(r"printf %s (\S+) \|", cmd)
    assert m
    blob = m.group(1).strip("'")
    decoded = base64.b64decode(blob).decode()
    assert decoded == "./x/one.txt\n./two.txt\n"
    # No codec flag when compression is disabled.
    assert "tar c -C /r -T" in cmd


def test_fallback_remote_cmd_filters_server_side_and_compresses():
    cmd = _pull._fallback_remote_cmd("/r/results", ["metrics.json"], None, "-z")
    assert cmd.startswith("cd /r/results && find . -type f")
    assert "-name metrics.json" in cmd
    assert "-print0 | tar c -z --null --no-recursion -T - -f -" in cmd


# --- local present manifest ---------------------------------------------------


def test_local_present_manifest_hashes_only_present_files(tmp_path):
    (tmp_path / "present.txt").write_text("here")
    manifest = _pull._local_present_manifest(tmp_path, ["present.txt", "absent.txt"])
    paths = [e.path for e in manifest.entries]
    assert paths == ["present.txt"]  # absent one silently skipped (it's what we'll pull)


def _age_file(path: Path, seconds: float = 10.0) -> None:
    """Backdate *path*'s mtime past the pull-cache skew window so a stat-match is
    trusted from cache (a just-written file is treated as dirty by the skew guard)."""
    import os

    st = path.stat()
    old = st.st_mtime - seconds
    os.utime(path, (old, old))


def test_local_present_manifest_reuses_cache(tmp_path, monkeypatch):
    (tmp_path / "f.txt").write_text("content")
    _age_file(tmp_path / "f.txt")  # clear the skew window (pin flip: F2 skew guard)
    # First call hashes + writes the cache.
    m1 = _pull._local_present_manifest(tmp_path, ["f.txt"])
    sha = m1.entries[0].sha256
    # Second call must reuse the cache (no re-hash) — spy on _sha256_of.
    import hpc_agent.infra.manifest as manifest_mod

    calls = {"n": 0}
    real = manifest_mod._sha256_of

    def _spy(p):
        calls["n"] += 1
        return real(p)

    monkeypatch.setattr(manifest_mod, "_sha256_of", _spy)
    m2 = _pull._local_present_manifest(tmp_path, ["f.txt"])
    assert m2.entries[0].sha256 == sha
    assert calls["n"] == 0  # size+mtime matched -> cached sha reused


def test_local_present_manifest_young_file_rehashed(tmp_path, monkeypatch):
    """G2 skew window: a file modified within the skew window counts DIRTY.

    A just-written file (mtime ~ now) is never trusted from stat alone — a torn
    write in flight can share the prior mtime at coarse fs granularity.
    """
    (tmp_path / "f.txt").write_text("content")  # NOT aged: young
    _pull._local_present_manifest(tmp_path, ["f.txt"])  # seed cache
    import hpc_agent.infra.manifest as manifest_mod

    calls = {"n": 0}
    real = manifest_mod._sha256_of
    monkeypatch.setattr(
        manifest_mod, "_sha256_of", lambda p: (calls.__setitem__("n", calls["n"] + 1), real(p))[1]
    )
    _pull._local_present_manifest(tmp_path, ["f.txt"])
    assert calls["n"] == 1  # young -> re-hashed despite the stat match


def test_local_present_manifest_moved_cmd_sha_evicts_on_stat_match(tmp_path):
    """FIRES (D2, run-13 finding-13 pull direction): a moved ``.hpc_cmd_sha`` evicts
    the cached sha even when size+mtime collide — the torn-overwrite class.

    Mirrors tests/ops/aggregate/test_flow_stale_mirror.py: a repair/graft re-runs
    a task, moving its cmd_sha sidecar, but a torn overwrite leaves the summary's
    (size, mtime_ns) unchanged. The OLD stat-only cache would serve the stale sha
    (the pull-delta then judges local==remote and never re-pulls); the cmd_sha
    gate re-hashes, so the manifest reflects the file actually on disk.
    """
    import os

    task = tmp_path / "task_0"
    task.mkdir()
    summary = task / "metrics.json"
    summary.write_text('{"metric": 999.0}')  # the stale Jul-11 blown copy
    (task / ".hpc_cmd_sha").write_text("a" * 64)
    _age_file(summary)
    m1 = _pull._local_present_manifest(tmp_path, ["task_0/metrics.json"])
    stale_sha = m1.entries[0].sha256

    # Torn overwrite: same byte length -> same size; restore mtime -> stat collides.
    st = summary.stat()
    summary.write_text('{"metric": 007.0}')  # fresh value, identical length
    os.utime(summary, ns=(st.st_atime_ns, st.st_mtime_ns))
    (task / ".hpc_cmd_sha").write_text("b" * 64)  # cmd_sha moved on the graft

    m2 = _pull._local_present_manifest(tmp_path, ["task_0/metrics.json"])
    assert summary.stat().st_mtime_ns == st.st_mtime_ns  # stat really did collide
    assert m2.entries[0].sha256 != stale_sha  # evicted + re-hashed to the fresh value

    # PASSES: an unchanged cmd_sha with an unchanged stat is served from cache.
    m3 = _pull._local_present_manifest(tmp_path, ["task_0/metrics.json"])
    assert m3.entries[0].sha256 == m2.entries[0].sha256


def test_local_present_manifest_foreign_rows_dropped(tmp_path):
    """G2 foreign-rows guard: a cache row for a path not in the current key set is
    never served and drops out of the rewritten cache."""
    import json as _json

    (tmp_path / "keep.txt").write_text("keep")
    _age_file(tmp_path / "keep.txt")
    # Pre-seed the cache with a FOREIGN row (a path absent from this call's set).
    hpc = tmp_path / ".hpc"
    hpc.mkdir()
    (hpc / ".pull_hash_cache.json").write_text(
        _json.dumps(
            {
                "version": 1,
                "entries": {
                    "keep.txt": {"size": 4, "mtime_ns": 0, "sha256": "wrong", "cmd_sha": ""},
                    "foreign.txt": {"size": 9, "mtime_ns": 9, "sha256": "x", "cmd_sha": ""},
                },
            }
        )
    )
    manifest = _pull._local_present_manifest(tmp_path, ["keep.txt"])
    assert [e.path for e in manifest.entries] == ["keep.txt"]  # foreign row never manifested
    # The rewritten cache holds only the current key set.
    doc = _json.loads((hpc / ".pull_hash_cache.json").read_text())
    assert set(doc["entries"]) == {"keep.txt"}


def test_local_present_manifest_severed_read_not_cached(tmp_path, monkeypatch):
    """D2 success-only: a read that raises is cached by neither manifest nor cache."""
    import json as _json

    (tmp_path / "f.txt").write_text("content")
    _age_file(tmp_path / "f.txt")
    import hpc_agent.infra.manifest as manifest_mod

    def _boom(_p):
        raise OSError("VPN severed mid-hash")

    monkeypatch.setattr(manifest_mod, "_sha256_of", _boom)
    manifest = _pull._local_present_manifest(tmp_path, ["f.txt"])
    assert manifest.entries == ()  # severed read -> not manifested
    doc = _json.loads((tmp_path / ".hpc" / ".pull_hash_cache.json").read_text())
    assert doc["entries"] == {}  # ... and not cached


# --- delta orchestration (ssh transfer mocked) --------------------------------


def _patch_transfer(record: list, results=None):
    """Patch the engine's ssh transfer to record remote_cmds and return results.

    *results* is a list of CompletedProcess returned in order (default: all ok).
    """
    seq = list(results or [])

    def fake(*, ssh_target, remote_cmd, local_path, codec_flag, total_bytes, timeout):
        record.append(remote_cmd)
        return seq.pop(0) if seq else _ok()

    return patch.object(_pull, "_pull_transfer_with_retry", side_effect=fake)


def test_delta_pulls_exactly_the_changed_set(tmp_path, capsys):
    # Remote has 3 files; one already-identical locally, one differs, one new.
    (tmp_path / "same.txt").write_text("s")
    remote = _manifest({"same.txt": "aaa", "changed.txt": "bbb", "new.txt": "ccc"})
    # local present: same.txt matches remote sha; changed.txt present but differs.
    (tmp_path / "changed.txt").write_text("old")

    record: list[str] = []
    with (
        patch.object(_pull, "_remote_pull_manifest", return_value=remote),
        patch.object(
            _pull,
            "_local_present_manifest",
            return_value=_manifest({"same.txt": "aaa", "changed.txt": "OLD"}),
        ),
        _patch_transfer(record),
    ):
        result = _pull.tar_ssh_pull(ssh_target="u@h", remote_path="/r/results", local_path=tmp_path)
    assert result.ok
    assert result.files_pulled == 2  # changed.txt + new.txt
    assert result.skipped_unchanged == 1  # same.txt
    # Exactly one batch, and its remote cmd names ONLY the changed set (base64).
    assert len(record) == 1
    import base64
    import re

    blob = re.search(r"printf %s (\S+) \|", record[0]).group(1).strip("'")
    names = base64.b64decode(blob).decode()
    assert "./changed.txt" in names and "./new.txt" in names
    assert "./same.txt" not in names
    assert "content-hash PULL delta" in capsys.readouterr().err


def test_delta_nothing_to_pull_when_all_identical(tmp_path):
    remote = _manifest({"a.txt": "h1", "b.txt": "h2"})
    record: list[str] = []
    with (
        patch.object(_pull, "_remote_pull_manifest", return_value=remote),
        patch.object(_pull, "_local_present_manifest", return_value=remote),
        _patch_transfer(record),
    ):
        result = _pull.tar_ssh_pull(ssh_target="u@h", remote_path="/r", local_path=tmp_path)
    assert result.ok
    assert result.files_pulled == 0
    assert result.skipped_unchanged == 2
    assert record == []  # zero transfers


def test_delta_batches_split_across_transfers(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_PULL_BATCH_MAX_FILES", "1")  # one file per batch
    remote = _manifest({"a.txt": "h1", "b.txt": "h2"})
    record: list[str] = []
    with (
        patch.object(_pull, "_remote_pull_manifest", return_value=remote),
        patch.object(_pull, "_local_present_manifest", return_value=_manifest({})),
        _patch_transfer(record),
    ):
        result = _pull.tar_ssh_pull(ssh_target="u@h", remote_path="/r", local_path=tmp_path)
    assert result.ok
    assert result.files_pulled == 2
    assert len(record) == 2  # two batches -> two transfers


def test_resumable_partial_pull_reports_landed_progress(tmp_path, monkeypatch):
    # Batch 1 lands, batch 2 fails -> ok=False, but files_pulled counts batch 1.
    monkeypatch.setenv("HPC_PULL_BATCH_MAX_FILES", "1")
    remote = _manifest({"a.txt": "h1", "b.txt": "h2"})
    record: list[str] = []
    with (
        patch.object(_pull, "_remote_pull_manifest", return_value=remote),
        patch.object(_pull, "_local_present_manifest", return_value=_manifest({})),
        _patch_transfer(record, results=[_ok(), _fail(1, "VPN severed mid-pull")]),
    ):
        result = _pull.tar_ssh_pull(ssh_target="u@h", remote_path="/r", local_path=tmp_path)
    assert not result.ok
    assert result.files_pulled == 1  # batch 1 landed durably
    assert "VPN severed" in result.stderr_tail
    assert len(record) == 2  # stopped after the failing batch


# --- manifest-less fallback ---------------------------------------------------


def test_fallback_when_no_remote_manifest(tmp_path, capsys):
    # Simulate an already-landed filtered set so _count_landed can measure it.
    (tmp_path / "metrics.json").write_text("{}")
    (tmp_path / "other.csv").write_text("x,y")
    record: list[str] = []
    with (
        patch.object(_pull, "_remote_pull_manifest", return_value=None),
        _patch_transfer(record),
    ):
        result = _pull.tar_ssh_pull(
            ssh_target="u@h",
            remote_path="/r",
            local_path=tmp_path,
            include_globs=["metrics.json"],
        )
    assert result.ok
    assert result.files_pulled == 1  # only the include-matching file counted
    assert result.skipped_unchanged == 0  # no delta on the fallback
    # The fallback still filters server-side (find|tar), just without the delta.
    assert record and record[0].startswith("cd /r && find . -type f")
    assert "no delta" in capsys.readouterr().err


def test_fallback_failure_surfaces(tmp_path):
    record: list[str] = []
    with (
        patch.object(_pull, "_remote_pull_manifest", return_value=None),
        _patch_transfer(record, results=[_fail(255, "connect: host down")]),
    ):
        result = _pull.tar_ssh_pull(ssh_target="u@h", remote_path="/r", local_path=tmp_path)
    assert not result.ok
    assert "host down" in result.stderr_tail


# --- compression wiring -------------------------------------------------------


def test_compression_flag_flows_into_remote_and_local_tar(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_TAR_STREAM_COMPRESSION", "gzip")
    remote = _manifest({"a.txt": "h1"})
    captured = {}

    def fake(*, ssh_target, remote_cmd, local_path, codec_flag, total_bytes, timeout):
        captured["remote_cmd"] = remote_cmd
        captured["codec_flag"] = codec_flag
        return _ok()

    with (
        patch.object(_pull, "_remote_pull_manifest", return_value=remote),
        patch.object(_pull, "_local_present_manifest", return_value=_manifest({})),
        patch.object(_pull, "_pull_transfer_with_retry", side_effect=fake),
    ):
        _pull.tar_ssh_pull(ssh_target="u@h", remote_path="/r", local_path=tmp_path)
    assert captured["codec_flag"] == "-z"  # local tar x will use -z
    assert "tar c -z -C /r" in captured["remote_cmd"]  # remote tar c uses -z


def test_compression_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_TAR_STREAM_COMPRESSION", "none")
    remote = _manifest({"a.txt": "h1"})
    captured = {}

    def fake(*, ssh_target, remote_cmd, local_path, codec_flag, total_bytes, timeout):
        captured["codec_flag"] = codec_flag
        captured["remote_cmd"] = remote_cmd
        return _ok()

    with (
        patch.object(_pull, "_remote_pull_manifest", return_value=remote),
        patch.object(_pull, "_local_present_manifest", return_value=_manifest({})),
        patch.object(_pull, "_pull_transfer_with_retry", side_effect=fake),
    ):
        _pull.tar_ssh_pull(ssh_target="u@h", remote_path="/r", local_path=tmp_path)
    assert captured["codec_flag"] is None
    assert "tar c -C /r" in captured["remote_cmd"]  # no codec flag


# --- connect-failure retry (rank 25) ------------------------------------------


def test_connect_failure_retries_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(_pull.time, "sleep", lambda s: sleeps.append(s))
    results = [_fail(255, "ssh: connect ...: Connection refused"), _ok()]
    with (
        patch.object(_pull, "guarded_call", side_effect=lambda target, fn: fn()),
        patch.object(_pull, "_pull_transfer", side_effect=lambda **kw: results.pop(0)),
    ):
        proc = _pull._pull_transfer_with_retry(
            ssh_target="u@h",
            remote_cmd="x",
            local_path=Path("."),
            codec_flag=None,
            total_bytes=0,
            timeout=1,
        )
    assert proc.returncode == 0
    assert sleeps == [2.0]  # one retry on the tight schedule


def test_non_connect_failure_not_retried(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(_pull.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def one_shot(**kw):
        calls["n"] += 1
        return _fail(2, "tar: remote command failed")

    with (
        patch.object(_pull, "guarded_call", side_effect=lambda target, fn: fn()),
        patch.object(_pull, "_pull_transfer", side_effect=one_shot),
    ):
        proc = _pull._pull_transfer_with_retry(
            ssh_target="u@h",
            remote_cmd="x",
            local_path=Path("."),
            codec_flag=None,
            total_bytes=0,
            timeout=1,
        )
    assert proc.returncode == 2
    assert calls["n"] == 1  # a remote-command error is not a connect retry
    assert sleeps == []


def test_spawn_enoent_not_retried(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(_pull.time, "sleep", lambda s: sleeps.append(s))

    def boom(**kw):
        raise FileNotFoundError("ssh not found")

    with (
        patch.object(_pull, "guarded_call", side_effect=lambda target, fn: fn()),
        patch.object(_pull, "_pull_transfer", side_effect=boom),
    ):
        proc = _pull._pull_transfer_with_retry(
            ssh_target="u@h",
            remote_cmd="x",
            local_path=Path("."),
            codec_flag=None,
            total_bytes=0,
            timeout=1,
        )
    assert proc.returncode == 127
    assert "ENOENT" in proc.stderr
    assert sleeps == []


# --- rsync_pull reroute onto the engine ---------------------------------------


def test_rsync_pull_routes_rsyncless_to_tar_ssh_pull(tmp_path):
    """On the rsync-less path, rsync_pull delegates to the PULL engine, joining
    remote_subdir onto remote_path and passing include through as include_globs."""
    captured = {}

    def fake_engine(*, ssh_target, remote_path, local_path, include_globs, timeout):
        captured.update(
            ssh_target=ssh_target,
            remote_path=remote_path,
            include_globs=include_globs,
        )
        return transport.PullResult(
            ok=True, files_pulled=3, bytes_pulled=99, skipped_unchanged=1, stderr_tail=""
        )

    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.tar_ssh_pull", side_effect=fake_engine),
    ):
        proc = transport.rsync_pull(
            ssh_target="u@h",
            remote_path="/r",
            remote_subdir="_combiner",
            local_dir=tmp_path / "out",
            include=["wave_*.json"],
        )
    assert proc.returncode == 0  # PullResult.ok -> rc 0
    assert captured["ssh_target"] == "u@h"
    assert captured["remote_path"] == "/r/_combiner"  # subdir joined
    assert captured["include_globs"] == ["wave_*.json"]


def test_rsync_pull_reroute_maps_failure_to_nonzero(tmp_path):
    def fake_engine(**kw):
        return transport.PullResult(
            ok=False, files_pulled=0, bytes_pulled=0, skipped_unchanged=0, stderr_tail="boom"
        )

    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.tar_ssh_pull", side_effect=fake_engine),
    ):
        proc = transport.rsync_pull(
            ssh_target="u@h", remote_path="/r", remote_subdir="_combiner", local_dir=tmp_path / "o"
        )
    assert proc.returncode == 1
    assert proc.stderr == "boom"


def test_rsync_pull_still_uses_rsync_when_present(tmp_path):
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value="/usr/bin/rsync"),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()) as run_mock,
    ):
        transport.rsync_pull(
            ssh_target="u@h", remote_path="/r", remote_subdir="_combiner", local_dir=tmp_path / "o"
        )
    assert run_mock.call_args[0][0][0] == "rsync"  # engine reroute is fallback-only


# --- F7: transfer-plane bypasses the engine + preamble (byte-equality) --------
#
# The verify-during-build claim (rt.transfer-plane-bypasses-engine): agent B's
# preamble-free control plane (env_python / remote_activation_for_sidecar) lives
# in clusters.py/submit_flow.py/host_retarget.py/monitor_flow.py — the CONTROL
# plane. Transfer-plane data ops (tar|ssh pull, the manifest round-trip) build
# their remote command directly and spawn ssh via ssh_argv, never routing through
# ssh_engine (capture-mode ssh_run only) nor prepending any conda/module preamble.
# These pins keep it that way: a preamble token appearing in a transfer command,
# or the manifest round-trip's argv diverging from the raw ssh_argv form, reds.

_PREAMBLE_MARKERS = (
    "conda activate",
    "module load",
    "hpc_preamble",
    "source ",
    "CONDA_SOURCE",
    "HPC_AGENT_OP",  # build_remote_command's control-plane marker
)


def test_transfer_plane_remote_cmds_carry_no_preamble():
    batch = _pull._batch_remote_cmd("/r/results", ["a.json", "sub/b.json"], "-z")
    fallback = _pull._fallback_remote_cmd("/r/results", ["metrics.json"], None, "-z")
    for cmd in (batch, fallback):
        for marker in _PREAMBLE_MARKERS:
            assert marker not in cmd, f"preamble token {marker!r} leaked into {cmd!r}"


def test_manifest_round_trip_argv_is_raw_ssh_no_preamble(monkeypatch):
    """Byte-level: the manifest ssh argv is exactly ssh_argv + target + raw cmd —
    no engine channel, no preamble wrap between our command and ssh."""
    from hpc_agent.infra.ssh_options import ssh_argv

    captured = {}

    def _fake_run(argv, *, timeout_sec, **_kw):
        captured["argv"] = list(argv)
        return _ok(stdout="")

    monkeypatch.setattr(_pull, "run_capture_bounded", _fake_run)
    monkeypatch.setattr(_pull, "run_with_named_pipe_retry", lambda fn: fn())
    _pull._ssh_capture("u@h", "cd /r && echo hi", timeout=5, what="manifest")

    argv = captured["argv"]
    assert argv[:-2] == list(ssh_argv("ssh"))  # the raw ssh invocation, no engine
    assert argv[-2] == "u@h"
    assert argv[-1] == "cd /r && echo hi"  # command byte-identical, unpreambled
    for marker in _PREAMBLE_MARKERS:
        assert marker not in argv[-1]


def test_transfer_plane_never_imports_engine_or_activation():
    """The transfer-plane module must not route data ops through the ssh engine
    or the control-plane activation seam (grep the source — a call would red)."""
    import inspect

    src = inspect.getsource(_pull)
    assert "remote_activation_for_sidecar" not in src
    assert "engine_ssh_run" not in src
    assert "engine_enabled" not in src


# --- integration: the REAL remote command strings against a local tree --------

_needs_posix_shell = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None or shutil.which("tar") is None,
    reason="executes the remote-side POSIX find|tar|base64 commands locally",
)


def _run_remote_to_local(remote_cmd: str, dest: Path, codec_flag: str | None) -> None:
    """Execute *remote_cmd* via ``sh`` (the archive to stdout) and extract it
    locally with ``tar x`` into *dest* — end-to-end proof the command strings
    round-trip real bytes."""
    dest.mkdir(parents=True, exist_ok=True)
    ssh = subprocess.Popen(["sh", "-c", remote_cmd], stdout=subprocess.PIPE)
    assert ssh.stdout is not None
    tar_x = ["tar", "x"]
    if codec_flag:
        tar_x.append(codec_flag)
    tar_x += ["-f", "-", "-C", str(dest)]
    extract = subprocess.run(tar_x, stdin=ssh.stdout, capture_output=True, timeout=30)
    ssh.stdout.close()
    ssh.wait(timeout=30)
    assert extract.returncode == 0, extract.stderr


@_needs_posix_shell
def test_integration_batch_cmd_pulls_exact_members(tmp_path):
    remote = tmp_path / "remote"
    for rel in ("a.json", "sub/b.json", "skip.csv"):
        f = remote / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"content-{rel}")
    cmd = _pull._batch_remote_cmd(str(remote), ["a.json", "sub/b.json"], "-z")
    dest = tmp_path / "local"
    _run_remote_to_local(cmd, dest, "-z")
    assert (dest / "a.json").read_text() == "content-a.json"
    assert (dest / "sub" / "b.json").read_text() == "content-sub/b.json"
    assert not (dest / "skip.csv").exists()  # not in the batch


@_needs_posix_shell
def test_integration_fallback_cmd_filters_server_side(tmp_path):
    remote = tmp_path / "remote"
    for rel in ("t0/metrics.json", "t0/big.csv", "t1/metrics.json"):
        f = remote / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"data-{rel}")
    cmd = _pull._fallback_remote_cmd(str(remote), ["metrics.json"], None, "-z")
    dest = tmp_path / "local"
    _run_remote_to_local(cmd, dest, "-z")
    # Only the metrics.json files crossed (the 1000x lever): the CSV stayed put.
    assert (dest / "t0" / "metrics.json").is_file()
    assert (dest / "t1" / "metrics.json").is_file()
    assert not (dest / "t0" / "big.csv").exists()


@_needs_posix_shell
def test_integration_full_engine_end_to_end(tmp_path, monkeypatch):
    """Drive tar_ssh_pull with ssh replaced by a local ``sh`` exec, so the real
    manifest snippet, delta, and batched transfer run against on-disk trees."""
    remote = tmp_path / "remote"
    for rel in ("m0/metrics.json", "m0/trace.csv", "m1/metrics.json"):
        f = remote / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"payload-{rel}")
    local = tmp_path / "local"

    # Replace the two ssh seams with local ``sh`` execs so the REAL manifest
    # snippet, delta, and batched transfer run against on-disk trees.
    def fake_capture(ssh_target, remote_cmd, *, timeout, what):
        return subprocess.run(
            ["sh", "-c", remote_cmd], capture_output=True, text=True, timeout=timeout
        )

    def fake_transfer(*, ssh_target, remote_cmd, local_path, codec_flag, total_bytes, timeout):
        _run_remote_to_local(remote_cmd, Path(local_path), codec_flag)
        return _ok()

    monkeypatch.setattr(_pull, "_ssh_capture", fake_capture)
    monkeypatch.setattr(_pull, "_pull_transfer_with_retry", fake_transfer)

    result = _pull.tar_ssh_pull(
        ssh_target="u@h",
        remote_path=str(remote),
        local_path=local,
        include_globs=["metrics.json"],
    )
    assert result.ok
    assert result.files_pulled == 2  # two metrics.json, the CSV filtered out
    assert result.skipped_unchanged == 0  # fresh local dir
    assert (local / "m0" / "metrics.json").read_text() == "payload-m0/metrics.json"
    assert (local / "m1" / "metrics.json").is_file()
    assert not (local / "m0" / "trace.csv").exists()

    # Second call: everything already identical locally -> zero pull, all skipped.
    result2 = _pull.tar_ssh_pull(
        ssh_target="u@h",
        remote_path=str(remote),
        local_path=local,
        include_globs=["metrics.json"],
    )
    assert result2.ok
    assert result2.files_pulled == 0
    assert result2.skipped_unchanged == 2  # the resumability/delta invariant


# ─── F7 verify-during-build (unit 2.4b), pull side: the PULL engine bypasses the
# ssh engine and is preamble-free ──────────────────────────────────────────────


def test_pull_remote_commands_are_preamble_free() -> None:
    """E1 byte-equality (pull side): the pull engine's remote command builders emit
    raw ``tar c`` / ``find | tar c`` shell — no ``module load`` / ``conda
    activate`` / ``source`` control-plane preamble and no ``HPC_AGENT_OP=``/
    ``timeout -k`` ssh_run wrapper. The pull never routes through
    ``remote_activation_for_sidecar`` or ``build_remote_command``."""
    forbidden = ("module load", "conda activate", "source ", "HPC_AGENT_OP=", "timeout -k")
    batch_cmd = _pull._batch_remote_cmd("/r", ["m0/metrics.json", "m1/metrics.json"], "z")
    fallback_cmd = _pull._fallback_remote_cmd("/r", ["metrics.json"], [], "z")
    manifest_cmd_probe: list[str] = []

    # The manifest round-trip's remote command flows through _ssh_capture; capture
    # it via a run_capture_bounded spy so its raw shape is asserted too.
    def _spy(cmd, *_a, **_kw):
        manifest_cmd_probe.append(str(cmd[-1]))
        return _ok(stdout='{"files": []}')

    with patch("hpc_agent.infra.transport._pull.run_capture_bounded", side_effect=_spy):
        _pull._remote_pull_manifest(
            ssh_target="u@h", remote_path="/r", include_globs=[], exclude=[], timeout=60
        )

    for cmd in (batch_cmd, fallback_cmd, *manifest_cmd_probe):
        for token in forbidden:
            assert token not in cmd, f"pull command acquired {token!r}: {cmd!r}"
    assert "tar c" in batch_cmd  # raw archive, nothing wrapped around it
    assert "find" in fallback_cmd and "tar c" in fallback_cmd


def test_pull_transfer_drives_bounded_runner_not_ssh_run(tmp_path: Path) -> None:
    """Row 9 (engine-seam laws extend, pull side): the ssh->tar pull's SINK is
    ``run_capture_bounded`` (the one-shot tree-kill runner). ``ssh_run`` is the
    ONLY seam that consults the asyncssh engine, and the ``_pull`` module never
    even imports it — so the pull can never route through the engine. The ssh
    SOURCE is a bounded ``subprocess.Popen`` reaped on the deadline (the
    ``_pull_transfer`` exemption). A regression that pulled ``ssh_run`` into the
    pull engine (re-arming the engine gate) would trip the hasattr assertion."""
    import io
    import os

    # The engine-gated seam is structurally absent from the pull module.
    assert not hasattr(_pull, "ssh_run"), "pull engine must not import ssh_run (the engine gate)"

    calls: list[str] = []

    def _rec_bounded(cmd, *_a, **kw):
        calls.append(str(cmd[0]))
        # Drain the pump's read end so the pump thread can complete (the sink
        # normally consumes ssh's piped archive bytes).
        stdin = kw.get("stdin")
        if isinstance(stdin, int):
            while os.read(stdin, 65536):
                pass
        return _ok()

    with (
        patch("hpc_agent.infra.transport._pull.run_capture_bounded", side_effect=_rec_bounded),
        patch("hpc_agent.infra.transport._pull.subprocess.Popen") as popen_mock,
    ):
        ssh_proc = popen_mock.return_value
        ssh_proc.stdout = io.BytesIO(b"")  # empty archive stream
        ssh_proc.stderr = MagicMock()
        ssh_proc.stderr.read.return_value = b""
        ssh_proc.returncode = 0
        ssh_proc.wait.return_value = 0
        _pull._pull_transfer(
            ssh_target="u@h",
            remote_cmd="tar c -C /r -T - -f -",
            local_path=tmp_path,
            codec_flag=None,
            total_bytes=0,
            timeout=60,
        )
    assert any("tar" in c for c in calls)  # the local tar x sink ran on the bounded runner
