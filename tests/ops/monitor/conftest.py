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
