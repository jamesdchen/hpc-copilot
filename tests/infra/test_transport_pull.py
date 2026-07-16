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
from unittest.mock import patch

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


def test_local_present_manifest_reuses_cache(tmp_path, monkeypatch):
    (tmp_path / "f.txt").write_text("content")
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
