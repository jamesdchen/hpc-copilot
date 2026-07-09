"""Pure-API preflight transport gate (#337 Class B).

A pure-API backend (``requires_ssh=False``) has no login node, so
``check_preflight`` must issue ZERO ssh for it: no TCP :22 probe, no ``ssh
<host> echo ok`` round-trip, no merged uv probe. The cluster arm is replaced by
structured *skipped* checks (the existing ``_check`` builder) so the envelope
shape is preserved. The gate dispatches on the backend's ``requires_ssh``
capability via :func:`hpc_agent.infra.backends.backend_requires_ssh` — core
never branches on the scheduler name.

These tests booby-trap every SSH/TCP touchpoint so a single stray probe fails
the test, and prove a built-in SSH cluster still probes (the byte-for-byte
sanity arm).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from hpc_agent.infra import backends
from hpc_agent.infra.backends import HPCBackend
from hpc_agent.ops import submit_preflight as sp
from hpc_agent.ops.preflight import check as preflight


def _which_for(present: set[str]):
    from hpc_agent.infra import ssh_options

    resolved = {ssh_options._ssh_binary(): "ssh", ssh_options._scp_binary(): "scp"}

    def _which(binary: str) -> str | None:
        capability = resolved.get(binary, binary)
        return f"/usr/bin/{capability}" if capability in present else None

    return _which


def _boom_ssh(*a: Any, **k: Any) -> Any:
    raise AssertionError("pure-API preflight must not open an ssh round-trip")


def _boom_tcp(*a: Any, **k: Any) -> Any:
    raise AssertionError("pure-API preflight must not open a TCP :22 probe")


@pytest.fixture
def pure_api_backend():
    """Register a pure-API backend (``requires_ssh=False``) for the test."""

    @backends.register("fakepureapipreflight")
    class _FakePureApi(HPCBackend):  # pragma: no cover - never constructed here
        scheduler_name = "fakepureapipreflight"
        requires_ssh = False

        def _build_command(self, *a: object, **k: object) -> object:
            raise NotImplementedError

    try:
        yield "fakepureapipreflight"
    finally:
        backends._REGISTRY.pop("fakepureapipreflight", None)


def test_pure_api_cluster_preflight_issues_zero_ssh(
    monkeypatch: pytest.MonkeyPatch, pure_api_backend: str
) -> None:
    monkeypatch.setattr(
        preflight,
        "load_clusters_config",
        lambda: {"crowd": {"host": "ignored", "scheduler": pure_api_backend}},
    )
    with (
        mock.patch.object(preflight.shutil, "which", _which_for({"ssh", "rsync"})),
        mock.patch.object(preflight.socket, "create_connection", _boom_tcp),
        mock.patch("hpc_agent.infra.remote.ssh_run", _boom_ssh),
    ):
        result = preflight.check_preflight(cluster="crowd")

    checks = {c["name"]: c for c in result["checks"]}
    # The transport checks are present but structurally skipped — no SSH issued.
    assert checks["cluster_tcp_22"]["ok"] is True
    assert "pure-API" in checks["cluster_tcp_22"]["detail"]
    assert checks["cluster_ssh_echo"]["ok"] is True
    assert "pure-API" in checks["cluster_ssh_echo"]["detail"]


def test_pure_api_cluster_skips_merged_uv_probe(
    monkeypatch: pytest.MonkeyPatch, pure_api_backend: str
) -> None:
    # A runtime=uv spec would normally ride the cluster ssh round-trip; for a
    # pure-API cluster the uv probe must be skipped too (zero ssh), not run
    # standalone afterwards.
    monkeypatch.setattr(
        preflight,
        "load_clusters_config",
        lambda: {"crowd": {"host": "ignored", "scheduler": pure_api_backend}},
    )
    monkeypatch.setattr(preflight, "runtime_uv_preflight", _boom_ssh)
    spec = {
        "ssh_target": "user@crowd",
        "job_env": {"HPC_RUNTIME": "uv", "CONDA_ENV": "x"},
    }
    with (
        mock.patch.object(preflight.shutil, "which", _which_for({"ssh", "rsync"})),
        mock.patch.object(preflight.socket, "create_connection", _boom_tcp),
        mock.patch("hpc_agent.infra.remote.ssh_run", _boom_ssh),
    ):
        result = preflight.check_preflight(cluster="crowd", spec=spec)

    checks = {c["name"]: c for c in result["checks"]}
    assert checks["runtime_uv"]["ok"] is True
    assert "pure-API" in checks["runtime_uv"]["detail"]


def test_pure_api_cluster_still_gets_placeholder_check(
    monkeypatch: pytest.MonkeyPatch, pure_api_backend: str
) -> None:
    """``cluster_config_customized`` is purely local (no SSH), so skipping the
    transport probes must NOT skip it: a packaged ``<your_user>`` /
    ``<your_scratch>`` placeholder config on a pure-API backend fails
    preflight instead of surfacing later as a confusing submit failure."""
    monkeypatch.setattr(
        preflight,
        "load_clusters_config",
        lambda: {
            "crowd": {
                "host": "ignored",
                "scheduler": pure_api_backend,
                "user": "<your_user>",
                "scratch": "<your_scratch>",
            }
        },
    )
    with (
        mock.patch.object(preflight.shutil, "which", _which_for({"ssh", "rsync"})),
        mock.patch.object(preflight.socket, "create_connection", _boom_tcp),
        mock.patch("hpc_agent.infra.remote.ssh_run", _boom_ssh),
    ):
        result = preflight.check_preflight(cluster="crowd")

    checks = {c["name"]: c for c in result["checks"]}
    assert checks["cluster_config_customized"]["ok"] is False
    assert "<your_" in checks["cluster_config_customized"]["detail"]
    assert result["all_ok"] is False


def test_pure_api_cluster_customized_config_passes_placeholder_check(
    monkeypatch: pytest.MonkeyPatch, pure_api_backend: str
) -> None:
    monkeypatch.setattr(
        preflight,
        "load_clusters_config",
        lambda: {"crowd": {"host": "ignored", "scheduler": pure_api_backend, "user": "jdc"}},
    )
    with (
        mock.patch.object(preflight.shutil, "which", _which_for({"ssh", "rsync"})),
        mock.patch.object(preflight.socket, "create_connection", _boom_tcp),
        mock.patch("hpc_agent.infra.remote.ssh_run", _boom_ssh),
    ):
        result = preflight.check_preflight(cluster="crowd")

    checks = {c["name"]: c for c in result["checks"]}
    assert checks["cluster_config_customized"]["ok"] is True


def _tcp_ok() -> mock.MagicMock:
    cm = mock.MagicMock()
    cm.__enter__ = mock.MagicMock(return_value=cm)
    cm.__exit__ = mock.MagicMock(return_value=False)
    return cm


def test_ssh_cluster_still_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: a built-in SSH backend still opens TCP + ssh echo (unchanged)."""
    ssh_run = mock.MagicMock(return_value=SimpleNamespace(returncode=0, stdout="ok\n", stderr=""))
    monkeypatch.setattr(
        preflight,
        "load_clusters_config",
        lambda: {"hpc": {"host": "h", "scheduler": "sge", "user": "jdc", "conda_envs": ["e"]}},
    )
    with (
        mock.patch.object(preflight.shutil, "which", _which_for({"ssh", "rsync"})),
        mock.patch.object(preflight.socket, "create_connection", return_value=_tcp_ok()),
        mock.patch("hpc_agent.infra.remote.ssh_run", ssh_run),
    ):
        result = preflight.check_preflight(cluster="hpc")

    checks = {c["name"]: c for c in result["checks"]}
    assert checks["cluster_tcp_22"]["ok"] is True
    assert "open" in checks["cluster_tcp_22"]["detail"]  # real probe, not skipped
    assert checks["cluster_ssh_echo"]["ok"] is True
    ssh_run.assert_called_once()  # the production ssh path was exercised


# ─── submit-preflight routes the cluster arm to the no-op for pure-API ───────


def test_submit_preflight_omits_cluster_for_pure_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path, pure_api_backend: str
) -> None:
    monkeypatch.setattr(
        sp,
        "load_clusters_config",
        lambda: {"crowd": {"host": "ignored", "scheduler": pure_api_backend}},
    )
    calls = sp._build_subcalls(experiment_dir=tmp_path, cluster="crowd", skip=[])
    by_name = {c.name: c for c in calls}
    # check-preflight runs WITHOUT --cluster — its cluster (ssh) arm is the
    # no-op for a pure-API backend, so zero ssh is issued.
    assert "--cluster" not in by_name["check-preflight"].argv


def test_submit_preflight_keeps_cluster_for_ssh_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(
        sp,
        "load_clusters_config",
        lambda: {"hpc": {"host": "h", "scheduler": "sge"}},
    )
    calls = sp._build_subcalls(experiment_dir=tmp_path, cluster="hpc", skip=[])
    by_name = {c.name: c for c in calls}
    assert "--cluster" in by_name["check-preflight"].argv
