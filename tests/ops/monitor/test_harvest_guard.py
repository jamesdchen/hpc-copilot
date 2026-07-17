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

from hpc_agent.errors import ScopeLocked, SshCircuitOpen
from hpc_agent.ops.monitor.harvest_guard import (
    CIRCUIT_WAIT_CAP_SEC,
    TERMINAL_CAUSES,
    harvest_marker_path,
    harvest_on_terminal,
)

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260703-090000-aaa"


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
        _sweep=lambda combiner_dir, run_id: {2: ["task 5 unreadable"]},
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
        _sweep=lambda combiner_dir, run_id: {},
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

    def _boom_sweep(combiner_dir: str, run_id: str) -> dict[int, list[str]]:
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
            _sweep=lambda combiner_dir, run_id: {},
        )
    markers = _read_markers(experiment, _RUN_ID)
    assert [m["terminal_cause"] for m in markers] == ["timeout", "complete"]


def _circuit_exc(deadline: float | None) -> SshCircuitOpen:
    exc = SshCircuitOpen("ssh circuit for host 'h' is OPEN (test)")
    exc.host = "h"
    exc.deadline = deadline
    return exc


class TestCircuitOpenBoundedRetry:
    """``SshCircuitOpen`` names its own deadline — the guard waits it out ONCE.

    2026-07-06: a 3×60s hoffman2 latency spike opened the breaker mid-harvest;
    the guard recorded ``harvest_ok:false`` and parked a finished 20/20 run
    292s short of the deadline the exception itself carried. A detached
    terminal worker has nowhere else to be: within one BASE cooldown it must
    sleep to the deadline and retry once (the sanctioned half-open probe).
    A doubled cooldown or a deadline-less error records as before — waiting
    there would ride a genuinely unhealthy host.
    """

    _NOW = 1000.0

    def test_near_deadline_waits_and_retries_once(
        self, journal_home: Path, experiment: Path
    ) -> None:
        calls: list[int] = []
        sleeps: list[float] = []

        def _agg(experiment_dir: Path, run_id: str) -> Any:
            calls.append(1)
            if len(calls) == 1:
                raise _circuit_exc(self._NOW + 120.0)
            return SimpleNamespace(
                aggregated_metrics={"r": {"pi": 3.14}},
                escalation_reason=None,
                combiner_dir_local="/c",
            )

        marker = harvest_on_terminal(
            experiment,
            _RUN_ID,
            terminal_cause="complete",
            _aggregate=_agg,
            _sweep=lambda combiner_dir, run_id: {},
            _clock=lambda: self._NOW,
            _sleep=sleeps.append,
        )

        assert len(calls) == 2
        # Slept past the deadline (remaining 120s + slack), recorded loudly.
        assert sleeps == [pytest.approx(125.0)]
        assert marker["circuit_waited_sec"] == pytest.approx(125.0)
        assert marker["metrics_harvested"] is True
        assert marker["metrics_error"] is None
        assert marker["harvest_ok"] is True

    def test_doubled_cooldown_records_without_waiting(
        self, journal_home: Path, experiment: Path
    ) -> None:
        """A remaining cooldown past the cap = the half-open probe already
        failed; the guard must NOT ride an unhealthy host."""
        calls: list[int] = []

        def _agg(experiment_dir: Path, run_id: str) -> Any:
            calls.append(1)
            raise _circuit_exc(self._NOW + CIRCUIT_WAIT_CAP_SEC + 1.0)

        marker = harvest_on_terminal(
            experiment,
            _RUN_ID,
            terminal_cause="complete",
            _aggregate=_agg,
            _sweep=lambda combiner_dir, run_id: {},
            _clock=lambda: self._NOW,
            _sleep=lambda s: pytest.fail(f"guard slept {s}s on a doubled cooldown"),
        )

        assert len(calls) == 1
        assert marker["circuit_waited_sec"] is None
        assert marker["metrics_harvested"] is False
        assert marker["metrics_error"] is not None
        assert "SshCircuitOpen" in marker["metrics_error"]

    def test_deadline_less_error_records_without_waiting(
        self, journal_home: Path, experiment: Path
    ) -> None:
        """Bare construction (older sites, tests) carries no deadline → no wait."""

        def _agg(experiment_dir: Path, run_id: str) -> Any:
            raise _circuit_exc(None)

        marker = harvest_on_terminal(
            experiment,
            _RUN_ID,
            terminal_cause="failed",
            _aggregate=_agg,
            _sweep=lambda combiner_dir, run_id: {},
            _clock=lambda: self._NOW,
            _sleep=lambda s: pytest.fail(f"guard slept {s}s with no deadline"),
        )

        assert marker["circuit_waited_sec"] is None
        assert marker["metrics_harvested"] is False
        assert "SshCircuitOpen" in (marker["metrics_error"] or "")

    def test_retry_failure_is_recorded_with_no_third_attempt(
        self, journal_home: Path, experiment: Path
    ) -> None:
        """The retry is ONCE: a second failure records loudly and stops."""
        calls: list[int] = []

        def _agg(experiment_dir: Path, run_id: str) -> Any:
            calls.append(1)
            raise _circuit_exc(self._NOW + 60.0)

        marker = harvest_on_terminal(
            experiment,
            _RUN_ID,
            terminal_cause="complete",
            _aggregate=_agg,
            _sweep=lambda combiner_dir, run_id: {},
            _clock=lambda: self._NOW,
            _sleep=lambda s: None,
        )

        assert len(calls) == 2
        assert marker["circuit_waited_sec"] == pytest.approx(65.0)
        assert marker["metrics_harvested"] is False
        assert marker["metrics_error"] is not None
        assert marker["harvest_ok"] is False


def test_harvest_guard_records_locked_skip_not_failure(
    journal_home: Path, experiment: Path
) -> None:
    """A ScopeLocked reduction is a CLEAN SKIP, not a harvest failure.

    A locked scope is deliberate human state (the scope gate refused on
    purpose). The guard must record a skip reason, keep ``harvest_ok`` clean
    (never False), never set ``metrics_error`` (no anomaly), and not sweep.
    """

    def _locked_agg(experiment_dir: Path, run_id: str) -> Any:
        raise ScopeLocked.for_tag("holdout", locked_at="2026-07-06T12:00:00+00:00")

    def _must_not_sweep(combiner_dir: str, run_id: str) -> dict[int, list[str]]:
        raise AssertionError("sweep must not run on a clean scope-locked skip")

    marker = harvest_on_terminal(
        experiment,
        _RUN_ID,
        terminal_cause="complete",
        _aggregate=_locked_agg,
        _sweep=_must_not_sweep,
    )

    # Clean skip: recorded reason, not painted red, not an anomaly.
    assert marker["harvest_skipped_reason"] == "scope_locked"
    assert marker["harvest_ok"] is not False
    assert marker["metrics_harvested"] is False
    assert marker["metrics_error"] is None
    assert marker["error_sweep_ran"] is False
    assert marker["error_sweep_error"] is None
    # Durable on disk.
    on_disk = _read_markers(experiment, _RUN_ID)
    assert on_disk[-1]["harvest_skipped_reason"] == "scope_locked"


def test_write_marker_swallows_seam_oserror_and_harvest_continues(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2: the marker write routes through the flock+fsync append seam, which CAN
    raise OSError — the never-raise wrapper must swallow it (the guard runs from a
    caller's ``finally`` and must not mask the terminal cause).
    """
    import hpc_agent.ops.monitor.harvest_guard as guard

    def _boom_append(path: Any, record: Any, **kw: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(guard, "append_jsonl_line", _boom_append)

    # Must NOT raise even though the seam fails.
    marker = harvest_on_terminal(
        experiment,
        _RUN_ID,
        terminal_cause="complete",
        _aggregate=_ok_aggregate({"r": {"acc": 0.9}}),
        _sweep=lambda combiner_dir, run_id: {},
    )
    assert marker["harvest_ok"] is True  # harvest itself succeeded; only the write failed


def test_write_marker_routes_through_canonical_append_seam() -> None:
    """B2 routing pin: the marker write goes through the ONE flock+fsync+sort_keys
    append seam, not a bare ``open(...).write`` — so torn/interleaved final lines
    can't strand a finished run's evidence."""
    import inspect

    from hpc_agent.ops.monitor import harvest_guard

    src = inspect.getsource(harvest_guard._write_marker)
    assert "append_jsonl_line(" in src
    assert "open(" not in src  # no bare append path bypassing the seam


def test_abnormal_exit_skips_harvest_when_run_not_terminal(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run-#12 finding 19: the abnormal-exit sentinel means the WATCH died,
    not the run — with the journal recording a NON-terminal status, the guard
    records a clean skip and never touches the aggregate (no ssh, no pull of
    a live run's results)."""
    from hpc_agent._kernel.contract.vocabulary import JournalStatus
    from hpc_agent.state import journal as journal_module

    monkeypatch.setattr(
        journal_module,
        "load_run",
        lambda exp, rid: SimpleNamespace(status=JournalStatus.IN_FLIGHT),
    )
    calls: list[str] = []

    def _agg(experiment_dir: Path, run_id: str) -> Any:
        calls.append(run_id)
        return SimpleNamespace(aggregated_metrics={}, escalation_reason=None)

    marker = harvest_on_terminal(
        experiment, _RUN_ID, terminal_cause="abnormal-exit", _aggregate=_agg
    )
    assert marker["harvest_skipped_reason"] == "run_not_terminal"
    assert marker["metrics_harvested"] is False
    assert calls == []  # positive-evidence gate: nothing pulled
    on_disk = _read_markers(experiment, _RUN_ID)
    assert on_disk[-1]["harvest_skipped_reason"] == "run_not_terminal"


def test_abnormal_exit_harvests_when_journal_records_terminal(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With POSITIVE terminal evidence in the journal, the sentinel harvests."""
    from hpc_agent._kernel.contract.vocabulary import JournalStatus
    from hpc_agent.state import journal as journal_module

    monkeypatch.setattr(
        journal_module,
        "load_run",
        lambda exp, rid: SimpleNamespace(status=JournalStatus.COMPLETE),
    )
    marker = harvest_on_terminal(
        experiment,
        _RUN_ID,
        terminal_cause="abnormal-exit",
        _aggregate=_ok_aggregate({"r": {"acc": 1.0}}),
        _sweep=lambda d, run_id: {},
    )
    assert marker["metrics_harvested"] is True
    assert marker["harvest_skipped_reason"] is None


def test_named_terminal_cause_never_gated_on_status(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NAMED cause (the clean terminal branches) harvests regardless of the
    record's status — the gate binds ONLY the abnormal-exit sentinel."""
    from hpc_agent._kernel.contract.vocabulary import JournalStatus
    from hpc_agent.state import journal as journal_module

    monkeypatch.setattr(
        journal_module,
        "load_run",
        lambda exp, rid: SimpleNamespace(status=JournalStatus.IN_FLIGHT),
    )
    marker = harvest_on_terminal(
        experiment,
        _RUN_ID,
        terminal_cause="complete",
        _aggregate=_ok_aggregate({"r": {"acc": 1.0}}),
        _sweep=lambda d, run_id: {},
    )
    assert marker["metrics_harvested"] is True


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
