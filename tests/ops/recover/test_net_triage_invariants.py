"""``net-triage --probe-invariants`` — the sanctioned prober for the four invariants.

The readiness ledger's feed sites only ever HARVEST, so the three probing sensors
(``scratch`` / ``scheduler`` / ``env``) need a caller that is allowed to dial.
``net-triage`` is it: a diagnosis verb whose whole job is paying for connections
to answer "why can't I use this cluster?". Without this wiring the sensors were
built but unreachable in production, and ``net_triage.output.json`` advertised
three enum values nothing could emit — a schema promising facts the code could
not produce.

Opt-in, matching ``probe_preamble``'s idiom, because each rung costs a real
connection. What is pinned here: the rungs stay OFF by default (no cost, no
behaviour change for every existing caller), they emit real atoms when asked, and
they never move the transport verdict — reaching a login node and being able to
USE the cluster are different claims, and this verb must not conflate them.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from hpc_agent._wire.queries.net_triage import NetTriageSpec
from hpc_agent.infra import readiness_sensors as rs
from hpc_agent.ops.recover import net_triage as nt
from hpc_agent.ops.recover.net_triage import net_triage

HOST = "login.cluster.edu"
SCRATCH = "/u/scratch/someone"
CLUSTER = "testcluster"


@pytest.fixture()
def probes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """All-healthy transport + a stubbed cluster config and ssh capture."""
    outcomes: dict[str, Any] = {
        "commands": [],
        "ssh_rc": 0,
        "ssh_stdout": "hpc-agent 0.11.4+g766c293d\n",
        "ssh_stderr": "",
    }
    monkeypatch.setattr(nt, "_https_check", lambda url, t: (True, "HTTP 204"))
    monkeypatch.setattr(nt, "_dns_resolve", lambda host, t: (True, "resolved to 192.0.2.10"))
    monkeypatch.setattr(
        nt, "_tcp_connect", lambda host, port, t: (True, f"tcp connect to {host}:{port} ok")
    )
    monkeypatch.setattr(nt, "_configured_hosts", lambda: [(HOST, CLUSTER)])
    monkeypatch.setattr(
        nt,
        "_cluster_invariants",
        lambda cluster: (SCRATCH, "sge") if cluster == CLUSTER else (None, None),
    )
    monkeypatch.setattr(
        nt,
        "_cluster_activation",
        lambda cluster: (("module load python && conda activate hpc", f"someone@{HOST}")),
    )

    rs.clear_route_cache()
    rs.clear_readiness_ledger()

    def _resolve(argv, timeout):  # type: ignore[no-untyped-def]
        host = argv[-1]
        return subprocess.CompletedProcess(
            argv, 0, f"host {host}\nhostname {host}\nuser someone\nport 22\n", ""
        )

    monkeypatch.setattr(rs, "_run_route_resolution", _resolve)

    def _ssh(argv, timeout):  # type: ignore[no-untyped-def]
        outcomes["commands"].append(argv[-1])
        return subprocess.CompletedProcess(
            argv, outcomes["ssh_rc"], outcomes["ssh_stdout"], outcomes["ssh_stderr"]
        )

    monkeypatch.setattr(rs, "_run_probe_ssh", _ssh)
    # ``ssh_argv`` shells ``ssh -V`` once per process to pick its crypto; pin it
    # so no test here depends on which one ran first (the cache is process-wide).
    from hpc_agent.infra import ssh_options

    monkeypatch.setattr(ssh_options, "_local_openssh_supports_gcm", lambda: True)
    return outcomes


def _atoms(result: Any) -> dict[str, Any]:
    return {a.sensor: a for a in result.hosts[0].readiness}


def test_the_rungs_are_OFF_by_default(probes: dict[str, Any]) -> None:
    """Every existing caller keeps paying exactly what it paid before."""
    result = net_triage(spec=NetTriageSpec())
    assert set(_atoms(result)) & {"scratch", "scheduler", "env"} == set()
    assert probes["commands"] == [], "the default path must open no command-class ssh"


def test_opting_in_emits_all_three_invariant_atoms(probes: dict[str, Any]) -> None:
    result = net_triage(spec=NetTriageSpec(probe_invariants=True))
    atoms = _atoms(result)
    assert atoms["scratch"].target == SCRATCH
    assert atoms["scratch"].verdict == "ok"
    assert atoms["scheduler"].target == "sge"
    assert atoms["scheduler"].verdict == "ok"
    assert atoms["env"].verdict == "ok"
    # The env atom carries the FINGERPRINT, which is the whole reason to ask.
    assert "0.11.4+g766c293d" in atoms["env"].detail


def test_the_rungs_run_the_expected_command_classes(probes: dict[str, Any]) -> None:
    net_triage(spec=NetTriageSpec(probe_invariants=True))
    joined = " ".join(probes["commands"])
    assert f"test -d '{SCRATCH}'" in joined
    assert "df -P" in joined
    assert "qstat" in joined
    assert "hpc-agent --version" in joined


def test_the_env_rung_runs_UNDER_the_cluster_activation(probes: dict[str, Any]) -> None:
    """The fingerprint is meaningless outside the activated env — that is where
    the wheel lives. Reading it from a bare login shell would report the wrong
    wheel (or none) with full confidence.

    This holds even though ``probe_preamble`` is OFF: the activation prefix and
    the preamble RUNG are different things, and opting into the invariants must
    neither smuggle in the preamble rung nor lose the prefix.
    """
    net_triage(spec=NetTriageSpec(probe_invariants=True))
    env_cmd = next(c for c in probes["commands"] if c.endswith("hpc-agent --version"))
    assert env_cmd.startswith("module load python && conda activate hpc &&")


def test_opting_into_invariants_does_not_smuggle_in_the_preamble_rung(
    probes: dict[str, Any],
) -> None:
    """Two opt-ins, two costs. ``probe_preamble`` stays the only thing that buys
    the {connect, preamble} pair."""
    atoms = _atoms(net_triage(spec=NetTriageSpec(probe_invariants=True)))
    assert "preamble" not in atoms


def test_a_broken_invariant_never_flips_the_transport_verdict(
    probes: dict[str, Any],
) -> None:
    """Reaching a login node and being able to USE the cluster are different
    claims. A full scratch disk must not read as an unreachable host — that would
    make the fleet flag fire on healthy transport and teach the operator to ignore
    it, which is how the original 2026-07-30 conflation survived.
    """
    probes["ssh_rc"] = 1
    probes["ssh_stdout"] = ""
    probes["ssh_stderr"] = "df: /u/scratch/someone: No space left on device"
    result = net_triage(spec=NetTriageSpec(probe_invariants=True))
    host = result.hosts[0]
    assert _atoms(result)["scratch"].verdict == "down"
    assert host.verdict == "reachable"
    assert host.path_cause in (None, "path_ok", "path_unproven", "route_unresolved")
    assert result.all_reachable is True


def test_every_advertised_sensor_value_is_emittable(probes: dict[str, Any]) -> None:
    """The wire enum promised nine kinds; before this wiring three could never be
    produced by any code path, and a schema that advertises facts the code cannot
    emit is a lie a consumer will build against.

    ``hop`` / ``direct`` need a jumped route to appear, so they are excluded here
    and covered by the dead-hop fixtures in the sensor suite.
    """
    from typing import get_args, get_type_hints

    from hpc_agent._wire.queries.net_triage import ReadinessAtom

    declared = set(get_args(get_type_hints(ReadinessAtom)["sensor"]))
    spec = NetTriageSpec(probe_invariants=True, probe_preamble=True)
    emitted = set(_atoms(net_triage(spec=spec)))
    assert {"scratch", "scheduler", "env", "auth"} <= emitted
    assert declared - emitted <= {"hop", "direct"}
