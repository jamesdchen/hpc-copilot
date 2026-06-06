"""The two cluster-side ssh probes share ONE ssh connection (#295 Fix 2).

``check-preflight --cluster X --spec <uv-spec>`` (submit.md Step 7) used to fire
TWO independent ssh round-trips: the #275 ``runtime_uv`` probe and the functional
``cluster_ssh_echo`` round-trip. #289 fanned them concurrently (one RTT
wall-clock); #295 Fix 2 collapses both into a single multi-command ssh
connection, so a host with broken ControlMaster multiplexing pays one cold
handshake instead of two. These tests pin that exactly one ssh_run carries both
legs, that it routes through the spec's ssh_target, and that the
env-configurable timeout (#295 Fix 1) is honoured.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hpc_agent.ops.preflight import check as preflight


def _write_clusters(tmp_path: Path) -> Path:
    p = tmp_path / "clusters.yaml"
    p.write_text(
        "x:\n  host: h.test.edu\n  user: u\n  scheduler: slurm\n  scratch: /s\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def green_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the local-env checks pass without spawning ssh-add (Windows agent path)."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(preflight, "agent_available", lambda: True)
    monkeypatch.setattr(preflight, "agent_detail", lambda: "agent ok")
    # tcp:22 probe always "open" so the cluster ssh probe is reached.
    monkeypatch.setattr(
        preflight.socket, "create_connection", lambda *a, **k: contextlib.nullcontext()
    )


def test_uv_and_ssh_echo_probes_share_one_ssh_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, green_local_env: None
) -> None:
    """#295 Fix 2: echo + runtime_uv ride ONE ssh round-trip (was a concurrent
    two-connection fan). Pin exactly one ssh_run, both checks produced, and the
    connection routed through the spec's ssh_target (the production endpoint),
    not the bare clusters.yaml host."""
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(_write_clusters(tmp_path)))
    calls: list[tuple[str, str, Any]] = []

    def _ssh_run(cmd: str, *, ssh_target: str, timeout: Any) -> Any:
        calls.append((cmd, ssh_target, timeout))
        # One connection carries both legs; emit both sentinel tokens (with
        # activation noise interleaved to prove the parser is presence-based).
        return SimpleNamespace(
            returncode=0,
            stdout="__HPC_ECHO_OK__\nLoading module cuda...\n/usr/bin/uv\n__HPC_UV_OK__\n",
            stderr="",
        )

    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", _ssh_run)

    spec = {"ssh_target": "u@h.test.edu", "job_env": {"HPC_RUNTIME": "uv"}}
    result = preflight.check_preflight(cluster="x", spec=spec)

    checks = {c["name"]: c for c in result["checks"]}
    assert checks["cluster_ssh_echo"]["ok"] is True
    assert checks["runtime_uv"]["ok"] is True
    assert len(calls) == 1, "both probes must share exactly one ssh connection"
    assert calls[0][1] == "u@h.test.edu", "merged probe routes through the spec ssh_target"


def test_merged_probe_reports_uv_missing_without_extra_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, green_local_env: None
) -> None:
    """When uv is absent the merged probe still fires exactly one connection:
    the echo leg passes (round-trip worked) and the uv leg fails with the
    actionable remediation."""
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(_write_clusters(tmp_path)))
    calls: list[str] = []

    def _ssh_run(cmd: str, *, ssh_target: str, timeout: Any) -> Any:
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0, stdout="__HPC_ECHO_OK__\n__HPC_UV_MISSING__\n", stderr=""
        )

    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", _ssh_run)
    spec = {"ssh_target": "u@h.test.edu", "job_env": {"HPC_RUNTIME": "uv", "CONDA_ENV": "ml"}}
    result = preflight.check_preflight(cluster="x", spec=spec)

    checks = {c["name"]: c for c in result["checks"]}
    assert checks["cluster_ssh_echo"]["ok"] is True
    assert checks["runtime_uv"]["ok"] is False
    assert "uv` was not found on PATH" in checks["runtime_uv"]["detail"]
    assert len(calls) == 1


def test_cluster_ssh_timeout_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, green_local_env: None
) -> None:
    """#295 Fix 1: HPC_CLUSTER_SSH_TIMEOUT overrides the per-probe ssh timeout."""
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(_write_clusters(tmp_path)))
    monkeypatch.setenv("HPC_CLUSTER_SSH_TIMEOUT", "30")
    seen: dict[str, Any] = {}

    def _ssh_run(cmd: str, *, ssh_target: str, timeout: Any) -> Any:
        seen["timeout"] = timeout
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", _ssh_run)
    # non-uv spec → the echo-only path runs _cluster_ssh_echo_check with the env timeout.
    preflight.check_preflight(cluster="x", spec=None)
    assert seen["timeout"] == 30


def test_cluster_ssh_timeout_defaults_to_15(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, green_local_env: None
) -> None:
    """Default per-probe timeout is 15s (up from the old hardcoded 5s)."""
    monkeypatch.delenv("HPC_CLUSTER_SSH_TIMEOUT", raising=False)
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(_write_clusters(tmp_path)))
    seen: dict[str, Any] = {}

    def _ssh_run(cmd: str, *, ssh_target: str, timeout: Any) -> Any:
        seen["timeout"] = timeout
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", _ssh_run)
    preflight.check_preflight(cluster="x", spec=None)
    assert seen["timeout"] == 15


def test_cluster_ssh_timeout_non_integer_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-integer override degrades to the 15s default, not a crash."""
    monkeypatch.setenv("HPC_CLUSTER_SSH_TIMEOUT", "not-a-number")
    assert preflight._cluster_ssh_timeout() == 15


def test_standalone_uv_probe_without_cluster(
    monkeypatch: pytest.MonkeyPatch, green_local_env: None
) -> None:
    # No --cluster → the uv probe can't merge with an echo, but still runs standalone.
    monkeypatch.setattr(preflight, "runtime_uv_preflight", lambda *a, **k: None)
    spec = {"ssh_target": "u@h", "job_env": {"HPC_RUNTIME": "uv"}}
    checks = {c["name"]: c for c in preflight.check_preflight(spec=spec)["checks"]}
    assert checks["runtime_uv"]["ok"] is True
    assert "cluster_ssh_echo" not in checks


def test_cluster_echo_without_uv_spec_is_standalone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, green_local_env: None
) -> None:
    # --cluster but a non-uv spec → cluster_ssh_echo runs standalone, no runtime_uv probe.
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(_write_clusters(tmp_path)))
    monkeypatch.setattr(
        "hpc_agent.infra.remote.ssh_run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="ok\n", stderr=""),
    )
    checks = {c["name"]: c for c in preflight.check_preflight(cluster="x", spec=None)["checks"]}
    assert checks["cluster_ssh_echo"]["ok"] is True
    assert "runtime_uv" not in checks
