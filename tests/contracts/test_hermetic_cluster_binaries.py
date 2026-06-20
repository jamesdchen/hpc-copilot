"""Fire-path proof for the default-tier cluster-binary hermeticity guard.

The guard itself lives in ``tests/conftest.py`` (the autouse
``_hermetic_cluster_binaries`` fixture). Per the repo principle that every
enforcement mechanism must demonstrate it can actually fire
(``docs/internals/engineering-principles.md``), this module exercises both
sides of the contract:

* in a default-tier (non-``slow``) test, every cluster binary is shadowed by a
  stub that exits non-zero with the pointer message — proving a test that
  *reaches* the cluster fails loudly and host-independently; and
* a ``slow``-marked test opts back out — the stubs are NOT in force, so the
  real-process tier keeps its real binaries.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from hpc_agent.infra import ssh_options

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="guard is POSIX-only; the blocking CI matrix is Linux (see conftest).",
)

_SHIM_DIRNAME = "hermetic_cluster_shims"


@pytest.mark.parametrize("name", ["ssh", "scp", "rsync", "ssh-add"])
def test_real_cluster_binary_is_shadowed_in_default_tier(name: str) -> None:
    """A default-tier test that shells out to a cluster binary hits the stub."""
    proc = subprocess.run([name, "ignored-arg"], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 97, (
        f"{name!r} did not resolve to the hermetic stub (rc={proc.returncode}); "
        "the guard fixture is not shadowing PATH for this test."
    )
    assert "hermetic-guard" in proc.stderr
    assert "@pytest.mark.slow" in proc.stderr


def test_env_resolvers_point_at_the_stub_in_default_tier() -> None:
    """The ``HPC_*_BINARY`` resolvers (which win unconditionally) are pinned."""
    for resolver in (ssh_options._ssh_binary, ssh_options._scp_binary):
        resolved = resolver()
        assert _SHIM_DIRNAME in resolved, (
            f"{resolver.__name__}() returned {resolved!r}; the env-override knob "
            "is not pointing at the hermetic stub."
        )


@pytest.mark.slow
def test_slow_tier_opts_out_of_the_guard() -> None:
    """A ``slow`` test keeps the real binaries — the stub dir is not in force."""
    assert _SHIM_DIRNAME not in os.environ.get("PATH", "")
    assert _SHIM_DIRNAME not in (os.environ.get("HPC_SSH_BINARY") or "")
