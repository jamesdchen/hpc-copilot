"""``DeterministicCampaignResolver._backend_for`` resolves against the registry.

The resolver picks the backend a prior run used from the cluster config. #337
(Class A) replaced its hardcoded ``("sge","slurm","pbspro","torque")`` guard
with a live-registry membership check, so a registered plugin backend resolves
through unchanged while an *unregistered* scheduler string still falls back to
``slurm`` (rather than shipping a name the submit spec's ``BackendName``
validator would reject downstream).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hpc_agent.infra import backends
from hpc_agent.infra.backends import HPCBackend
from hpc_agent.meta.campaign.deterministic_resolver import DeterministicCampaignResolver


def _backend_for(scheduler_value, monkeypatch):
    # ``_backend_for`` lazily imports ``load_clusters_config``; patch it at the
    # source module so the resolver reads our synthetic cluster config.
    import hpc_agent.infra.clusters as clusters

    monkeypatch.setattr(
        clusters, "load_clusters_config", lambda: {"c": {"scheduler": scheduler_value}}
    )
    record = SimpleNamespace(cluster="c")
    return DeterministicCampaignResolver._backend_for(record, {})


@pytest.mark.parametrize("name", ["sge", "slurm", "pbspro", "torque"])
def test_builtin_scheduler_resolves_through(name, monkeypatch) -> None:
    assert _backend_for(name, monkeypatch) == name


def test_unregistered_scheduler_falls_back_to_slurm(monkeypatch) -> None:
    assert _backend_for("not-a-real-backend", monkeypatch) == "slurm"


def test_registered_plugin_backend_resolves_through(monkeypatch) -> None:
    # A registered plugin backend name must survive the guard — proof the guard
    # reads the live registry, not a frozen built-in tuple.
    @backends.register("fakeresolverbackend")
    class _FakeBackend(HPCBackend):  # pragma: no cover - never executed
        scheduler_name = "fakeresolverbackend"
        requires_ssh = False

        def _build_command(self, *a, **k):
            raise NotImplementedError

    try:
        assert _backend_for("fakeresolverbackend", monkeypatch) == "fakeresolverbackend"
    finally:
        backends._REGISTRY.pop("fakeresolverbackend", None)
