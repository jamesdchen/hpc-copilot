"""A broken INVARIANT reaches the human (s2-readiness pillar 3, consumer half).

Pillar 3 gave ``auth`` / ``scratch`` / ``scheduler`` / ``env`` real sensors and
real feed sites. The claim this module checks is the one that makes that worth
anything: an atom from any of them travels the whole way — durable ledger →
overall verdict → ``cluster-readiness`` render — and DEGRADES the cluster, with
its age and its subject visible, without a single line of consumer code knowing
those kinds exist.

That "without a line of code" is the load-bearing part and the reason these are
end-to-end fixtures rather than unit assertions: the vocabulary was extended in
ONE place (``SensorKind``, mirrored by ``state/readiness.SENSOR_KINDS``), and if
any consumer had grown its own parallel list, a scratch failure would render as
nothing at all while the cluster read ``ready``. A silent green is the precise
failure this substrate exists to prevent, so it gets a test that would notice.

``now`` is injected on every call — no assertion here depends on a wall clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from hpc_agent._wire.queries.cluster_readiness import ClusterReadinessSpec
from hpc_agent.ops.cluster_readiness_op import cluster_readiness
from hpc_agent.state import readiness
from tests._no_network import no_network  # noqa: F401 — autouse zero-network tripwire

HOST = "invariant.example.edu"
SCRATCH = "/u/scratch/someone"
T0 = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _run(experiment: Path, *, offset_sec: float = 0.0, **kw: Any) -> Any:
    return cluster_readiness(
        experiment_dir=experiment,
        spec=ClusterReadinessSpec(now=_at(offset_sec).isoformat(timespec="seconds"), **kw),
    )


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _healthy_transport() -> None:
    """The ``connect`` atom every host accumulates — the REQUIRED sensor.

    Present in each fixture below so the cluster would read ``ready`` if the
    invariant atom under test were ignored. Without it every case would read
    ``stale`` for a missing required sensor and prove nothing about the invariant.
    """
    readiness.record_observation(
        HOST, readiness.CONNECT, "ok", source="ssh-circuit", route="effective", now=T0
    )


# ── the overall verdict degrades ─────────────────────────────────────────────


def test_a_fresh_scratch_down_degrades_a_host_whose_transport_is_fine() -> None:
    """Transport green + storage broken is DEGRADED, not ready. The whole point of
    naming invariants separately: a reachable login node is not a usable cluster.
    """
    _healthy_transport()
    readiness.record_observation(
        HOST,
        readiness.SCRATCH,
        "down",
        source="submit-flow",
        target=SCRATCH,
        route="effective",
        detail="rsync push exit 11: No space left on device",
        now=T0,
    )
    doc = readiness.read_ledger(HOST)
    assert readiness.overall_verdict(doc, now=_at(60)) == "degraded"
    # ...and it really was the SCRATCH atom that did it: the same ledger minus
    # that one row reads ``ready``, so this pins the invariant rather than
    # re-pinning the transport verdict under a new name.
    transport_only = [a for a in doc["atoms"] if a["sensor"] == readiness.CONNECT]
    assert (
        readiness.overall_verdict(
            {"schema_version": readiness.SCHEMA_VERSION, "atoms": transport_only}, now=_at(60)
        )
        == "ready"
    )


def test_a_fresh_scheduler_down_degrades() -> None:
    _healthy_transport()
    readiness.record_observation(
        HOST,
        readiness.SCHEDULER,
        "down",
        source="submit-flow",
        target="sge",
        route="effective",
        detail="array dispatch failed: qsub: Unauthorized Request",
        now=T0,
    )
    assert readiness.overall_verdict(readiness.read_ledger(HOST), now=_at(60)) == "degraded"


def test_an_auth_down_degrades() -> None:
    """The invariant that had NO sensor before pillar 3 — the breaker structurally
    could not feed it, so this row could never appear."""
    _healthy_transport()
    readiness.record_observation(
        HOST,
        readiness.AUTH,
        "down",
        source="readiness-sensors",
        route="effective",
        detail="the host answered and REFUSED the credentials: Permission denied (publickey)",
        now=T0,
    )
    assert readiness.overall_verdict(readiness.read_ledger(HOST), now=_at(60)) == "degraded"


def test_a_STALE_scratch_failure_reads_stale_not_degraded() -> None:
    """The host may have healed and nothing has looked since. Fencing a cluster on
    expired evidence is the mistake the whole freshness axis exists to avoid — and
    it must hold for the new kinds exactly as it does for transport.
    """
    _healthy_transport()
    readiness.record_observation(
        HOST, readiness.SCRATCH, "down", source="submit-flow", target=SCRATCH, now=T0
    )
    horizon = readiness.stale_after_sec(readiness.SCRATCH)
    assert readiness.overall_verdict(readiness.read_ledger(HOST), now=_at(horizon + 60)) == "stale"


def test_env_carries_its_OWN_much_longer_horizon() -> None:
    """A wheel changes on a deliberate reinstall — hours, not minutes. Holding the
    env fingerprint to the transport horizon would pin every ledger permanently
    stale for no evidence gain, so its horizon is separate and must stay so.
    """
    assert readiness.stale_after_sec(readiness.ENV) > readiness.stale_after_sec(readiness.SCRATCH)
    _healthy_transport()
    readiness.record_observation(
        HOST, readiness.ENV, "down", source="submit-flow", route="effective", now=T0
    )
    # Past the transport horizon but well inside env's: still a LIVE failure.
    assert (
        readiness.overall_verdict(
            readiness.read_ledger(HOST), now=_at(readiness.DEFAULT_STALE_AFTER_SEC + 60)
        )
        == "degraded"
    )


# ── the render shows it ──────────────────────────────────────────────────────


def test_a_scratch_down_renders_with_its_subject_age_and_provenance(
    experiment: Path,
) -> None:
    _healthy_transport()
    readiness.record_observation(
        HOST,
        readiness.SCRATCH,
        "down",
        source="submit-flow",
        target=SCRATCH,
        route="effective",
        detail="rsync push exit 11: No space left on device",
        now=T0,
    )
    result = _run(experiment, host=HOST, offset_sec=120)
    entry = result.clusters[0]
    assert entry.verdict == "degraded"
    assert result.counts["degraded"] == 1
    assert "degraded — a fresh observation says an invariant is broken" in result.render
    # The full subject: sensor/route → target. Two scratch roots on one cluster
    # must never render as one line.
    assert f"scratch/effective → {SCRATCH}: down" in result.render
    assert "2m ago" in result.render
    assert "via submit-flow" in result.render
    assert "No space left on device" in result.render


def test_a_scheduler_down_renders_under_its_backend_family(experiment: Path) -> None:
    _healthy_transport()
    readiness.record_observation(
        HOST,
        readiness.SCHEDULER,
        "down",
        source="submit-flow",
        target="sge",
        route="effective",
        detail="array dispatch failed: qsub: Unauthorized Request",
        now=T0,
    )
    result = _run(experiment, host=HOST, offset_sec=45)
    assert result.clusters[0].verdict == "degraded"
    assert "scheduler/effective → sge: down" in result.render
    assert "45s ago" in result.render
    assert "Unauthorized Request" in result.render


def test_an_unfed_invariant_renders_unknown_rather_than_vanishing(experiment: Path) -> None:
    """Absence is EMITTED. An invariant nobody fed must never be mistaken for a
    green one — which is exactly what omitting the row would achieve.
    """
    _healthy_transport()
    render = _run(experiment, host=HOST, offset_sec=10).render
    for sensor in (readiness.AUTH, readiness.SCRATCH, readiness.SCHEDULER, readiness.ENV):
        assert f"{sensor}: unknown (no observation recorded)" in render


def test_every_sensor_kind_is_renderable(experiment: Path) -> None:
    """The vocabulary is extended in ONE place. A consumer holding a parallel list
    would silently drop the new kinds — a broken invariant rendering as nothing at
    all while the host still reads green.
    """
    for index, sensor in enumerate(readiness.SENSOR_KINDS):
        readiness.record_observation(
            HOST, sensor, "ok", source="test", target=f"t{index}", route="effective", now=T0
        )
    result = _run(experiment, host=HOST, offset_sec=5)
    for index, sensor in enumerate(readiness.SENSOR_KINDS):
        assert f"{sensor}/effective → t{index}: ok" in result.render
    assert "unknown (no observation recorded)" not in result.render
    assert result.clusters[0].verdict == "ready"


def test_the_invariants_render_AFTER_the_transport_legs(experiment: Path) -> None:
    """Render order is position in ``SENSOR_KINDS``: transport first, invariants
    after — the order a human diagnoses in (a dead hop explains a dead scratch,
    never the reverse)."""
    for sensor in readiness.SENSOR_KINDS:
        readiness.record_observation(
            HOST, sensor, "ok", source="test", target="t", route="effective", now=T0
        )
    render = _run(experiment, host=HOST, offset_sec=5).render
    positions = [render.index(f"{sensor}/effective") for sensor in readiness.SENSOR_KINDS]
    assert positions == sorted(positions)
