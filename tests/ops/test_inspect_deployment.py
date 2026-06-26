"""Tests for the ``inspect-deployment`` primitive.

Pins the read-only, throttled, scratch-confined contract:

* Resolves ``ssh_target`` + ``scratch`` from the cluster config.
* Derives ``REPO_DIR`` from a run's journaled ``remote_path`` (``--run-id``),
  or probes an explicit ``--path``.
* Confines the probe strictly under scratch (a path outside → ``spec_invalid``).
* Runs exactly ONE ``ssh_run`` call carrying a fixed read-only probe — no
  caller-supplied command string.
* A non-existent target is ``exists=False`` (not an error); an SSH transport
  failure is ``remote_command_failed``.

The cluster + journal + SSH surfaces are mocked — the verb's job is to assemble
a bounded probe and route it through the throttled seam, which is exactly what
these tests assert.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hpc_agent import errors
from hpc_agent.ops.inspect_deployment import _LINE_CAP, inspect_deployment

_CLUSTERS = {"disc": {"host": "login.disc.edu", "user": "jc", "scratch": "/scratch1/jc"}}


def _cp(stdout: str = "", rc: int = 0, stderr: str = "boom") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _wire(monkeypatch, *, ssh_handler, record=None, clusters=None) -> list[tuple[str, dict]]:
    """Patch the cluster config, journal, and SSH seam; return the ssh call log."""
    calls: list[tuple[str, dict]] = []

    def _ssh(cmd, **kw):
        calls.append((cmd, kw))
        return ssh_handler(cmd, **kw)

    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config",
        lambda: clusters if clusters is not None else _CLUSTERS,
    )
    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", _ssh)
    monkeypatch.setattr("hpc_agent.state.journal.load_run", lambda exp, rid: record)
    return calls


def test_run_id_derives_repo_dir_and_lists(monkeypatch, tmp_path: Path) -> None:
    record = SimpleNamespace(remote_path="/scratch1/jc/exp-abc/", cluster="disc")
    listing = "/scratch1/jc/exp-abc\n/scratch1/jc/exp-abc/results\n"
    calls = _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(listing), record=record)

    out = inspect_deployment(experiment_dir=tmp_path, cluster="disc", run_id="exp-abc", depth=2)

    assert out["exists"] is True
    assert out["repo_dir"] == "/scratch1/jc/exp-abc"  # trailing slash stripped
    assert out["path"] == "/scratch1/jc/exp-abc"
    assert out["run_id"] == "exp-abc"
    assert out["ssh_target"] == "jc@login.disc.edu"
    assert out["entries"] == ["/scratch1/jc/exp-abc", "/scratch1/jc/exp-abc/results"]
    assert out["entry_count"] == 2
    assert out["truncated"] is False
    # Exactly one throttled connection; the probe is read-only and bounded.
    assert len(calls) == 1
    cmd, kw = calls[0]
    assert kw["ssh_target"] == "jc@login.disc.edu"
    # Fixed read-only probe over the quoted target; depth interpolated.
    assert "find" in cmd and "-maxdepth 2" in cmd
    assert "/scratch1/jc/exp-abc" in cmd  # the (shell-quoted) target path
    assert "rm " not in cmd and " > " not in cmd  # no write/destructive affordance


def test_explicit_path_under_scratch_lists(monkeypatch, tmp_path: Path) -> None:
    calls = _wire(
        monkeypatch,
        ssh_handler=lambda c, **k: _cp("/scratch1/jc/other\n"),
        record=None,
    )
    out = inspect_deployment(experiment_dir=tmp_path, cluster="disc", path="/scratch1/jc/other")
    assert out["exists"] is True
    assert out["path"] == "/scratch1/jc/other"
    assert out["repo_dir"] is None and out["run_id"] is None
    assert out["depth"] == 1
    assert len(calls) == 1


def test_path_outside_scratch_is_spec_invalid(monkeypatch, tmp_path: Path) -> None:
    calls = _wire(monkeypatch, ssh_handler=lambda c, **k: _cp("nope"))
    with pytest.raises(errors.SpecInvalid):
        inspect_deployment(experiment_dir=tmp_path, cluster="disc", path="/etc/passwd")
    assert calls == []  # never probed


def test_scratch_root_itself_is_refused(monkeypatch, tmp_path: Path) -> None:
    _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(""))
    with pytest.raises(errors.SpecInvalid):
        inspect_deployment(experiment_dir=tmp_path, cluster="disc", path="/scratch1/jc")


def test_missing_target_reports_not_exists(monkeypatch, tmp_path: Path) -> None:
    from hpc_agent.ops.inspect_deployment import _MISSING_SENTINEL

    _wire(
        monkeypatch,
        ssh_handler=lambda c, **k: _cp(_MISSING_SENTINEL + "\n"),
    )
    out = inspect_deployment(experiment_dir=tmp_path, cluster="disc", path="/scratch1/jc/gone")
    assert out["exists"] is False
    assert out["entries"] == []
    assert out["entry_count"] == 0


def test_explicit_path_on_scratchless_cluster_is_refused(monkeypatch, tmp_path: Path) -> None:
    """A cluster with no scratch can't confine --path → refuse, never probe."""
    calls = _wire(
        monkeypatch,
        ssh_handler=lambda c, **k: _cp("anything"),
        clusters={"local": {"host": "h", "user": "u"}},  # no scratch
    )
    with pytest.raises(errors.SpecInvalid):
        inspect_deployment(experiment_dir=tmp_path, cluster="local", path="/anywhere/x")
    assert calls == []  # never probed


def test_run_id_on_scratchless_cluster_is_allowed(monkeypatch, tmp_path: Path) -> None:
    """--run-id is exempt: its target is the run's own journaled deploy path."""
    record = SimpleNamespace(remote_path="/data/run-7", cluster="local")
    calls = _wire(
        monkeypatch,
        ssh_handler=lambda c, **k: _cp("/data/run-7\n"),
        record=record,
        clusters={"local": {"host": "h", "user": "u"}},  # no scratch
    )
    out = inspect_deployment(experiment_dir=tmp_path, cluster="local", run_id="run-7")
    assert out["exists"] is True and out["path"] == "/data/run-7"
    assert len(calls) == 1


def test_neither_target_is_spec_invalid(monkeypatch, tmp_path: Path) -> None:
    _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(""))
    with pytest.raises(errors.SpecInvalid):
        inspect_deployment(experiment_dir=tmp_path, cluster="disc")


def test_both_targets_is_spec_invalid(monkeypatch, tmp_path: Path) -> None:
    _wire(
        monkeypatch,
        ssh_handler=lambda c, **k: _cp(""),
        record=SimpleNamespace(remote_path="/scratch1/jc/x"),
    )
    with pytest.raises(errors.SpecInvalid):
        inspect_deployment(
            experiment_dir=tmp_path, cluster="disc", run_id="x", path="/scratch1/jc/y"
        )


def test_unknown_run_id_is_spec_invalid(monkeypatch, tmp_path: Path) -> None:
    _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(""), record=None)
    with pytest.raises(errors.SpecInvalid):
        inspect_deployment(experiment_dir=tmp_path, cluster="disc", run_id="ghost")


def test_unknown_cluster_raises(monkeypatch, tmp_path: Path) -> None:
    _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(""))
    with pytest.raises(errors.ClusterUnknown):
        inspect_deployment(experiment_dir=tmp_path, cluster="nope", path="/scratch1/jc/x")


@pytest.mark.parametrize("depth", [0, -1, 5, 99])
def test_bad_depth_is_spec_invalid(monkeypatch, tmp_path: Path, depth: int) -> None:
    _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(""))
    with pytest.raises(errors.SpecInvalid):
        inspect_deployment(
            experiment_dir=tmp_path, cluster="disc", path="/scratch1/jc/x", depth=depth
        )


def test_ssh_transport_failure_raises(monkeypatch, tmp_path: Path) -> None:
    _wire(monkeypatch, ssh_handler=lambda c, **k: _cp("", rc=255, stderr="conn refused"))
    with pytest.raises(errors.RemoteCommandFailed):
        inspect_deployment(experiment_dir=tmp_path, cluster="disc", path="/scratch1/jc/x")


def test_listing_at_cap_is_truncated(monkeypatch, tmp_path: Path) -> None:
    listing = "\n".join(f"/scratch1/jc/x/f{i}" for i in range(_LINE_CAP)) + "\n"
    _wire(monkeypatch, ssh_handler=lambda c, **k: _cp(listing))
    out = inspect_deployment(experiment_dir=tmp_path, cluster="disc", path="/scratch1/jc/x")
    assert out["entry_count"] == _LINE_CAP
    assert out["truncated"] is True
