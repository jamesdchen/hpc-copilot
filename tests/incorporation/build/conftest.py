"""Local fixtures for tests under ``tests/incorporation/build/``.

The autouse :func:`_isolate_clusters_config` here is scoped to this
directory so it only affects tests that build / validate submit specs.
Tests elsewhere (``tests/cli/test_envelope.py``,
``tests/ops/aggregate/test_canary_verify.py``, …) intentionally either
load the packaged ``clusters.yaml`` or set their own
``HPC_CLUSTERS_CONFIG``; isolating those would break their explicit
contract.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_clusters_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Isolate every test in this directory from the host's clusters.yaml.

    Tests that build submit specs hit ``validate_remote_path_under_scratch``,
    which calls :func:`load_clusters_config` to look up the cluster's
    ``scratch`` root. The search order (see ``infra/clusters.py``) falls
    through to ``~/.hpc-agent/clusters.yaml`` on a dev box — which
    `hpc-agent setup` populates with the developer's real cluster scratch
    (e.g. ``/u/scratch/<your_user>``). That value bleeds into the test
    and rejects the synthetic ``/u/scratch/alice/exp``-style fixtures
    here, even though the source change under test is fine.

    Point ``HPC_CLUSTERS_CONFIG`` at an empty per-test yaml so
    ``load_clusters_config`` returns ``{}`` by default. Individual tests
    that want their own cluster config can re-monkeypatch the env var or
    pass ``path=`` explicitly — both override this autouse default.
    """
    iso_dir = tmp_path_factory.mktemp("isolated_clusters")
    empty = iso_dir / "clusters.yaml"
    empty.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(empty))
