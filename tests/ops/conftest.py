"""Shared fixtures for ``tests/ops``.

The O2 pull-parity merge made ``tar_ssh_pull`` the preferred engine inside
``aggregate_flow._pull`` whenever the seam is importable. The ops suites were
written against the legacy ``rsync_pull`` seam (they mock it directly), so
without a pin the adapter routes AROUND those mocks into the hermetic
real-ssh guard. Pin the legacy path once here — the whole package's default —
instead of per-module whack-a-mole; the tar path's own coverage lives in
``tests/ops/aggregate/test_pull_tar_seam.py`` (whose tests set/del the env
explicitly, overriding this autouse default) and the O2 transport suite.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _ops_legacy_pull_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_AGGREGATE_TAR_PULL", "0")
