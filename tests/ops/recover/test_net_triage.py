"""Tests for ``net-triage`` — the mechanized connectivity differential.

Probes are monkeypatched module functions (no real network in any test);
breaker state is written as the durable JSON docs ``infra.ssh_circuit``
persists, under the suite-wide isolated journal home.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from hpc_agent._wire.queries.net_triage import NetTriageSpec
from hpc_agent.infra.ssh_circuit import circuit_state_path
from hpc_agent.ops.recover import net_triage as nt
from hpc_agent.ops.recover.net_triage import net_triage, open_circuit_lines

HOST = "login.cluster.edu"


def _write_circuit(
    host: str,
    *,
    state: str = "open",
    failures: int = 4,
    opened_at: float | None = None,
    cooldown_sec: float = 300.0,
    last_detail: str = "connection timed out: ...",
) -> None:
    """Persist a breaker state doc in the shape ``infra.ssh_circuit`` writes."""
    path = circuit_state_path(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {
        "schema_version": 1,
        "host": host,
        "state": state,
        "consecutive_failures": failures,
        "cooldown_sec": cooldown_sec,
        "opened_at": time.time() if opened_at is None else opened_at,
        "probe_claimed_at": None,
        "last_failure": {"at": time.time(), "detail": last_detail},
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture()
def probes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Default all-healthy probes; tests flip individual outcomes."""
    outcomes: dict[str, Any] = {
        "https": (True, "HTTP 204"),
        "dns": (True, "resolved to 192.0.2.10"),
        "tcp": (True, f"tcp connect to {HOST}:22 ok"),
        "tcp_calls": 0,
    }
    monkeypatch.setattr(nt, "_https_check", lambda url, t: outcomes["https"])
    monkeypatch.setattr(nt, "_dns_resolve", lambda host, t: outcomes["dns"])

    def _tcp(host: str, port: int, t: float) -> tuple[bool, str]:
        outcomes["tcp_calls"] += 1
        result: tuple[bool, str] = outcomes["tcp"]
        return result

    monkeypatch.setattr(nt, "_tcp_connect", _tcp)
    # No real clusters.yaml in tests: the caller-supplied host is the fleet.
    monkeypatch.setattr(nt, "_configured_hosts", lambda: [])
    return outcomes


def _one(spec_host: str = HOST) -> Any:
    out = net_triage(spec=NetTriageSpec(host=spec_host))
    assert len(out.hosts) == 1
    return out


# ─── verdict table: one test per enum arm ────────────────────────────────────


def test_verdict_reachable(probes: dict[str, Any]) -> None:
    out = _one()
    (h,) = out.hosts
    assert h.verdict == "reachable"
    assert h.breaker.state == "missing"  # no state file → healthy, fail-open
    assert out.all_reachable is True
    assert "auth or ssh config" in h.remediation


def test_verdict_breaker_open_cooling_skips_tcp_probe(probes: dict[str, Any]) -> None:
    """An open breaker yields the breaker verdict AND the TCP probe is never
    made — triage must not add a connection or race the half-open slot."""
    _write_circuit(HOST, failures=4)
    out = _one()
    (h,) = out.hosts
    assert h.verdict == "breaker_open_cooling"
    assert probes["tcp_calls"] == 0
    assert h.tcp_ok is None
    assert "breaker is open" in (h.tcp_detail or "")
    assert h.breaker.state == "open"
    assert h.breaker.consecutive_failures == 4
    assert h.breaker.cooldown_until is not None
    # Remediation names the deadline and the per-host override.
    assert h.breaker.cooldown_until in h.remediation
    assert f"HPC_SSH_CIRCUIT_OVERRIDE={HOST}" in h.remediation


def test_verdict_local_network_down(probes: dict[str, Any]) -> None:
    probes["https"] = (False, "URLError: unreachable")
    probes["tcp"] = (False, "TimeoutError: timed out")
    out = _one()
    (h,) = out.hosts
    assert h.verdict == "local_network_down"
    assert "LOCAL NETWORK DOWN" in out.summary
    assert "cluster is not implicated" in h.remediation


def test_verdict_dns_failure_skips_tcp(probes: dict[str, Any]) -> None:
    probes["dns"] = (False, "gaierror: Name or service not known")
    out = _one()
    (h,) = out.hosts
    assert h.verdict == "dns_failure"
    assert probes["tcp_calls"] == 0
    assert h.tcp_ok is None
    assert "alias" in h.remediation


def test_verdict_host_unreachable_network_ok(probes: dict[str, Any]) -> None:
    """TCP fails, control passes — cluster-side/border issue; the remediation
    cites the traceroute discrimination and forbids retry-storms."""
    probes["tcp"] = (False, "TimeoutError: timed out")
    out = _one()
    (h,) = out.hosts
    assert h.verdict == "host_unreachable_network_ok"
    assert "traceroute" in h.remediation
    assert "retry-storm" in h.remediation
    assert "out-of-band" in h.remediation
    assert out.all_reachable is False


def test_tcp_success_outranks_open_breaker_is_impossible_probe_skipped(
    probes: dict[str, Any],
) -> None:
    """With the breaker open the probe never runs, so direct-evidence
    precedence cannot fire — the breaker verdict stands."""
    _write_circuit(HOST)
    out = _one()
    assert out.hosts[0].verdict == "breaker_open_cooling"
    assert probes["tcp_calls"] == 0


def test_control_failure_outranks_open_breaker(probes: dict[str, Any]) -> None:
    """A dead local network explains everything — including the breaker."""
    _write_circuit(HOST)
    probes["https"] = (False, "URLError: unreachable")
    out = _one()
    assert out.hosts[0].verdict == "local_network_down"
    assert out.hosts[0].breaker.state == "open"  # still surfaced as evidence


# ─── fail-open + enumeration ─────────────────────────────────────────────────


def test_fail_open_on_corrupt_breaker_state(probes: dict[str, Any]) -> None:
    path = circuit_state_path(HOST)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    out = _one()
    (h,) = out.hosts
    assert h.breaker.state == "missing"
    assert h.verdict == "reachable"


def test_caller_host_is_normalized_and_merged_with_configured(
    probes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nt, "_configured_hosts", lambda: [("cfg.cluster.edu", "cfgcluster")])
    out = net_triage(spec=NetTriageSpec(host=f"user@{HOST}"))
    assert [(h.host, h.cluster) for h in out.hosts] == [
        ("cfg.cluster.edu", "cfgcluster"),
        (HOST, None),
    ]


def test_no_hosts_yields_empty_summary(probes: dict[str, Any]) -> None:
    out = net_triage(spec=NetTriageSpec())
    assert out.hosts == []
    assert out.all_reachable is False
    assert "no hosts to triage" in out.summary


def test_open_circuit_lines_scans_state_dir(probes: dict[str, Any]) -> None:
    assert open_circuit_lines() == []
    _write_circuit(HOST, failures=4)
    _write_circuit("healthy.cluster.edu", state="closed", failures=0)
    lines = open_circuit_lines()
    assert len(lines) == 1
    assert f"ssh circuit for {HOST}: OPEN until" in lines[0]
    assert "(4 failures)" in lines[0]
    assert "net-triage" in lines[0]


# ─── surfacing: doctor + status-snapshot carry the breaker line ──────────────


def test_doctor_carries_open_circuit_line(probes: dict[str, Any], tmp_path: Any) -> None:
    from hpc_agent._wire.queries.doctor import DoctorSpec
    from hpc_agent.ops.recover.doctor import doctor

    _write_circuit(HOST, failures=4)
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-05T01:00:00+00:00"))
    assert out["needs_attention"] is True
    assert "1 open ssh circuit(s)" in out["attention_summary"]
    assert f"ssh circuit for {HOST}: OPEN until" in out["attention_summary"]
    assert len(out["open_ssh_circuits"]) == 1
    assert "(4 failures)" in out["open_ssh_circuits"][0]


def test_doctor_all_closed_has_no_circuit_lines(probes: dict[str, Any], tmp_path: Any) -> None:
    from hpc_agent._wire.queries.doctor import DoctorSpec
    from hpc_agent.ops.recover.doctor import doctor

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-05T01:00:00+00:00"))
    assert out["open_ssh_circuits"] == []
    assert out["needs_attention"] is False


def test_status_snapshot_brief_carries_open_circuit_line(
    probes: dict[str, Any], tmp_path: Any
) -> None:
    from hpc_agent._wire.workflows.status_blocks import StatusSnapshotSpec
    from hpc_agent.ops.status_blocks import status_snapshot

    _write_circuit(HOST, failures=4)
    result = status_snapshot(tmp_path, spec=StatusSnapshotSpec())
    lines = result.brief["open_ssh_circuits"]
    assert len(lines) == 1
    assert f"ssh circuit for {HOST}: OPEN until" in lines[0]

    # All-closed → the key is present and empty (shape-stable brief).
    circuit_state_path(HOST).unlink()
    result = status_snapshot(tmp_path, spec=StatusSnapshotSpec())
    assert result.brief["open_ssh_circuits"] == []
