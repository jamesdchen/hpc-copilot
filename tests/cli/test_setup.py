"""Tests for ``hpc-agent setup`` — install assets, optionally probe a cluster.

The ``--cluster <name>`` option grew out of the preflight-as-setup
migration: environment-authority work (SSH agent, cluster reachability)
moved out of a runtime skill into one-time setup, and setup writes the
24h cache marker that ``/submit-hpc``'s Step 6b gate reads. These tests
pin the contract — envelope shape, marker writing on green, no marker on
red or dry-run.
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
) -> dict:
    """Invoke ``cmd_setup`` in-process; return the parsed envelope."""
    from hpc_agent.agent_cli import cmd_setup

    args = argparse.Namespace(
        claude_dir=str(isolated_dirs / "claude"),
        dry_run=dry_run,
        cluster=cluster,
        experiment_dir=experiment_dir,
    )
    buf = io.StringIO()
    with mock.patch("sys.stdout", buf):
        rc = cmd_setup(args)
    assert rc == 0
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


def test_setup_with_cluster_green_writes_marker(isolated_dirs: Path) -> None:
    """A green probe writes the cache marker the Step 6b gate reads."""
    with mock.patch(
        "hpc_agent.atoms.preflight.check_preflight",
        return_value={"all_ok": True, "checks": []},
    ):
        env = _run_setup(isolated_dirs, cluster="hoffman2")

    assert env["data"]["preflight"] == {"all_ok": True, "checks": []}
    marker_path = Path(env["data"]["preflight_marker"])
    assert marker_path.exists()
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    assert payload["all_ok"] is True
    assert payload["cluster"] == "hoffman2"
    assert "checked_at" in payload


def test_setup_with_cluster_red_skips_marker(isolated_dirs: Path) -> None:
    """A failing probe surfaces failures and does NOT write the marker."""
    failures = {
        "all_ok": False,
        "checks": [{"name": "ssh_auth_sock", "ok": False, "detail": "unset"}],
    }
    with mock.patch(
        "hpc_agent.atoms.preflight.check_preflight",
        return_value=failures,
    ):
        env = _run_setup(isolated_dirs, cluster="hoffman2")

    assert env["data"]["preflight"] == failures
    assert "preflight_marker" not in env["data"]
    # The journal dir may or may not exist; the marker file itself must not.
    from hpc_agent._internal.layout import JournalLayout

    expected = JournalLayout(isolated_dirs).preflight_marker("hoffman2")
    assert not expected.exists()


def test_setup_with_cluster_dry_run_skips_marker(isolated_dirs: Path) -> None:
    """``--dry-run`` reports the probe outcome but writes no marker."""
    with mock.patch(
        "hpc_agent.atoms.preflight.check_preflight",
        return_value={"all_ok": True, "checks": []},
    ):
        env = _run_setup(isolated_dirs, cluster="hoffman2", dry_run=True)

    assert env["data"]["preflight"]["all_ok"] is True
    assert "preflight_marker" not in env["data"]


def test_setup_experiment_dir_scopes_marker(isolated_dirs: Path, tmp_path: Path) -> None:
    """``--experiment-dir`` controls which journal receives the marker."""
    other = tmp_path / "other_experiment"
    other.mkdir()
    with mock.patch(
        "hpc_agent.atoms.preflight.check_preflight",
        return_value={"all_ok": True, "checks": []},
    ):
        env = _run_setup(isolated_dirs, cluster="hoffman2", experiment_dir=str(other))

    marker_path = Path(env["data"]["preflight_marker"])
    # Marker hash derives from *other*, not cwd.
    from hpc_agent._internal.layout import JournalLayout

    expected = JournalLayout(other).preflight_marker("hoffman2")
    assert marker_path == expected
    assert marker_path.exists()
