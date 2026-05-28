"""Control-plane remote env activation (issue #135 item 3).

The status reporter + combiner run directly on the login node via
``ssh_run`` and never source the job preamble, so they need the conda /
module activation built inline. These pin the prefix shape and the
sidecar-driven resolution.
"""

from __future__ import annotations

import pytest

import hpc_agent.infra.clusters as clusters_mod
from hpc_agent.infra.clusters import (
    remote_activation_for_sidecar,
    remote_activation_prefix,
)


def test_prefix_empty_when_nothing_configured() -> None:
    assert remote_activation_prefix({}) == ""
    assert remote_activation_prefix({"modules": [], "conda_source": None}) == ""


def test_prefix_conda_source_and_first_env() -> None:
    p = remote_activation_prefix({"conda_source": "/c/conda.sh", "conda_envs": ["envA", "envB"]})
    assert p == "source /c/conda.sh && conda activate envA && "


def test_prefix_modules_only() -> None:
    p = remote_activation_prefix({"modules": ["python/3.10", "gcc"]})
    assert p == "module load python/3.10 && module load gcc && "


def test_prefix_conda_env_override_wins() -> None:
    p = remote_activation_prefix(
        {"conda_source": "/c/conda.sh", "conda_envs": ["default"]},
        conda_env="per-run",
    )
    assert "conda activate per-run && " in p
    assert "default" not in p


def test_prefix_placeholder_env_is_not_activated() -> None:
    # The `<your_env>` placeholder must not become `conda activate <your_env>`.
    p = remote_activation_prefix({"conda_source": "/c/conda.sh", "conda_envs": ["<your_env>"]})
    assert p == "source /c/conda.sh && "


def test_prefix_full_chain() -> None:
    p = remote_activation_prefix(
        {"modules": ["python/3.10"], "conda_source": "/c/conda.sh", "conda_envs": ["hpc-pi"]}
    )
    assert p == "module load python/3.10 && source /c/conda.sh && conda activate hpc-pi && "


def test_for_sidecar_no_cluster_is_empty() -> None:
    assert remote_activation_for_sidecar({}) == ""
    assert remote_activation_for_sidecar({"env": {"conda_env": "x"}}) == ""


def test_for_sidecar_resolves_cluster_and_run_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        clusters_mod,
        "load_clusters_config",
        lambda: {"myc": {"conda_source": "/c/conda.sh", "conda_envs": ["fallback"]}},
    )
    # The sidecar's resolved env wins over the cluster's conda_envs[0].
    p = remote_activation_for_sidecar({"cluster": "myc", "env": {"conda_env": "run-env"}})
    assert p == "source /c/conda.sh && conda activate run-env && "


def test_for_sidecar_bad_config_falls_back_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> dict:
        raise RuntimeError("clusters.yaml unreadable")

    monkeypatch.setattr(clusters_mod, "load_clusters_config", _boom)
    # A broken config must not break status/aggregate — degrade to bare python.
    assert remote_activation_for_sidecar({"cluster": "myc"}) == ""
