"""The S2 pre-detach path gate: refuse synchronously, consult before sensing.

The 2026-07-30 shape under test: two detached S2 workers died in ~16s because
the ``ProxyJump`` hop was dead, and the cause was only discoverable by reading
worker logs afterwards. The gate's job is to put that discrimination in the
SYNCHRONOUS response instead.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.infra import readiness_sensors as rs
from hpc_agent.ops import path_gate

HOP = "usc-discovery"
HOST = "hoffman2.idre.ucla.edu"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the process-global caches AND re-enable the gate for this file.

    The suite disables the gate by default (``tests/conftest.py``) so no test
    accidentally dials a fixture host. This file tests the gate itself, so it
    turns it back on — safely, because every case here injects a ``reader`` and
    never reaches a sensor.
    """
    monkeypatch.delenv(path_gate.GATE_ENV, raising=False)
    rs.clear_route_cache()
    rs.clear_readiness_ledger()


def _readiness(cause: rs.PathCause, *, jumped: bool = True, sentence: str = "") -> rs.PathReadiness:
    route = rs.RouteChain(
        host=HOST,
        hostname=HOST,
        proxy_jump=(HOP,) if jumped else (),
        resolved=True,
    )
    return rs.PathReadiness(route=route, cause=cause, sentence=sentence)


def test_dead_hop_refuses_synchronously_with_the_named_cause() -> None:
    """The refusal the human reads at fire time — cause + what direct did."""
    readiness = _readiness(
        "hop_down_direct_ok", sentence=f"path dead (hop {HOP} down); direct alternative OK"
    )
    with pytest.raises(errors.SshUnreachable) as excinfo:
        path_gate.assert_path_clear_for_detach(
            HOST, run_id="run-abc", reader=lambda *a, **k: readiness
        )
    message = str(excinfo.value)
    assert "hop_down_direct_ok" in message, "the NAMED cause must lead the refusal"
    assert f"path dead (hop {HOP} down); direct alternative OK" in message
    assert "Do NOT fail over to a sibling" in message, "must not steer through the dead hop"
    assert "BEFORE detaching" in message


def test_healthy_path_passes_and_returns_the_reading() -> None:
    readiness = _readiness("path_ok", jumped=False)
    out = path_gate.assert_path_clear_for_detach(
        HOST, run_id="run-abc", reader=lambda *a, **k: readiness
    )
    assert out is readiness


def test_unresolved_route_fails_open() -> None:
    """A diagnosis layer must never be the reason a healthy submit refuses."""
    readiness = _readiness("route_unresolved")
    assert (
        path_gate.assert_path_clear_for_detach(
            HOST, run_id="run-abc", reader=lambda *a, **k: readiness
        )
        is readiness
    )


def test_a_raising_sensor_fails_open() -> None:
    def _boom(*_a: object, **_k: object) -> rs.PathReadiness:
        raise RuntimeError("ssh -G exploded")

    assert path_gate.assert_path_clear_for_detach(HOST, run_id="r", reader=_boom) is None


def test_env_opt_out_disables_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(path_gate.GATE_ENV, "0")
    called: list[int] = []

    def _reader(*_a: object, **_k: object) -> rs.PathReadiness:
        called.append(1)
        return _readiness("hop_down_direct_ok")

    assert path_gate.assert_path_clear_for_detach(HOST, run_id="r", reader=_reader) is None
    assert called == [], "the opt-out must skip sensing entirely, not just the raise"


def test_gate_consults_a_fresh_reading_instead_of_sensing() -> None:
    """Consult-first (s2-readiness pillars 1/3): a fresh L1 sweep is reused."""
    rs.record_readiness(_readiness("path_ok", jumped=False))
    sensed: list[int] = []

    def _reader(*_a: object, **_k: object) -> rs.PathReadiness:
        sensed.append(1)
        return _readiness("path_ok", jumped=False)

    out = path_gate.assert_path_clear_for_detach(
        HOST, run_id="r", reader=_reader, freshness_window_sec=600.0
    )
    assert out is not None
    assert sensed == [], "a fresh ledger reading must be reused, not re-sensed"


def test_gate_refuses_off_a_fresh_dead_reading_without_redialling() -> None:
    """The consulted reading is authoritative for refusal too, not just for pass."""
    rs.record_readiness(
        _readiness(
            "hop_down_direct_ok", sentence=f"path dead (hop {HOP} down); direct alternative OK"
        )
    )
    sensed: list[int] = []

    def _reader(*_a: object, **_k: object) -> rs.PathReadiness:
        sensed.append(1)
        return _readiness("path_ok")

    with pytest.raises(errors.SshUnreachable):
        path_gate.assert_path_clear_for_detach(
            HOST, run_id="r", reader=_reader, freshness_window_sec=600.0
        )
    assert sensed == []


def test_stale_reading_is_resensed() -> None:
    rs.record_readiness(_readiness("path_ok", jumped=False))
    sensed: list[int] = []

    def _reader(*_a: object, **_k: object) -> rs.PathReadiness:
        sensed.append(1)
        return _readiness("path_ok", jumped=False)

    path_gate.assert_path_clear_for_detach(
        HOST, run_id="r", reader=_reader, freshness_window_sec=0.0
    )
    assert sensed == [1], "a stale reading must be re-sensed, never trusted"
