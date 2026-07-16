"""Shared fixtures for the monitor test package.

The guaranteed terminal-harvest (design §5) added to ``monitor_flow``
invokes ``harvest_on_terminal`` on EVERY terminal path AND on abnormal
loop exit. Its default metrics-harvest seam does a real ``aggregate-flow``
(SSH + rsync). Left un-stubbed, every monitor-flow test that reaches a
terminal branch or breaks the loop via a sentinel exception would attempt
a live cluster connection.

This autouse fixture stubs the two harvest seams
(``harvest_guard._default_aggregate`` / ``_default_sweep``) with
cluster-free fakes, so the WHOLE monitor test package exercises the real
guard — control flow, marker writes, loud logging — without touching a
cluster. Tests that assert on harvest *behavior* override
``monitor_flow.harvest_on_terminal`` (with a recorder) or these seams
(with failing fakes) in their own body; a per-test ``monkeypatch.setattr``
runs after this fixture and wins.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

# ── monitor harvest/announce counters (latency-elimination Unit 1.0) ──────────
# The exec/dial counters for the ``ssh_run`` seam live in ``tests/_ssh_fakes.py``
# (``FakeSSH.exec_count`` / ``.dials_by_host`` / ``.mark`` / ``.execs_since``).
# This conftest owns the monitor-package seams that DON'T go through ``ssh_run``:
# the terminal-harvest cycle (``harvest_guard._default_aggregate``/``_default_sweep``)
# and the crash-only announce census read (``read_announcements``). The recorder
# below counts both read-only so the P3/F4 census-fold units can assert e.g.
# "fleet-of-3 census = 1 read/tick" and "marker write wakes with zero intervening
# reads". Landed ONCE; a missing shape goes to the integrator, never grown on a
# unit branch.


@dataclass
class MonitorIOCounter:
    """Read-only tally of the stubbed monitor cluster-IO seams for a test.

    Every monitor test gets one (the autouse harvest/announce stubs record into
    it); a test that wants to assert on counts just adds ``monitor_io_counter``
    to its signature and reads the SAME instance. Recording is side-effect-only
    — the stubs' return values are unchanged, so existing tests stay green.
    """

    harvest_cycles: int = 0
    announce_reads: list[str] = field(default_factory=list)

    @property
    def announce_read_count(self) -> int:
        """Total ``read_announcements`` census reads dispatched."""
        return len(self.announce_reads)

    @property
    def announce_reads_by_host(self) -> Counter[str]:
        """Per-host census-read breakdown — ``{ssh_target: n}``."""
        return Counter(self.announce_reads)

    def mark(self) -> int:
        """Snapshot the announce-read count for a window measurement.

        Pair with :meth:`announce_reads_since` to assert "zero intervening reads"
        across a marker wait.
        """
        return len(self.announce_reads)

    def announce_reads_since(self, mark: int) -> int:
        """How many census reads landed since *mark*."""
        return len(self.announce_reads) - mark


@pytest.fixture
def monitor_io_counter() -> MonitorIOCounter:
    """The per-test monitor cluster-IO tally (see :class:`MonitorIOCounter`)."""
    return MonitorIOCounter()


@pytest.fixture(autouse=True)
def _cluster_free_harvest(
    monkeypatch: pytest.MonkeyPatch, monitor_io_counter: MonitorIOCounter
) -> None:
    from hpc_agent.ops.monitor import harvest_guard

    def _aggregate(experiment_dir, run_id):  # noqa: ANN001, ANN202
        monitor_io_counter.harvest_cycles += 1
        return SimpleNamespace(
            aggregated_metrics={},
            escalation_reason=None,
            combiner_dir_local=None,
        )

    monkeypatch.setattr(harvest_guard, "_default_aggregate", _aggregate)
    monkeypatch.setattr(
        harvest_guard,
        "_default_sweep",
        lambda combiner_dir: {},
    )


@pytest.fixture(autouse=True)
def _no_announcements(
    monkeypatch: pytest.MonkeyPatch, monitor_io_counter: MonitorIOCounter
) -> None:
    """Default the crash-only announce fast path to "no markers" package-wide.

    Both announce consumers read the cluster's per-task announcement markers in
    one ssh exec (``docs/design/crash-only-monitoring.md``): ``reconcile``
    (Phase 1, BEFORE its heavy 3-way probe) and the ``monitor_flow`` poll loop
    (Phase 2, announce-first over the per-task reporter walk). Left un-stubbed,
    every existing reconcile/flow test would hit a real ``read_announcements``
    ssh call. This autouse fixture stubs BOTH module references to report a
    NOT-PRESENT census (``present == 0``), so the legacy probe / reporter-walk
    paths run byte-identically (no announce dir == fall through). Tests that
    exercise the announce path override ``<module>.read_announcements`` (with
    counts) in their own body; a per-test ``monkeypatch.setattr`` runs after
    this and wins.

    The stub also records each read's ``ssh_target`` into ``monitor_io_counter``
    (side-effect-only — the returned census is unchanged) so the census-fold
    units can assert per-tick / per-host read counts read-only (Unit 1.0).
    """
    from hpc_agent.ops import monitor_flow
    from hpc_agent.ops.monitor import reconcile

    def _absent(*, ssh_target, remote_path, run_id, task_count):
        monitor_io_counter.announce_reads.append(ssh_target)
        return {
            "present": 0,
            "announced": 0,
            "complete": 0,
            "failed": 0,
            "missing": max(0, int(task_count)),
        }

    monkeypatch.setattr(reconcile, "read_announcements", _absent)
    monkeypatch.setattr(monitor_flow, "read_announcements", _absent)


@pytest.fixture(autouse=True)
def _no_census_scheduler_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the census marker-lifecycle cross-checks to "unavailable".

    ``monitor_flow``'s announce census gained two fail-closed ssh cross-checks:
    ``_census_liveness_probe`` (F17/F23 — one scheduler alive-probe when the
    census is inconclusive) and ``_census_complete_task_ids`` (F28 — a
    complete-marker listing for wave bookkeeping). Left un-stubbed, any flow test
    that reaches the census leg with a partial/failed census (or with
    ``auto_combine_waves``) would attempt a real cluster connection. This autouse
    fixture defaults BOTH to the fail-closed "could not probe" result (``None``),
    so every existing test runs byte-identically (no probe == no census
    adjustment). Tests that exercise a cross-check override the relevant module
    reference in their own body; a per-test ``monkeypatch.setattr`` wins.
    """
    from hpc_agent.ops import monitor_flow

    monkeypatch.setattr(monitor_flow, "_census_liveness_probe", lambda record: None)
    monkeypatch.setattr(monitor_flow, "_census_complete_task_ids", lambda record, run_id: None)
