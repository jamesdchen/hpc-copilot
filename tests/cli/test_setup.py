"""Tests for ``hpc-agent setup`` — install assets, optionally probe a cluster.

The ``--cluster <name>`` option grew out of the preflight-as-setup
migration: environment-authority work (SSH agent, cluster reachability)
moved out of a runtime skill into one-time setup. These tests pin the
contract — envelope shape, and (#F31) a RED probe exiting cluster-error /
``ok:false`` so a scripted bootstrap sees the failure (the dead 24h cache
marker its predecessor wrote, and the nonexistent "Step 6b gate" that
supposedly read it, were removed).
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ``~/.claude/`` and the journal root under *tmp_path*."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run_setup(
    isolated_dirs: Path,
    *,
    cluster: str | None = None,
    dry_run: bool = False,
    experiment_dir: str | None = None,
    expect_rc: int = 0,
) -> dict:
    """Invoke ``cmd_setup`` in-process; return the parsed envelope."""
    from hpc_agent.cli.setup import cmd_setup

    args = argparse.Namespace(
        claude_dir=str(isolated_dirs / "claude"),
        dry_run=dry_run,
        cluster=cluster,
        experiment_dir=experiment_dir,
    )
    buf = io.StringIO()
    with mock.patch("sys.stdout", buf):
        rc = cmd_setup(args)
    assert rc == expect_rc
    lines = [line for line in buf.getvalue().strip().splitlines() if line.strip()]
    assert len(lines) == 1
    return json.loads(lines[0])


def test_setup_without_cluster_installs_assets_only(isolated_dirs: Path) -> None:
    """Vanilla ``setup`` installs assets — no preflight key, no marker."""
    env = _run_setup(isolated_dirs)
    assert env["ok"] is True
    assert "assets" in env["data"]
    assert "preflight" not in env["data"]
    assert "preflight_marker" not in env["data"]


def test_setup_with_cluster_green_returns_verdict_ok(isolated_dirs: Path) -> None:
    """A green probe exits 0 / ok:true and reports the verdict (no dead marker)."""
    with mock.patch(
        "hpc_agent.ops.preflight.check.check_preflight",
        return_value={"all_ok": True, "checks": []},
    ):
        env = _run_setup(isolated_dirs, cluster="hoffman2")

    assert env["ok"] is True
    assert env["data"]["preflight"] == {"all_ok": True, "checks": []}
    # The 24h cache marker (and its nonexistent Step 6b consumer) were removed.
    assert "preflight_marker" not in env["data"]


def test_setup_with_cluster_red_exits_cluster_error(isolated_dirs: Path) -> None:
    """#F31 FIRE PATH: a failing probe exits cluster-error (2) with ok:false.

    Before the fix ``setup --cluster`` emitted ok:true / exit 0 regardless of
    ``preflight['all_ok']``, so a scripted bootstrap proceeded over a broken
    env. Now the red probe drives the exit-error mapping — the guard that
    previously could not fire.
    """
    failures = {
        "all_ok": False,
        "checks": [{"name": "ssh_auth_sock", "ok": False, "detail": "unset"}],
    }
    with mock.patch(
        "hpc_agent.ops.preflight.check.check_preflight",
        return_value=failures,
    ):
        env = _run_setup(isolated_dirs, cluster="hoffman2", expect_rc=2)

    assert env["ok"] is False
    assert env["error_code"] == "remote_command_failed"
    assert env["category"] == "cluster"
    # The failing checks ride in failure_features so the ok:false envelope is
    # as inspectable as the ok:true one.
    checks = env["failure_features"]["checks"]
    assert {c["name"]: c["ok"] for c in checks} == {"ssh_auth_sock": False}
    assert "ssh_auth_sock" in env["message"]


def test_setup_with_cluster_dry_run_green_exits_ok(isolated_dirs: Path) -> None:
    """``--dry-run`` reports the probe outcome; a green verdict exits 0."""
    with mock.patch(
        "hpc_agent.ops.preflight.check.check_preflight",
        return_value={"all_ok": True, "checks": []},
    ):
        env = _run_setup(isolated_dirs, cluster="hoffman2", dry_run=True)

    assert env["ok"] is True
    assert env["data"]["preflight"]["all_ok"] is True
    assert "preflight_marker" not in env["data"]
