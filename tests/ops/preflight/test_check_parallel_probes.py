"""The two cluster-side ssh probes fan concurrently (#289).

``check-preflight --cluster X --spec <uv-spec>`` (submit.md Step 7) fires
TWO independent ssh round-trips: the #275 ``runtime_uv`` probe and the
functional ``cluster_ssh_echo`` round-trip. They are independent, so they
run CONCURRENTLY instead of stacking two RTTs. The concurrency is pinned
with a ``threading.Barrier(2)``: if the probes ran sequentially the first
``barrier.wait()`` would block until timeout and raise, failing the test.
"""

from __future__ import annotations

import contextlib
import threading
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
    # tcp:22 probe always "open" so cluster_ssh_echo is reached.
    monkeypatch.setattr(
        preflight.socket, "create_connection", lambda *a, **k: contextlib.nullcontext()
    )


def test_uv_and_ssh_echo_probes_run_concurrently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, green_local_env: None
) -> None:
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(_write_clusters(tmp_path)))
    barrier = threading.Barrier(2, timeout=5)

    def _uv(ssh_target: str, *, job_env: dict[str, str], skip: bool) -> None:
        barrier.wait()  # releases only if the echo probe is ALSO running
        return None

    def _ssh_run(cmd: str, *, ssh_target: str, timeout: int) -> Any:
        barrier.wait()
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(preflight, "runtime_uv_preflight", _uv)
    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", _ssh_run)

    spec = {"ssh_target": "u@h.test.edu", "job_env": {"HPC_RUNTIME": "uv"}}
    result = preflight.check_preflight(cluster="x", spec=spec)

    checks = {c["name"]: c for c in result["checks"]}
    assert checks["runtime_uv"]["ok"] is True
    assert checks["cluster_ssh_echo"]["ok"] is True


def test_standalone_uv_probe_without_cluster(
    monkeypatch: pytest.MonkeyPatch, green_local_env: None
) -> None:
    # No --cluster → the uv probe can't fan, but still runs standalone (#289).
    monkeypatch.setattr(preflight, "runtime_uv_preflight", lambda *a, **k: None)
    spec = {"ssh_target": "u@h", "job_env": {"HPC_RUNTIME": "uv"}}
    checks = {c["name"]: c for c in preflight.check_preflight(spec=spec)["checks"]}
    assert checks["runtime_uv"]["ok"] is True
    assert "cluster_ssh_echo" not in checks


def test_cluster_echo_without_uv_spec_is_unfanned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, green_local_env: None
) -> None:
    # --cluster but a non-uv spec → cluster_ssh_echo runs, no runtime_uv probe.
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(_write_clusters(tmp_path)))
    monkeypatch.setattr(
        "hpc_agent.infra.remote.ssh_run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="ok\n", stderr=""),
    )
    checks = {c["name"]: c for c in preflight.check_preflight(cluster="x", spec=None)["checks"]}
    assert checks["cluster_ssh_echo"]["ok"] is True
    assert "runtime_uv" not in checks
