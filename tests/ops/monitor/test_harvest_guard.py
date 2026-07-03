"""Unit tests for the guaranteed terminal-harvest guard (design §5).

``harvest_on_terminal`` is the "no path ends in silence" guarantee: given
any terminal cause (or an abnormal loop exit) it best-effort-invokes the
metrics harvest + an error sweep and always records a durable, LOUD marker.
It must NEVER raise and NEVER mask the terminal cause.

These tests exercise the guard directly via its injected ``_aggregate`` /
``_sweep`` seams — no monitor loop, no cluster.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.ops.monitor.harvest_guard import (
    TERMINAL_CAUSES,
    harvest_marker_path,
    harvest_on_terminal,
)
from hpc_agent.state import run_record

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260703-090000-aaa"


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _read_markers(experiment_dir: Path, run_id: str) -> list[dict[str, Any]]:
    path = harvest_marker_path(experiment_dir, run_id)
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def _ok_aggregate(metrics: dict[str, Any], *, escalation: str | None = None, combiner: str = "/c"):
    def _agg(experiment_dir: Path, run_id: str) -> Any:
        return SimpleNamespace(
            aggregated_metrics=metrics,
            escalation_reason=escalation,
            combiner_dir_local=combiner,
        )

    return _agg


def test_happy_path_writes_marker_and_returns_ok(journal_home: Path, experiment: Path) -> None:
    """Metrics + sweep both succeed: harvest_ok, marker durable, sweep captured."""
    marker = harvest_on_terminal(
        experiment,
        _RUN_ID,
        terminal_cause="complete",
        _aggregate=_ok_aggregate({"r": {"acc": 0.9}}, escalation="empty_result_rows:tasks=3"),
        _sweep=lambda combiner_dir: {2: ["task 5 unreadable"]},
    )

    assert marker["harvest_ok"] is True
    assert marker["metrics_harvested"] is True
    assert marker["metrics_error"] is None
    assert marker["error_sweep_ran"] is True
    assert marker["aggregated_metric_keys"] == ["r"]
    assert marker["escalation_reason"] == "empty_result_rows:tasks=3"
    assert marker["waves_with_errors"] == {"2": ["task 5 unreadable"]}
    assert marker["terminal_cause"] == "complete"

    # Durable: the marker landed on disk verbatim.
    on_disk = _read_markers(experiment, _RUN_ID)
    assert len(on_disk) == 1
    assert on_disk[0]["harvest_ok"] is True
    assert on_disk[0]["waves_with_errors"] == {"2": ["task 5 unreadable"]}


def test_metrics_failure_is_loud_not_silent(journal_home: Path, experiment: Path) -> None:
    """A raising metrics harvest → recorded LOUD failure marker, never a raise.

    The sweep still runs (independent step) and the marker is still written.
    """

    def _boom_agg(experiment_dir: Path, run_id: str) -> Any:
        raise RuntimeError("ssh down")

    marker = harvest_on_terminal(
        experiment,
        _RUN_ID,
        terminal_cause="failed",
        _aggregate=_boom_agg,
        _sweep=lambda combiner_dir: {},
    )

    assert marker["metrics_harvested"] is False
    assert marker["metrics_error"] is not None
    assert "RuntimeError" in marker["metrics_error"]
    assert "ssh down" in marker["metrics_error"]
    # Sweep is independent — it still ran (over the default combiner dir).
    assert marker["error_sweep_ran"] is True
    # Overall harvest is flagged NOT ok (loud), not silently green.
    assert marker["harvest_ok"] is False
    # And it is durable.
    assert _read_markers(experiment, _RUN_ID)[0]["metrics_error"] == marker["metrics_error"]


def test_sweep_failure_is_recorded(journal_home: Path, experiment: Path) -> None:
    """A raising error sweep → recorded, harvest_ok False, still no raise."""

    def _boom_sweep(combiner_dir: str) -> dict[int, list[str]]:
        raise OSError("combiner dir vanished")

    marker = harvest_on_terminal(
        experiment,
        _RUN_ID,
        terminal_cause="abandoned",
        _aggregate=_ok_aggregate({}),
        _sweep=_boom_sweep,
    )

    assert marker["metrics_harvested"] is True
    assert marker["error_sweep_ran"] is False
    assert marker["error_sweep_error"] is not None
    assert "OSError" in marker["error_sweep_error"]
    assert marker["harvest_ok"] is False


def test_never_raises_even_when_both_seams_raise(journal_home: Path, experiment: Path) -> None:
    """Both steps raise → the guard still returns a marker, never propagates."""

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("kaboom")

    # Must not raise.
    marker = harvest_on_terminal(
        experiment,
        _RUN_ID,
        terminal_cause="partial-kill",
        _aggregate=_boom,
        _sweep=_boom,
    )
    assert marker["harvest_ok"] is False
    assert marker["metrics_error"] is not None
    assert marker["error_sweep_error"] is not None


def test_marker_is_append_only_across_calls(journal_home: Path, experiment: Path) -> None:
    """Re-arming a run accretes markers rather than clobbering (idempotent)."""
    for cause in ("timeout", "complete"):
        harvest_on_terminal(
            experiment,
            _RUN_ID,
            terminal_cause=cause,
            _aggregate=_ok_aggregate({}),
            _sweep=lambda combiner_dir: {},
        )
    markers = _read_markers(experiment, _RUN_ID)
    assert [m["terminal_cause"] for m in markers] == ["timeout", "complete"]


def test_terminal_causes_vocabulary_covers_design_terms() -> None:
    """The named §5 terminal causes are all present in the vocabulary."""
    assert {
        "complete",
        "failed",
        "timeout",
        "cap-overrun",
        "abandoned",
        "partial-kill",
        "abnormal-exit",
    } <= TERMINAL_CAUSES
