"""Test isolation for the backend registry.

``register_profile`` mutates the module-global ``_REGISTRY``. Without a
restore, a test that registers a resolved profile would leak it into
every later test in the same xdist worker — and with the conflict guard
in ``register_profile`` a leaked label can turn an unrelated later
registration into a spurious ``SpecInvalid``. This autouse fixture
snapshots the registry before each test and restores it after, so
profile-registration tests are hermetic.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_backend_registry():
    from hpc_agent.infra.backends import _REGISTRY, _populate_registry

    # Ensure the golden labels are present before snapshotting so the
    # baseline is the real steady state, then restore exactly that.
    _populate_registry()
    snapshot = dict(_REGISTRY)
    try:
        yield
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(snapshot)
