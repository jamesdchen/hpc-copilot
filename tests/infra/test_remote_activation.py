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


def test_fallback_cluster_seeds_activation_for_a_cluster_less_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run #7: every submit-flow sidecar carries no ``cluster``, so tier-2
    backfill never fires and the control-plane reporter/combiner/reducer runs
    bare login python (rc=127). A consumer passing the run record's cluster as
    ``fallback_cluster`` restores the backfill — the ONE seam replacing the
    per-consumer seeds copy-pasted into verify_canary / record_status."""
    monkeypatch.setattr(
        clusters_mod,
        "load_clusters_config",
        lambda: {
            "hoffman2": {"conda_source": "/c/conda.sh", "conda_envs": ["hpc-pi"]},
            "discovery": {"conda_source": "/d/conda.sh", "conda_envs": ["other"]},
        },
    )
    # A BARE sidecar with no fallback → unchanged "" (tier-3 bare python).
    assert remote_activation_for_sidecar({}) == ""
    # ...but the fallback restores the cluster backfill.
    seeded = remote_activation_for_sidecar({}, fallback_cluster="hoffman2")
    assert "conda activate hpc-pi" in seeded
    # The sidecar's OWN cluster still wins over the fallback (precedence).
    both = remote_activation_for_sidecar({"cluster": "discovery"}, fallback_cluster="hoffman2")
    assert "conda activate other" in both
    assert "hpc-pi" not in both


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


def test_for_sidecar_derives_from_cluster_when_env_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 13 (proving run #5): a hand-carried sidecar that dropped its
    ``env`` activation block but KEPT ``cluster`` must still activate — the
    prefix derives from clusters.yaml, never falls to a bare ``python`` that
    the cluster's Lmod default hijacks (``exit 127`` on every canary/status
    poll). Activation is a cluster-local fact; it cannot depend on a field the
    sidecar can drop.
    """
    monkeypatch.setattr(
        clusters_mod,
        "load_clusters_config",
        lambda: {"myc": {"conda_source": "/c/conda.sh", "conda_envs": ["hpc-pi"]}},
    )
    # No env at all — the damaged-sidecar shape.
    assert (
        remote_activation_for_sidecar({"cluster": "myc"})
        == "source /c/conda.sh && conda activate hpc-pi && "
    )
    # Present-but-empty env — same derivation.
    assert (
        remote_activation_for_sidecar({"cluster": "myc", "env": {}})
        == "source /c/conda.sh && conda activate hpc-pi && "
    )


def test_for_sidecar_explicit_pin_wins_over_absent_cluster_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: an activation the sidecar pins EXPLICITLY is honored even when
    the cluster is ad-hoc (absent from clusters.yaml) or its config drifted —
    the reporter must not fall to bare ``python`` when the sidecar itself
    carries enough to activate."""
    monkeypatch.setattr(clusters_mod, "load_clusters_config", dict)  # cluster unknown
    p = remote_activation_for_sidecar(
        {"cluster": "adhoc", "env": {"conda_source": "/pinned/conda.sh", "conda_env": "e"}}
    )
    assert p == "source /pinned/conda.sh && conda activate e && "


def test_for_sidecar_pin_overrides_cluster_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sidecar-pinned field wins per-field over the cluster's; omitted fields
    still back-fill from the cluster (so a pinned conda_source coexists with a
    cluster-derived module load)."""
    monkeypatch.setattr(
        clusters_mod,
        "load_clusters_config",
        lambda: {
            "myc": {
                "modules": ["python/3.10"],
                "conda_source": "/cluster/conda.sh",
                "conda_envs": ["cluster-env"],
            }
        },
    )
    p = remote_activation_for_sidecar(
        {"cluster": "myc", "env": {"conda_source": "/pinned/conda.sh"}}
    )
    # conda_source pinned by the sidecar wins; modules + conda_envs[0] back-fill
    # from the cluster.
    assert p == (
        "module load python/3.10 && source /pinned/conda.sh && conda activate cluster-env && "
    )
