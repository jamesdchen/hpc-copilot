"""Local conftest for the scheduler-in-a-container integration tier.

Registers the ``scheduler_integration`` marker HERE (via ``pytest_configure``)
rather than in ``pyproject.toml`` — the pyproject is owned by another surface
and this tier must stay self-contained so it can be iterated on without
touching shared config. ``--strict-markers`` therefore still passes because the
marker is registered before collection.

These tests are ALSO marked ``slow`` (see ``test_scheduler_smoke.py``): the
top-level ``tests/conftest.py`` autouse ``_hermetic_cluster_binaries`` fixture
shadows the real ``ssh`` / ``scp`` / ``rsync`` binaries for every NON-``slow``
test, and this tier's whole point is to reach a REAL cluster over those
binaries. The ``slow`` marker is the opt-out that lets the real transport
through.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "scheduler_integration: real submit spine against a real Slurm in a "
        "container (see docs/internals/scheduler-integration-ci.md). Inert "
        "unless HPC_SCHEDULER_IT=1 and the container env vars are present.",
    )
