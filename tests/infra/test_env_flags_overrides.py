"""``active_env_overrides`` — the ONE env-disclosure helper (B15).

The env-vs-record drift seat (run-12 finding 24 addendum): a stray HPC_*
transport override can outlive the session that set it and silently reroute
every ssh call while the durable record says it was retired. This helper is
the single definition every judgment surface (doctor, status-snapshot,
net-triage, campaign briefs) echoes verbatim — pure disclosure, never judged.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra.env_flags import active_env_overrides

#: The transport-affecting overrides whose silent presence reroutes / reshapes
#: SSH while the durable record says otherwise — the exact drift class B15
#: exists to surface. The helper returns ALL HPC_*, so this is a coverage pin:
#: each must appear verbatim once exported. (The SSH connection broker's env
#: switch was retired with the broker itself — run #9 zero-fallback evidence —
#: so HPC_SSH_BINARY stands in as the fifth real transport lever.)
_TRANSPORT_VARS = {
    "HPC_SSH_ENGINE": "asyncssh",
    "HPC_SSH_CIRCUIT_OVERRIDE": "login.cluster.edu",
    "HPC_NO_SSH_MULTIPLEX": "1",
    "HPC_CLUSTERS_CONFIG": "/tmp/clusters.yaml",
    "HPC_SSH_BINARY": "/usr/bin/ssh",
}


@pytest.fixture(autouse=True)
def _clean_hpc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from a world with no HPC_* exported so each test controls the set."""
    import os

    for key in [k for k in os.environ if k.startswith("HPC_")]:
        monkeypatch.delenv(key, raising=False)


def test_helper_echoes_exported_hpc_var_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_SSH_ENGINE", "asyncssh")
    out = active_env_overrides()
    assert out["HPC_SSH_ENGINE"] == "asyncssh"  # verbatim, never normalized


def test_helper_excludes_non_hpc_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH_LOOKALIKE_HPC", "no")  # does not start with HPC_
    monkeypatch.setenv("NOT_HPC_SSH_ENGINE", "no")
    monkeypatch.setenv("HPC_SSH_ENGINE", "asyncssh")
    out = active_env_overrides()
    assert out == {"HPC_SSH_ENGINE": "asyncssh"}
    assert all(k.startswith("HPC_") for k in out)


def test_helper_empty_when_no_hpc_var_set() -> None:
    assert active_env_overrides() == {}


@pytest.mark.parametrize(("var", "value"), sorted(_TRANSPORT_VARS.items()))
def test_five_transport_vars_are_surfaced(
    var: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each transport-affecting override, once exported, is disclosed verbatim —
    the drift class B15 exists to make visible (a stray one IS the finding)."""
    monkeypatch.setenv(var, value)
    out = active_env_overrides()
    assert out[var] == value
