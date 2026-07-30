"""The ``cluster-readiness`` verb (s2-readiness pillar 1's read surface).

Pins the four overall verdicts end to end through the real primitive, the
render's two non-negotiables (every atom carries its age; absence is RENDERED,
never omitted), determinism, and the fail-open posture — including that a
corrupt ledger is disclosed rather than fatal, and never green.

``now`` is injected on every call, so no assertion here depends on a wall clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.cluster_readiness import ClusterReadinessSpec
from hpc_agent.ops.cluster_readiness_op import cluster_readiness
from hpc_agent.state import readiness

HOST = "verb.example.edu"
T0 = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


def _iso(offset_sec: float = 0.0) -> str:
    return (T0 + timedelta(seconds=offset_sec)).isoformat(timespec="seconds")


def _run(experiment: Path, *, offset_sec: float = 0.0, **kw: Any) -> Any:
    return cluster_readiness(
        experiment_dir=experiment, spec=ClusterReadinessSpec(now=_iso(offset_sec), **kw)
    )


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


class TestVerdictRender:
    def test_unknown_when_nothing_was_ever_observed(self, experiment: Path) -> None:
        readiness.record_observation("other.example", readiness.CONNECT, "ok", source="s", now=T0)
        # Ask about a host that has no ledger at all.
        result = _run(experiment, host=HOST)
        assert result.clusters == []
        assert "(no clusters configured and no readiness ledger" in result.render

    def test_ready(self, experiment: Path) -> None:
        readiness.record_observation(
            HOST,
            readiness.CONNECT,
            "ok",
            source="ssh-circuit",
            route="effective",
            latency_ms=41,
            now=T0,
        )
        result = _run(experiment, host=HOST, offset_sec=30)
        entry = result.clusters[0]
        assert entry.verdict == "ready"
        assert result.counts["ready"] == 1
        assert "ready — every recorded invariant is green and fresh" in result.render
        # Sensor AND route: the route is part of the subject, not the evidence.
        assert "connect/effective: ok (30s ago · 41ms · via ssh-circuit)" in result.render

    def test_stale(self, experiment: Path) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="ssh-circuit", now=T0)
        horizon = readiness.stale_after_sec(readiness.CONNECT)
        result = _run(experiment, host=HOST, offset_sec=horizon + 60)
        assert result.clusters[0].verdict == "stale"
        assert "STALE (horizon 900s)" in result.render

    def test_degraded(self, experiment: Path) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="ssh-circuit", now=T0)
        readiness.record_observation(
            HOST,
            readiness.PREAMBLE,
            "timeout",
            source="ssh-circuit",
            route="effective",
            detail="the conda activation",
            now=T0,
        )
        result = _run(experiment, host=HOST, offset_sec=10)
        assert result.clusters[0].verdict == "degraded"
        assert "preamble/effective: timeout" in result.render
        assert "the conda activation" in result.render

    def test_every_verdict_word_comes_from_the_declared_vocabulary(self, experiment: Path) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "unknown", source="s", now=T0)
        result = _run(experiment, host=HOST, offset_sec=10)
        assert result.clusters[0].verdict in readiness.OVERALL_VERDICTS
        assert set(result.counts) == set(readiness.OVERALL_VERDICTS)


class TestRenderContract:
    def test_unfed_atoms_are_rendered_as_unknown_never_omitted(self, experiment: Path) -> None:
        """The one rule that stops an unfed invariant reading as a green one."""
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        result = _run(experiment, host=HOST, offset_sec=10)
        atoms = result.clusters[0].atoms
        # Every sensor kind appears exactly once: the fed one with its reading,
        # the rest as explicit unknown placeholders.
        assert sorted(a.sensor for a in atoms) == sorted(readiness.SENSOR_KINDS)
        for atom in atoms:
            if atom.sensor != readiness.CONNECT:
                assert atom.verdict == "unknown"
                assert atom.at is None
                assert f"{atom.sensor}: unknown (no observation recorded)" in result.render

    def test_every_recorded_atom_carries_an_age(self, experiment: Path) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        result = _run(experiment, host=HOST, offset_sec=3725)
        atom = next(a for a in result.clusters[0].atoms if a.sensor == readiness.CONNECT)
        assert atom.age_seconds == 3725
        assert "1h 2m ago" in result.render

    def test_a_multi_hop_chain_renders_one_row_per_element(self, experiment: Path) -> None:
        """The 2026-07-30 fix, read back off the ledger: a jumped host whose hop
        is dead can never read 'reachable' off the bare hostname, because the hop
        and the direct alternative are SEPARATE, separately-labelled subjects."""
        readiness.record_atoms(
            HOST,
            [
                {
                    "sensor": "hop",
                    "target": "usc-discovery",
                    "verdict": "down",
                    "route": "effective",
                },
                {"sensor": "direct", "target": HOST, "verdict": "ok", "route": "direct"},
                {"sensor": "path", "target": HOST, "verdict": "down", "route": "effective"},
            ],
            source="net-triage",
            now=T0,
        )
        result = _run(experiment, host=HOST, offset_sec=10)
        assert result.clusters[0].verdict == "degraded"
        assert "hop/effective → usc-discovery: down" in result.render
        assert "direct/direct → verb.example.edu: ok" in result.render
        assert "path/effective: down" in result.render

    def test_the_render_is_byte_identical_for_the_same_inputs(self, experiment: Path) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        a = _run(experiment, host=HOST, offset_sec=10)
        b = _run(experiment, host=HOST, offset_sec=10)
        assert a.render == b.render
        assert a.model_dump(mode="json") == b.model_dump(mode="json")

    def test_computed_at_dates_the_whole_projection(self, experiment: Path) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        result = _run(experiment, host=HOST, offset_sec=10)
        assert result.computed_at == _iso(10)
        assert result.computed_at in result.render


class TestFailOpen:
    def test_a_corrupt_ledger_is_disclosed_never_fatal_and_never_green(
        self, experiment: Path
    ) -> None:
        path = readiness.readiness_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Seed a real ledger first so the host is discoverable, then tear it.
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        path.write_text("{torn", encoding="utf-8")
        result = cluster_readiness(
            experiment_dir=experiment, spec=ClusterReadinessSpec(host=HOST, now=_iso(10))
        )
        # A host whose ledger is unreadable must stay VISIBLE — vanishing from
        # the report would read as "nothing to worry about here".
        entry = result.clusters[0]
        assert entry.host == HOST
        assert entry.ledger_corrupt is True
        assert entry.verdict == "unknown"
        assert all(atom.verdict == "unknown" for atom in entry.atoms)
        assert "ledger file could not be parsed" in result.render

    def test_a_corrupt_ledger_for_a_CONFIGURED_cluster_is_named(
        self, experiment: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hpc_agent.ops.cluster_readiness_op as op

        monkeypatch.setattr(op, "_configured_hosts", lambda: {"hoffman2": HOST})
        path = readiness.readiness_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{torn", encoding="utf-8")
        result = _run(experiment, offset_sec=10)
        entry = result.clusters[0]
        assert entry.cluster == "hoffman2"
        assert entry.ledger_corrupt is True
        assert entry.verdict == "unknown"
        assert "ledger file could not be parsed" in result.render

    def test_a_configured_but_never_contacted_cluster_shows_as_unknown(
        self, experiment: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Visible as unknown rather than missing — the whole point of unioning
        clusters.yaml into the scope."""
        import hpc_agent.ops.cluster_readiness_op as op

        monkeypatch.setattr(op, "_configured_hosts", lambda: {"discovery": "d.example"})
        result = _run(experiment)
        assert [(e.cluster, e.verdict) for e in result.clusters] == [("discovery", "unknown")]

    def test_an_unknown_cluster_key_is_not_refused(self, experiment: Path) -> None:
        result = _run(experiment, cluster="nope")
        assert result.clusters == []

    def test_user_at_host_in_the_spec_is_normalized(self, experiment: Path) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        result = _run(experiment, host=f"me@{HOST}", offset_sec=10)
        assert [e.host for e in result.clusters] == [HOST]

    def test_a_bad_now_override_is_spec_invalid(self, experiment: Path) -> None:
        with pytest.raises(errors.SpecInvalid):
            cluster_readiness(experiment_dir=experiment, spec=ClusterReadinessSpec(now="yesterday"))


class TestOrderingAndScope:
    def test_ledger_only_hosts_are_included_after_configured_ones(
        self, experiment: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hpc_agent.ops.cluster_readiness_op as op

        monkeypatch.setattr(op, "_configured_hosts", lambda: {"zeta": "z.example"})
        readiness.record_observation("a.example", readiness.CONNECT, "ok", source="s", now=T0)
        result = _run(experiment, offset_sec=10)
        # Sorted by (cluster or "", host): the ledger-only host has cluster=None
        # → sort key "" → first.
        assert [(e.cluster, e.host) for e in result.clusters] == [
            (None, "a.example"),
            ("zeta", "z.example"),
        ]

    def test_a_configured_host_is_not_duplicated_by_its_own_ledger(
        self, experiment: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hpc_agent.ops.cluster_readiness_op as op

        monkeypatch.setattr(op, "_configured_hosts", lambda: {"h2": HOST})
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        result = _run(experiment, offset_sec=10)
        assert [e.host for e in result.clusters] == [HOST]
