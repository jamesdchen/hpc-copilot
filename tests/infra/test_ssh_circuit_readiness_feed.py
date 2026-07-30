"""The circuit breaker's readiness feed (s2-readiness pillar 1).

The feed's whole contract is **harvest, never probe**: the breaker's existing
record sites hand the standing ledger an outcome they already classified, adding
no network call and no measurement beyond the attempt's own duration. These
tests pin that the atoms actually land, that they land with the injected clock's
instant (so the ledger is as deterministic as the breaker), that the feed reads
the breaker's OWN degradation verdict rather than recomputing one, and that a
broken ledger can never perturb SSH.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Any

import pytest

from hpc_agent.infra import ssh_circuit
from hpc_agent.state import readiness
from tests._no_network import no_network  # noqa: F401 — autouse zero-network tripwire

HOST = "feed.example.edu"
TARGET = f"someone@{HOST}"
#: A fixed wall instant the breaker's injectable clock returns, so every atom
#: stamp below is exact rather than "about now".
EPOCH = 1785412800.0  # 2026-07-30T20:00:00Z
INSTANT = datetime.fromtimestamp(EPOCH, tz=timezone.utc)


class FakeClock:
    """The breaker's test clock idiom: a stable, advanceable wall clock."""

    def __init__(self, start: float = EPOCH) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _cp(
    *, stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    """A real ``CompletedProcess`` — the exact type ``guarded_call`` classifies."""
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _atoms() -> dict[str, Any]:
    """The ledger's atoms keyed by sensor.

    The breaker only ever writes over the ``effective`` route against the target
    itself, so ``sensor`` alone is a unique key HERE — the durable ledger's real
    identity is ``(sensor, route, target)``, which the sensor layer's multi-hop
    readings need.
    """
    atoms = readiness.read_ledger(HOST)["atoms"]
    assert isinstance(atoms, list)
    keyed = {str(atom["sensor"]): atom for atom in atoms}
    assert len(keyed) == len(atoms), "the breaker must write one atom per sensor"
    return keyed


class TestSuccessFeedsConnect:
    def test_a_success_records_a_connect_ok_at_the_injected_instant(self) -> None:
        clock = FakeClock()
        ssh_circuit.record_connection_success(TARGET, clock=clock, latency_ms=37)
        atom = _atoms()[readiness.CONNECT]
        assert atom["verdict"] == "ok"
        assert atom["source"] == "ssh-circuit"
        assert atom["latency_ms"] == 37
        assert readiness.atom_age_sec(atom, now=INSTANT) == 0.0

    def test_the_atom_is_recorded_over_the_EFFECTIVE_route(self) -> None:
        """The breaker dials whatever ssh resolves, hops included — which is the
        route axis a later ``direct`` sensor reading discriminates against."""
        ssh_circuit.record_connection_success(TARGET, clock=FakeClock())
        atom = _atoms()[readiness.CONNECT]
        assert atom["route"] == "effective"
        assert atom["target"] == HOST

    def test_the_hot_path_early_return_still_feeds(self) -> None:
        """The steady healthy state is the case ``record_connection_success``
        returns from EARLIEST — and it is exactly the evidence the standing
        ledger exists to accumulate, so the feed must precede that return."""
        clock = FakeClock()
        # No breaker doc at all ⇒ the function returns before touching state.
        assert not ssh_circuit.circuit_state_path(HOST).exists()
        ssh_circuit.record_connection_success(TARGET, clock=clock)
        assert _atoms()[readiness.CONNECT]["verdict"] == "ok"

    def test_success_never_feeds_an_auth_atom(self) -> None:
        """This seam folds 'auth rejected but the host answered' into SUCCESS, so
        an auth atom fed from here would assert what the evidence cannot."""
        ssh_circuit.record_connection_success(TARGET, clock=FakeClock())
        assert readiness.AUTH not in _atoms()

    def test_the_ledger_keys_on_the_host_not_the_user_at_host(self) -> None:
        ssh_circuit.record_connection_success(TARGET, clock=FakeClock())
        assert readiness.read_ledger(HOST)["host"] == HOST


class TestFailureFeedsConnect:
    def test_a_failure_records_a_connect_failure_with_the_detail(self) -> None:
        clock = FakeClock()
        ssh_circuit.record_connection_failure(
            TARGET, detail="connection timed out", clock=clock, latency_ms=15000
        )
        atom = _atoms()[readiness.CONNECT]
        assert atom["detail"] == "connection timed out"
        assert atom["latency_ms"] == 15000
        assert readiness.atom_age_sec(atom, now=INSTANT) == 0.0

    @pytest.mark.parametrize(
        ("detail", "expected"),
        [
            ("connection timed out", "timeout"),
            ("ssh to h timed out after 60s: uname -a", "timeout"),
            ("connection refused: ", "down"),
            ("connection reset by peer: ", "down"),
        ],
    )
    def test_the_verdict_splits_timeout_from_down_on_evidence_already_recorded(
        self, detail: str, expected: str
    ) -> None:
        """The sensor layer's vocabulary distinguishes the two because they have
        different remediations, and the breaker's own detail already says which
        happened — no new classification is invented here."""
        ssh_circuit.record_connection_failure(TARGET, detail=detail, clock=FakeClock())
        assert _atoms()[readiness.CONNECT]["verdict"] == expected

    def test_a_failure_after_a_success_flips_the_atom_inside_the_coalesce_window(
        self,
    ) -> None:
        """Coalescing is for UNCHANGED atoms only — a flip must never be
        swallowed, however recent the previous observation."""
        clock = FakeClock()
        ssh_circuit.record_connection_success(TARGET, clock=clock)
        clock.advance(1.0)
        ssh_circuit.record_connection_failure(TARGET, detail="refused", clock=clock)
        assert _atoms()[readiness.CONNECT]["verdict"] == "down"


class TestPreambleFeed:
    def test_the_run13_livelock_records_a_preamble_timeout_atom(self) -> None:
        """The breaker already classifies this (probe-OK, ``module load`` times
        out, N re-open cycles in one incident window). The feed passes THAT
        verdict on; it never recomputes or re-probes one."""
        clock = FakeClock()
        detail = "ssh to h timed out after 60s: module load conda && source x/conda.sh"
        # Trip the circuit, then fail the half-open probe enough times to cross
        # DEGRADATION_CYCLE_THRESHOLD inside one incident window.
        for _ in range(ssh_circuit.CIRCUIT_THRESHOLD):
            ssh_circuit.record_connection_failure(TARGET, detail=detail, clock=clock)
        for _ in range(ssh_circuit.DEGRADATION_CYCLE_THRESHOLD):
            clock.advance(ssh_circuit.CYCLE3_PLUS_COOLDOWN_SEC + 1)
            ssh_circuit.check_circuit(TARGET, clock=clock)  # claims the probe slot
            ssh_circuit.record_connection_failure(TARGET, detail=detail, clock=clock)

        doc = ssh_circuit._read_doc(ssh_circuit.circuit_state_path(HOST))
        assert ssh_circuit.is_preamble_degraded(doc, now=clock())

        atom = _atoms()[readiness.PREAMBLE]
        # A timeout, not a "down": the connection probe keeps SUCCEEDING here.
        # Naming it down would claim a refusal nothing observed.
        assert atom["verdict"] == "timeout"
        assert atom["route"] == "effective"
        assert "conda" in atom["detail"]

    def test_an_ordinary_failure_feeds_no_preamble_atom(self) -> None:
        ssh_circuit.record_connection_failure(TARGET, detail="refused", clock=FakeClock())
        assert readiness.PREAMBLE not in _atoms()


class TestFeedIsNeverAGateOnSsh:
    def test_a_raising_ledger_does_not_break_the_breaker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> bool:
            raise RuntimeError("ledger on fire")

        monkeypatch.setattr(readiness, "record_observation", _boom)
        clock = FakeClock()
        # Both recorders must still do their real job.
        ssh_circuit.record_connection_failure(TARGET, detail="x", clock=clock)
        doc = ssh_circuit._read_doc(ssh_circuit.circuit_state_path(HOST))
        assert doc is not None and doc["consecutive_failures"] == 1
        ssh_circuit.record_connection_success(TARGET, clock=clock)
        doc = ssh_circuit._read_doc(ssh_circuit.circuit_state_path(HOST))
        assert doc is not None and doc["consecutive_failures"] == 0


class TestGuardedCallEndToEnd:
    def test_a_guarded_success_leaves_a_ready_ledger(self) -> None:
        clock = FakeClock()
        cp = _cp(stdout="", stderr="", returncode=0)
        ssh_circuit.guarded_call(TARGET, lambda: cp, clock=clock, sleep=lambda _s: None)
        doc = readiness.read_ledger(HOST)
        assert readiness.overall_verdict(doc, now=INSTANT) == "ready"
        # The one thing this seam measures that the breaker does not: the
        # attempt's own duration, which is a real (tiny) number, never None.
        assert isinstance(_atoms()[readiness.CONNECT]["latency_ms"], float)

    def test_a_guarded_connection_failure_leaves_a_degraded_ledger(self) -> None:
        clock = FakeClock()
        cp = _cp(stdout="", stderr="connection refused", returncode=255)
        ssh_circuit.guarded_call(TARGET, lambda: cp, clock=clock, sleep=lambda _s: None)
        doc = readiness.read_ledger(HOST)
        assert readiness.overall_verdict(doc, now=INSTANT) == "degraded"

    def test_an_inconclusive_outcome_records_no_atom(self) -> None:
        """rc != 255 non-zero is not transport evidence either way — the breaker
        records neither, and so must the ledger. Silence beats a wrong verdict."""
        clock = FakeClock()
        cp = _cp(stdout="", stderr="qsub: bad job", returncode=1)
        ssh_circuit.guarded_call(TARGET, lambda: cp, clock=clock, sleep=lambda _s: None)
        assert _atoms() == {}
