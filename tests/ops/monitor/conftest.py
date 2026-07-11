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

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _cluster_free_harvest(monkeypatch: pytest.MonkeyPatch) -> None:
    from hpc_agent.ops.monitor import harvest_guard

    monkeypatch.setattr(
        harvest_guard,
        "_default_aggregate",
        lambda experiment_dir, run_id: SimpleNamespace(
            aggregated_metrics={},
            escalation_reason=None,
            combiner_dir_local=None,
        ),
    )
    monkeypatch.setattr(
        harvest_guard,
        "_default_sweep",
        lambda combiner_dir: {},
    )


@pytest.fixture(autouse=True)
def _no_announcements(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the crash-only announce fast path to "no markers" package-wide.

    ``reconcile._reconcile_one`` now reads the cluster's per-task announcement
    markers in one ssh exec BEFORE the heavy 3-way probe
    (``docs/design/crash-only-monitoring.md``). Left un-stubbed, every existing
    reconcile/flow test would hit a real ``read_announcements`` ssh call. This
    autouse fixture stubs it to report ZERO announcements, so the legacy probe
    path runs byte-identically (zero markers == fall through). Tests that
    exercise the fast path override ``reconcile.read_announcements`` (with counts)
    in their own body; a per-test ``monkeypatch.setattr`` runs after this and wins.
    """
    from hpc_agent.ops.monitor import reconcile

    monkeypatch.setattr(
        reconcile,
        "read_announcements",
        lambda *, ssh_target, remote_path, run_id, task_count: {
            "announced": 0,
            "complete": 0,
            "failed": 0,
            "missing": max(0, int(task_count)),
        },
    )
