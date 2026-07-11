"""Tests for the ``dir-digest`` query verb (run-#11 mechanization).

Pins the BOUNDED digest that replaces raw ``ls``/``find``: counts, total size,
newest-N, extension histogram, opt-in marker scan — local (first-class) and
remote (mocked ssh seam, the inspect-deployment discipline). Also pins the
containment refusal and the boundedness invariant on a 1000-file tree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.dir_digest import DirDigestSpec
from hpc_agent.ops.dir_digest import (
    _MISSING_SENTINEL,
    _SEC_COUNTS,
    _SEC_HIST,
    _SEC_MARKERS,
    _SEC_NEWEST,
    dir_digest,
)
from hpc_agent.ops.worker_log_digest import KNOWN_MARKERS

_MARKED_LOG = """\
[dispatch] task_id=0 run_id=r
engine connect [throttle]: TimeoutError
[dispatch] WARN: ignoring flag
[dispatch] ERROR: env var not set
[dispatch] FAILED (exit 1)
[dispatch] FATAL: could not clean WIP
another [throttle] retry line
plain line
"""


def _build_tree(root: Path) -> None:
    """A small nested tree: files with several extensions + two marked logs."""
    (root / "train.py").write_text("print(1)\n", encoding="utf-8")
    (root / "config.yaml").write_text("a: 1\n", encoding="utf-8")
    (root / "README").write_text("no ext\n", encoding="utf-8")
    results = root / "results"
    results.mkdir()
    for i in range(3):
        (results / f"shard_{i}.json").write_text("{}\n", encoding="utf-8")
    logs = root / "logs"
    logs.mkdir()
    (logs / "worker.log").write_text(_MARKED_LOG, encoding="utf-8")
    (logs / "task.err").write_text("engine connect [throttle]: boom\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Local arm                                                                    #
# --------------------------------------------------------------------------- #


def test_local_counts_size_and_histogram(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    result = dir_digest(experiment_dir=tmp_path, spec=DirDigestSpec(path="."))

    assert result.scope == "local"
    assert result.exists is True and result.readable is True
    # 3 top files + 3 shards + 2 logs = 8 files; 2 subdirs (results, logs).
    assert result.file_count == 8
    assert result.dir_count == 2
    assert result.total_size_bytes > 0

    hist = {b.name: b.count for b in result.histogram}
    assert hist[".json"] == 3
    assert hist[".py"] == 1
    assert hist[".yaml"] == 1
    assert hist[".log"] == 1
    assert hist[".err"] == 1
    assert hist["(noext)"] == 1  # README


def test_local_newest_is_bounded_and_ordered(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    # Stagger mtimes so newest ordering is unambiguous.
    for i in range(5):
        f = d / f"f{i}.txt"
        f.write_text("x\n", encoding="utf-8")
        os.utime(f, (1000 + i, 1000 + i))
    result = dir_digest(experiment_dir=tmp_path, spec=DirDigestSpec(path="d", newest=2))

    assert result.newest_requested == 2
    assert len(result.newest) == 2
    assert [e.relpath for e in result.newest] == ["f4.txt", "f3.txt"]
    assert result.newest[0].mtime >= result.newest[1].mtime


def test_local_newest_zero_omits_list_but_counts(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    result = dir_digest(experiment_dir=tmp_path, spec=DirDigestSpec(path=".", newest=0))
    assert result.newest == []
    assert result.file_count == 8
    assert "no entry list requested" in result.render


def test_local_marker_scan_counts_across_log_and_err(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    result = dir_digest(experiment_dir=tmp_path, spec=DirDigestSpec(path="."))
    assert result.marker_scan is True
    assert result.files_scanned_for_markers == 2  # worker.log + task.err
    # [throttle]: 2 in the .log + 1 in the .err = 3; others once each in the .log.
    assert result.marker_counts["[throttle]"] == 3
    assert result.marker_counts["[dispatch] WARN"] == 1
    assert result.marker_counts["[dispatch] ERROR"] == 1
    assert result.marker_counts["[dispatch] FAILED"] == 1
    assert result.marker_counts["[dispatch] FATAL"] == 1
    assert set(result.marker_counts) == set(KNOWN_MARKERS)


def test_local_marker_scan_disabled(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    result = dir_digest(experiment_dir=tmp_path, spec=DirDigestSpec(path=".", marker_scan=False))
    assert result.marker_scan is False
    assert result.marker_counts == {}
    assert result.files_scanned_for_markers == 0
    assert "no marker scan requested" in result.render


def test_local_missing_dir_fails_open(tmp_path: Path) -> None:
    result = dir_digest(experiment_dir=tmp_path, spec=DirDigestSpec(path="nope"))
    assert result.exists is False and result.readable is False
    assert result.error is not None and "no such directory" in result.error
    assert result.file_count == 0
    assert "unreadable" in result.render


def test_local_path_to_file_not_dir_fails_open(tmp_path: Path) -> None:
    (tmp_path / "afile").write_text("x\n", encoding="utf-8")
    result = dir_digest(experiment_dir=tmp_path, spec=DirDigestSpec(path="afile"))
    assert result.readable is False
    assert result.error is not None and "not a directory" in result.error


def test_local_absolute_path_within_experiment_dir_accepted(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    result = dir_digest(
        experiment_dir=tmp_path, spec=DirDigestSpec(path=str((tmp_path / "results").resolve()))
    )
    assert result.readable is True
    assert result.file_count == 3


def test_local_path_escaping_experiment_dir_is_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        dir_digest(experiment_dir=tmp_path, spec=DirDigestSpec(path="../../etc"))


def test_local_deterministic(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    a = dir_digest(experiment_dir=tmp_path, spec=DirDigestSpec(path="."))
    b = dir_digest(experiment_dir=tmp_path, spec=DirDigestSpec(path="."))
    assert a.model_dump() == b.model_dump()


def test_local_result_bounded_on_1000_file_tree(tmp_path: Path) -> None:
    big = tmp_path / "big"
    big.mkdir()
    for i in range(1000):
        (big / f"f{i}.dat").write_text("y\n", encoding="utf-8")
    result = dir_digest(experiment_dir=tmp_path, spec=DirDigestSpec(path="big", newest=10))

    assert result.file_count == 1000  # counted
    # ...but the payload stays bounded regardless of tree size:
    assert len(result.newest) == 10
    assert len(result.histogram) <= 10
    assert len(result.marker_counts) == len(KNOWN_MARKERS)
    # The render carries no per-file listing — its size is bounded, not O(files).
    assert result.render.count("\n") < 60
    assert "f500.dat" not in result.render


# --------------------------------------------------------------------------- #
# Remote arm (ssh seam mocked — the inspect-deployment discipline)            #
# --------------------------------------------------------------------------- #

_CLUSTERS = {"disc": {"host": "login.disc.edu", "user": "jc", "scratch": "/scratch1/jc"}}


def _cp(stdout: str = "", rc: int = 0, stderr: str = "boom") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _wire(monkeypatch, *, ssh_handler, clusters=None) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    def _ssh(cmd, **kw):
        calls.append((cmd, kw))
        return ssh_handler(cmd, **kw)

    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: clusters if clusters is not None else _CLUSTERS,
    )
    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", _ssh)
    return calls


def _remote_stdout(*, with_markers: bool = True) -> str:
    lines = [
        _SEC_COUNTS,
        "8\t2\t4096",
        _SEC_NEWEST,
        "1700000002.0\t120\tresults/shard_2.json",
        "1700000001.0\t64\tlogs/worker.log",
        _SEC_HIST,
        "3\t.json",
        "1\t.py",
        _SEC_MARKERS,
    ]
    if with_markers:
        lines.append("FILES\t2")
        lines.append("[throttle]\t3")
        lines.append("[dispatch] WARN\t1")
    return "\n".join(lines) + "\n"


def test_remote_parses_bounded_digest(monkeypatch, tmp_path: Path) -> None:
    _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(_remote_stdout()))
    result = dir_digest(
        experiment_dir=tmp_path,
        spec=DirDigestSpec(path="/scratch1/jc/exp-abc", cluster="disc", newest=10),
    )
    assert result.scope == "remote"
    assert result.cluster == "disc"
    assert result.exists is True and result.readable is True
    assert result.file_count == 8
    assert result.dir_count == 2
    assert result.total_size_bytes == 4096
    assert [e.relpath for e in result.newest] == ["results/shard_2.json", "logs/worker.log"]
    assert {b.name: b.count for b in result.histogram} == {".json": 3, ".py": 1}
    assert result.files_scanned_for_markers == 2
    assert result.marker_counts["[throttle]"] == 3
    assert result.marker_counts["[dispatch] WARN"] == 1
    assert result.marker_counts["[dispatch] FATAL"] == 0  # absent → 0, shape stable
    assert set(result.marker_counts) == set(KNOWN_MARKERS)


def test_remote_issues_one_readonly_bash_lc_call(monkeypatch, tmp_path: Path) -> None:
    calls = _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(_remote_stdout()))
    dir_digest(
        experiment_dir=tmp_path,
        spec=DirDigestSpec(path="/scratch1/jc/exp-abc", cluster="disc"),
    )
    assert len(calls) == 1
    cmd, kw = calls[0]
    assert kw["ssh_target"] == "jc@login.disc.edu"
    assert cmd.startswith("bash -lc ")
    assert "find" in cmd and "-printf" in cmd
    assert "/scratch1/jc/exp-abc" in cmd
    # Read-only: no write/destructive affordance in the composed command.
    assert "rm " not in cmd
    assert " > " not in cmd and ">>" not in cmd
    assert "rmdir" not in cmd and "mv " not in cmd


def test_remote_marker_scan_off_omits_marker_pipeline(monkeypatch, tmp_path: Path) -> None:
    calls = _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(_remote_stdout(with_markers=False)))
    result = dir_digest(
        experiment_dir=tmp_path,
        spec=DirDigestSpec(path="/scratch1/jc/exp-abc", cluster="disc", marker_scan=False),
    )
    assert result.marker_scan is False
    assert result.marker_counts == {}
    cmd = calls[0][0]
    assert "grep" not in cmd  # no marker grep composed when scan is off


def test_remote_missing_target_fails_open(monkeypatch, tmp_path: Path) -> None:
    _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(_MISSING_SENTINEL + "\n"))
    result = dir_digest(
        experiment_dir=tmp_path,
        spec=DirDigestSpec(path="/scratch1/jc/gone", cluster="disc"),
    )
    assert result.exists is False and result.readable is False
    assert result.error is not None and "no such directory" in result.error


def test_remote_missing_target_with_login_chatter_fails_open(monkeypatch, tmp_path: Path) -> None:
    """Login-shell (`bash -lc`) profile/module chatter can precede the sentinel;
    a missing tree must still read exists=False, not a phantom empty dir
    (bug-sweep #40)."""
    chatter = "Loading module foo/1.2\nconda activate base\n"
    _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(chatter + _MISSING_SENTINEL + "\n"))
    result = dir_digest(
        experiment_dir=tmp_path,
        spec=DirDigestSpec(path="/scratch1/jc/gone", cluster="disc"),
    )
    assert result.exists is False and result.readable is False
    assert result.error is not None and "no such directory" in result.error


def test_remote_transport_failure_raises(monkeypatch, tmp_path: Path) -> None:
    _wire(monkeypatch, ssh_handler=lambda c, **k: _cp("", rc=255, stderr="conn refused"))
    with pytest.raises(errors.RemoteCommandFailed):
        dir_digest(
            experiment_dir=tmp_path,
            spec=DirDigestSpec(path="/scratch1/jc/x", cluster="disc"),
        )


def test_remote_path_outside_scratch_is_spec_invalid(monkeypatch, tmp_path: Path) -> None:
    calls = _wire(monkeypatch, ssh_handler=lambda c, **k: _cp("nope"))
    with pytest.raises(errors.SpecInvalid):
        dir_digest(
            experiment_dir=tmp_path,
            spec=DirDigestSpec(path="/etc/passwd", cluster="disc"),
        )
    assert calls == []  # never probed


def test_remote_scratchless_cluster_is_refused(monkeypatch, tmp_path: Path) -> None:
    calls = _wire(
        monkeypatch,
        ssh_handler=lambda c, **k: _cp("anything"),
        clusters={"local": {"host": "h", "user": "u"}},  # no scratch
    )
    with pytest.raises(errors.SpecInvalid):
        dir_digest(
            experiment_dir=tmp_path,
            spec=DirDigestSpec(path="/anywhere/x", cluster="local"),
        )
    assert calls == []


def test_remote_unknown_cluster_raises(monkeypatch, tmp_path: Path) -> None:
    _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(""))
    with pytest.raises(errors.ClusterUnknown):
        dir_digest(
            experiment_dir=tmp_path,
            spec=DirDigestSpec(path="/scratch1/jc/x", cluster="nope"),
        )


def test_remote_garbled_stdout_degrades_not_crashes(monkeypatch, tmp_path: Path) -> None:
    # Only a counts header with junk — parser must degrade to zeros/empties.
    _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(_SEC_COUNTS + "\ngarbage\n"))
    result = dir_digest(
        experiment_dir=tmp_path,
        spec=DirDigestSpec(path="/scratch1/jc/x", cluster="disc"),
    )
    assert result.readable is True
    assert result.file_count == 0 and result.dir_count == 0
    assert result.newest == [] and result.histogram == []
    # marker_scan requested but section empty → all-zero, stable shape.
    assert set(result.marker_counts) == set(KNOWN_MARKERS)
    assert all(v == 0 for v in result.marker_counts.values())
