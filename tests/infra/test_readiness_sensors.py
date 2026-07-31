"""Readiness sensors: labelled legs, the dead-hop fixture, and the discriminator.

Every probe seam here is faked — ``ssh -G`` resolution, the TCP connector, and
the bounded ssh capture — so nothing in this file opens a socket. That is the
point of the sensor layer: the readings are injected, and what is under test is
the DERIVATION (which leg speaks for what, and which cause the atoms settle on).
"""

from __future__ import annotations

import subprocess

import pytest

from hpc_agent.infra import readiness_sensors as rs

HOP = "usc-discovery"
TARGET = "hoffman2.idre.ucla.edu"


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """Route cache + readiness ledger are process-global; isolate every test."""
    rs.clear_route_cache()
    rs.clear_readiness_ledger()


def _ssh_g(*, proxyjump: str | None, hostname: str = TARGET) -> str:
    lines = [f"host {hostname}", f"hostname {hostname}", "user someone", "port 22"]
    if proxyjump is not None:
        lines.append(f"proxyjump {proxyjump}")
    return "\n".join(lines) + "\n"


def _fake_resolution(monkeypatch: pytest.MonkeyPatch, stdout: str, rc: int = 0) -> None:
    """Serve *stdout* for TARGET's resolution; IDENTITY for every other token.

    ``sense_leg`` resolves each hop token through its own ``ssh -G`` pass
    (the 2026-07-30 alias fix), so a static single-answer fake would leak
    TARGET's hostname into the hop's dial. An unknown token resolves to
    itself — which keeps every connector fixture keyed on the tokens it
    already names."""

    def _run(argv, timeout):  # type: ignore[no-untyped-def]
        host = argv[-1]
        if host == TARGET:
            return subprocess.CompletedProcess(argv, rc, stdout, "")
        return subprocess.CompletedProcess(argv, 0, _ssh_g(proxyjump=None, hostname=host), "")

    monkeypatch.setattr(rs, "_run_route_resolution", _run)


def _connector(verdicts: dict[str, bool]):  # type: ignore[no-untyped-def]
    """A TCP connector fake: host -> reachable. Records every dial."""
    dialed: list[str] = []

    def _connect(host: str, port: int, timeout: float) -> tuple[bool, str]:
        dialed.append(host)
        if verdicts.get(host, True):
            return True, f"tcp connect to {host}:{port} ok"
        return False, "ConnectionRefusedError: [Errno 111] Connection refused"

    _connect.dialed = dialed  # type: ignore[attr-defined]
    return _connect


def _atom(readiness: rs.PathReadiness, sensor: str, *, route: str | None = None) -> rs.VerdictAtom:
    """The named atom, asserted present — a missing one is a test failure, not None."""
    found = readiness.atom(sensor, route=route)  # type: ignore[arg-type]
    assert found is not None, f"expected a {sensor!r} atom in {[a.sensor for a in readiness.atoms]}"
    return found


# ── route resolution reads ssh's own answer, never a config re-parse ─────────


def test_resolve_route_parses_the_effective_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=HOP))
    route = rs.resolve_route(TARGET)
    assert route.resolved is True
    assert route.jumped is True
    assert route.proxy_jump == (HOP,)
    assert route.final_hostname == TARGET


def test_resolve_route_treats_proxyjump_none_as_no_jump(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ssh -G`` spells "no jump" as the literal ``none`` — not an empty chain."""
    _fake_resolution(monkeypatch, _ssh_g(proxyjump="none"))
    assert rs.resolve_route(TARGET).jumped is False


def test_unresolvable_route_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A diagnosis layer must never be the reason something refuses."""
    _fake_resolution(monkeypatch, "", rc=255)
    route = rs.resolve_route(TARGET)
    assert route.resolved is False
    assert rs._classify(route, ()) == "route_unresolved"


# ── THE dead-hop fixture: the 2026-07-30 incident, mechanized ────────────────


def test_dead_hop_with_healthy_direct_yields_the_incident_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hop refuses, target answers directly ⇒ three labelled verdicts + the sentence.

    This is the exact shape that read ``hoffman2: reachable`` on 2026-07-30 while
    the configured jump was dead.
    """
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=HOP))
    connect = _connector({HOP: False, TARGET: True})

    readiness = rs.read_path_readiness(TARGET, connect=connect)

    hop = _atom(readiness, "hop")
    direct = _atom(readiness, "direct")
    path = _atom(readiness, "path")
    assert (hop.target, hop.verdict) == (HOP, "down")
    assert (direct.target, direct.verdict) == (TARGET, "ok")
    assert path.verdict == "down", "a dead hop must make the PATH dead, whatever the target said"
    assert readiness.cause == "hop_down_direct_ok"
    assert readiness.sentence == f"path dead (hop {HOP} down); direct alternative OK"
    assert readiness.ok is False


def test_dead_hop_with_dead_direct_names_both(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=HOP))
    readiness = rs.read_path_readiness(TARGET, connect=_connector({HOP: False, TARGET: False}))
    assert readiness.cause == "hop_down_direct_dead"
    assert readiness.sentence == f"path dead (hop {HOP} down); direct alternative also dead"


def test_unjumped_host_produces_no_route_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    """An un-jumped host must render exactly as it did before this layer existed."""
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=None))
    readiness = rs.read_path_readiness(TARGET, connect=_connector({TARGET: True}))
    assert readiness.sentence == ""
    assert readiness.cause == "path_ok"


def test_unjumped_host_dials_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sensor layer must not have doubled the cost of the ordinary case."""
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=None))
    connect = _connector({TARGET: True})
    rs.read_path_readiness(TARGET, connect=connect)
    assert connect.dialed == [TARGET]


# ── the discriminator: node degradation vs a tunnel drop ─────────────────────


def _fake_command_class(monkeypatch: pytest.MonkeyPatch, outcomes: dict[tuple[bool, str], int]):
    """Fake the bounded ssh capture, keyed by ``(is_direct, command_kind)``."""
    seen: list[tuple[bool, str]] = []

    def _run(argv, timeout):  # type: ignore[no-untyped-def]
        direct = "ProxyJump=none" in argv
        kind = "connect" if argv[-1] == "true" else "preamble"
        seen.append((direct, kind))
        rc = outcomes.get((direct, kind), 0)
        return subprocess.CompletedProcess(argv, rc, "", "boom" if rc else "")

    monkeypatch.setattr(rs, "_run_probe_ssh", _run)
    return seen


def test_preamble_ok_on_direct_route_is_a_transport_flap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connect OK, preamble hangs through the tunnel, preamble OK direct ⇒ TRANSPORT.

    The reading that actually settled 2026-07-30. The pre-fix breaker called this
    node-local degradation and recommended retargeting through the same hop.
    """
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=HOP))
    seen = _fake_command_class(monkeypatch, {(False, "preamble"): 255})

    readiness = rs.read_path_readiness(
        TARGET, connect=_connector({HOP: True, TARGET: True}), activation="module load x && "
    )

    assert readiness.cause == "transport_flap"
    assert (True, "preamble") in seen, "the DIRECT route must be probed to discriminate"
    remedy = rs.path_remediation(readiness)
    assert "TRANSPORT" in remedy
    assert "do NOT retarget" in remedy or "not node-local" in remedy.lower()


def test_preamble_failing_on_both_routes_is_node_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=HOP))
    _fake_command_class(monkeypatch, {(False, "preamble"): 255, (True, "preamble"): 255})
    readiness = rs.read_path_readiness(
        TARGET, connect=_connector({HOP: True, TARGET: True}), activation="module load x && "
    )
    assert readiness.cause == "preamble_degraded"
    assert "host-retarget" in rs.path_remediation(readiness)


def test_healthy_effective_route_skips_the_direct_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discriminator costs a connection — it must not fire when nothing is wrong."""
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=HOP))
    seen = _fake_command_class(monkeypatch, {})
    readiness = rs.read_path_readiness(
        TARGET, connect=_connector({HOP: True, TARGET: True}), activation="module load x && "
    )
    assert readiness.cause == "path_ok"
    assert all(not direct for direct, _kind in seen), "no direct probe when the route is healthy"


def test_preamble_is_not_attempted_when_connect_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead connect must never masquerade as a preamble failure."""
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=None))
    _fake_command_class(monkeypatch, {(False, "connect"): 255})
    readiness = rs.read_path_readiness(
        TARGET, connect=_connector({TARGET: True}), activation="module load x && "
    )
    preamble = _atom(readiness, "preamble", route="effective")
    assert preamble.verdict == "skipped"
    assert "connect class did not pass" in preamble.detail


# ── consult-first / freshness (s2-readiness pillars 1 + 3) ───────────────────


def test_a_fresh_reading_is_reused_instead_of_redialled(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=None))
    first = _connector({TARGET: True})
    rs.read_path_readiness(TARGET, connect=first)
    assert len(first.dialed) == 1

    second = _connector({TARGET: True})
    rs.read_path_readiness(TARGET, connect=second, freshness_window_sec=600.0)
    assert second.dialed == [], "a fresh atom must be reused, not re-dialled"


def test_a_stale_reading_is_resensed(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=None))
    rs.read_path_readiness(TARGET, connect=_connector({TARGET: True}))
    again = _connector({TARGET: True})
    rs.read_path_readiness(TARGET, connect=again, freshness_window_sec=0.0)
    assert again.dialed == [TARGET], "a stale atom must be re-sensed"


def test_consult_returns_none_past_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=None))
    rs.read_path_readiness(TARGET, connect=_connector({TARGET: True}))
    assert rs.consult_readiness(TARGET, window_sec=600.0) is not None
    assert rs.consult_readiness(TARGET, window_sec=0.0) is None


def test_atoms_carry_the_ledger_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pillar 1's unit of record: what / verdict / latency / when, on every atom."""
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=None))
    readiness = rs.read_path_readiness(TARGET, connect=_connector({TARGET: True}))
    direct = _atom(readiness, "direct")
    assert direct.target == TARGET
    assert direct.verdict == "ok"
    assert direct.latency_ms is not None and direct.latency_ms >= 0.0
    # ``at`` is the repo-wide ``utcnow_iso`` form (offset-aware, ``+00:00``);
    # ``at_epoch`` is the freshness key the consult path actually compares.
    assert direct.at.endswith("+00:00") and direct.at_epoch > 0.0


def test_failed_connect_is_never_called_node_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect that never established cannot be a preamble story.

    Regression: this classified as ``preamble_degraded``, whose remediation opens
    "connect succeeds but the activation preamble fails" — directly contradicting
    the FAILED connect atom it was derived from. On an un-jumped route a dead
    connect is simply an unreachable target.
    """
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=None))
    _fake_command_class(monkeypatch, {(False, "connect"): 255})
    readiness = rs.read_path_readiness(
        TARGET, connect=_connector({TARGET: True}), activation="module load x && "
    )
    assert readiness.cause == "target_unreachable"
    assert "connect succeeds" not in rs.path_remediation(readiness)


def test_failed_connect_through_a_jump_suspects_the_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jumped + connect never established ⇒ the jump is the differing ingredient."""
    _fake_resolution(monkeypatch, _ssh_g(proxyjump=HOP))
    _fake_command_class(monkeypatch, {(False, "connect"): 255})
    readiness = rs.read_path_readiness(
        TARGET, connect=_connector({HOP: True, TARGET: True}), activation="module load x && "
    )
    assert readiness.cause == "transport_flap"


class TestHopAliasResolution:
    """The 2026-07-30 first-live-day defect: a ProxyJump written as an ssh
    ALIAS (``usc-discovery``) has no DNS record, and the raw TCP dial on the
    token reported a HEALTHY hop as down. The dial must go to the alias-
    resolved HostName; the atom's identity stays the configured token."""

    def test_hop_alias_is_resolved_before_the_dial(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hpc_agent.infra import readiness_sensors as rs

        monkeypatch.setattr(
            rs,
            "resolve_route",
            lambda token, **_k: rs.RouteChain(
                host=token,
                hostname="discovery2.usc.edu" if token == "usc-discovery" else token,
                resolved=True,
            ),
        )
        dialed: list[str] = []

        def fake_connect(host: str, port: int, timeout: float) -> tuple[bool, str]:
            dialed.append(host)
            # The REAL hostname answers; the raw alias would fail resolution.
            return (host == "discovery2.usc.edu", "connect probe")

        atom = rs.sense_leg("usc-discovery", kind="hop", connect=fake_connect)
        assert dialed == ["discovery2.usc.edu"]
        assert atom.verdict == "ok"
        assert atom.target == "usc-discovery"  # identity stays the token
        assert "alias usc-discovery -> discovery2.usc.edu" in (atom.detail or "")

    def test_resolution_failure_falls_back_to_the_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hpc_agent.infra import readiness_sensors as rs

        def boom(token: str, **_k: object) -> object:
            raise OSError("ssh -G unavailable")

        monkeypatch.setattr(rs, "resolve_route", boom)
        atom = rs.sense_leg("10.0.0.7", kind="hop", connect=lambda h, p, t: (True, "connect probe"))
        assert atom.verdict == "ok"
        assert atom.target == "10.0.0.7"
