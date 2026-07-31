"""The U4 incremental-harvest GATE — when the watch streams finished results.

``ops/monitor/stream_harvest`` owns the policy half of the incremental harvest:
the pure decision of whether THIS tick should pull, the read-only breaker
consult that pauses it, and the honest ``N complete, M pulled locally``
rendering. The mechanism half (the pull) lives in
``aggregate_flow.prefetch_per_task_results`` and is pinned separately.

Pinned here:

* the two triggers (size, staleness) and the hard spacing floor that gates
  BOTH — the difference between "stream every couple of minutes" and "one pull
  per poll against a login node";
* never double-pulling: an unchanged complete count is not a backlog, so an
  idle tail streams zero times however long it idles;
* the first stream is NOT held for one spacing floor (that floor exists to
  space repeats, and the first bytes are the whole point);
* the breaker PAUSES the stream — including ``half_open_eligible``, whose
  probe slot belongs to the watch's own poll — and fails open on an
  unreadable circuit doc;
* the env knobs, including the fail-safe on garbage values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent.ops.monitor import stream_harvest as sh

if TYPE_CHECKING:
    from pathlib import Path

_FLOOR = sh.STREAM_SPACING_FLOOR_SEC
_INTERVAL = sh.STREAM_MIN_INTERVAL_SEC
_BATCH = sh.STREAM_BATCH_TASKS


# ---------------------------------------------------------------------------
# should_stream — the two triggers and the floor
# ---------------------------------------------------------------------------


def test_size_trigger_fires_once_the_batch_threshold_is_reached() -> None:
    """A full batch of newly-complete tasks streams without waiting out the
    staleness interval — that is the whole point of the size trigger."""
    assert sh.should_stream(
        complete=_BATCH,
        last_streamed_complete=0,
        seconds_since_last=_FLOOR,
    )


def test_below_the_batch_threshold_waits_for_the_staleness_interval() -> None:
    """A sub-batch trickle does NOT stream on size; it streams once the backlog
    has aged past the staleness interval, so a slow tail still arrives."""
    assert not sh.should_stream(complete=1, last_streamed_complete=0, seconds_since_last=_FLOOR)
    assert sh.should_stream(complete=1, last_streamed_complete=0, seconds_since_last=_INTERVAL)


def test_spacing_floor_gates_the_size_trigger_too() -> None:
    """A burst completion (thousands of tasks in one tick) must not turn the
    batch threshold into a pull-per-poll storm: the floor gates BOTH triggers."""
    assert not sh.should_stream(
        complete=10_000,
        last_streamed_complete=0,
        seconds_since_last=_FLOOR - 1,
    )
    assert sh.should_stream(
        complete=10_000,
        last_streamed_complete=0,
        seconds_since_last=_FLOOR,
    )


def test_unchanged_complete_count_never_streams_however_long_it_idles() -> None:
    """The never-double-pull invariant. An idle tail has no backlog, so no
    amount of elapsed time makes it eligible — the transport's delta would
    return empty, but we do not even spend the round trip to learn that."""
    for elapsed in (0.0, _INTERVAL, _INTERVAL * 100):
        assert not sh.should_stream(
            complete=500, last_streamed_complete=500, seconds_since_last=elapsed
        )


def test_zero_complete_never_streams() -> None:
    """Nothing has finished; there is nothing to move."""
    assert not sh.should_stream(
        complete=0, last_streamed_complete=-1, seconds_since_last=float("inf")
    )


def test_first_stream_is_not_held_for_a_spacing_floor() -> None:
    """A never-streamed run reports an unbounded backlog age, so the very first
    backlog moves immediately. Holding the FIRST bytes for a spacing floor
    would reintroduce exactly the latency this unit exists to remove."""
    assert sh.should_stream(complete=1, last_streamed_complete=-1, seconds_since_last=float("inf"))


def test_backlog_is_measured_from_the_last_streamed_count_not_zero() -> None:
    """After streaming at 100 complete, the next size trigger needs a FULL
    fresh batch — not the cumulative count crossing the threshold again."""
    assert not sh.should_stream(
        complete=100 + _BATCH - 1,
        last_streamed_complete=100,
        seconds_since_last=_FLOOR,
    )
    assert sh.should_stream(
        complete=100 + _BATCH,
        last_streamed_complete=100,
        seconds_since_last=_FLOOR,
    )


def test_explicit_thresholds_override_the_env_defaults() -> None:
    """The keyword overrides exist so the loop's tests can pin behaviour
    without reaching into process env."""
    assert sh.should_stream(
        complete=2,
        last_streamed_complete=0,
        seconds_since_last=0.0,
        batch=2,
        min_interval=999.0,
        spacing_floor=0.0,
    )


# ---------------------------------------------------------------------------
# opt-out: spec knob over env
# ---------------------------------------------------------------------------


def test_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sh.INCREMENTAL_HARVEST_ENV, raising=False)
    assert sh.incremental_harvest_enabled() is True


def test_env_zero_opts_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_ENV, "0")
    assert sh.incremental_harvest_enabled() is False


def test_spec_knob_wins_over_the_env_in_both_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A metered-link run and a fat-pipe run share one operator shell; the RUN
    is the thing that knows, so its explicit answer beats the env either way."""
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_ENV, "0")
    assert sh.incremental_harvest_enabled(True) is True
    monkeypatch.delenv(sh.INCREMENTAL_HARVEST_ENV, raising=False)
    assert sh.incremental_harvest_enabled(False) is False


def test_a_pure_api_backend_cannot_be_switched_on_by_either_knob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pure-API backend's results arrive over its own API, not from a remote
    tree — so there is nothing to stream, and neither knob may claim otherwise.
    The capability check comes FIRST for exactly that reason."""
    import hpc_agent.infra.backends as backends_module

    monkeypatch.delenv(sh.INCREMENTAL_HARVEST_ENV, raising=False)
    monkeypatch.setattr(backends_module, "backend_requires_ssh", lambda _n: False)
    assert sh.incremental_harvest_enabled(backend="some-api-backend") is False
    assert sh.incremental_harvest_enabled(True, backend="some-api-backend") is False


def test_an_ssh_backend_still_defers_to_the_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    import hpc_agent.infra.backends as backends_module

    monkeypatch.delenv(sh.INCREMENTAL_HARVEST_ENV, raising=False)
    monkeypatch.setattr(backends_module, "backend_requires_ssh", lambda _n: True)
    assert sh.incremental_harvest_enabled(backend="sge") is True
    assert sh.incremental_harvest_enabled(False, backend="sge") is False


# ---------------------------------------------------------------------------
# env knobs — fail-safe
# ---------------------------------------------------------------------------


def test_env_knobs_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_BATCH_ENV, "7")
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_INTERVAL_ENV, "12.5")
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_FLOOR_ENV, "0")
    assert sh.stream_batch_tasks() == 7
    assert sh.stream_min_interval_sec() == 12.5
    assert sh.stream_spacing_floor_sec() == 0.0


def test_garbage_env_degrades_to_the_shipped_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo in an operator's shell must not silently disable the stream nor
    hammer the cluster — it degrades to the shipped defaults."""
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_BATCH_ENV, "banana")
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_INTERVAL_ENV, "")
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_FLOOR_ENV, "-4")
    assert sh.stream_batch_tasks() == _BATCH
    assert sh.stream_min_interval_sec() == _INTERVAL
    assert sh.stream_spacing_floor_sec() == _FLOOR


def test_zero_batch_is_rejected_but_zero_interval_is_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``0`` is a meaningful interval ("stream every tick with anything new")
    but a meaningless batch size, so only the interval accepts it."""
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_BATCH_ENV, "0")
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_INTERVAL_ENV, "0")
    assert sh.stream_batch_tasks() == _BATCH
    assert sh.stream_min_interval_sec() == 0.0


# ---------------------------------------------------------------------------
# stream_blocked_by — the breaker consult
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("closed", None),
        ("open", "ssh_circuit_open"),
        # The half-open probe slot belongs to the WATCH's own poll. An
        # opportunistic byte-mover must never spend it.
        ("half_open_eligible", "ssh_circuit_half_open_eligible"),
    ],
)
def test_breaker_state_decides_whether_the_stream_may_run(
    monkeypatch: pytest.MonkeyPatch, state: str, expected: str | None
) -> None:
    import hpc_agent.infra.ssh_circuit as circuit

    monkeypatch.setattr(circuit, "effective_state_for_host", lambda host, **_kw: state)
    assert sh.stream_blocked_by("user@login1.cluster") == expected


def test_breaker_read_keys_the_host_not_the_user_at_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read seam must key the same doc the breaker WRITES, or a pause would
    consult a circuit that never opens."""
    seen: list[str] = []
    import hpc_agent.infra.ssh_circuit as circuit

    def _fake(host: str, **_kw: object) -> str:
        seen.append(host)
        return "closed"

    monkeypatch.setattr(circuit, "effective_state_for_host", _fake)
    sh.stream_blocked_by("someuser@login1.cluster")
    assert seen == ["login1.cluster"]


def test_missing_ssh_target_pauses_rather_than_raising() -> None:
    assert sh.stream_blocked_by(None) == "no_ssh_target"
    assert sh.stream_blocked_by("") == "no_ssh_target"


def test_absent_circuit_doc_fails_open(journal_home: Path) -> None:
    """A breaker-state read must never be the thing that silently stops
    streaming: an absent/unreadable doc reads as ``closed``."""
    assert sh.stream_blocked_by("user@never-seen-this-host.invalid") is None


# ---------------------------------------------------------------------------
# render_stream_lag — the honest disclosure
# ---------------------------------------------------------------------------


def test_lag_line_names_the_shortfall() -> None:
    """The trainwreck's own numbers. A gap must READ as a gap — the silence
    about it is what let 1741 finished results sit unreadable for 2h+."""
    line = sh.render_stream_lag(2100, 359, 2100)
    assert "2100 complete/2100" in line
    assert "359 pulled locally" in line
    assert "1741 not yet pulled" in line


def test_lag_line_without_a_total() -> None:
    assert sh.render_stream_lag(10, 10) == "10 complete, 10 pulled locally"


def test_no_shortfall_clause_when_caught_up_or_ahead() -> None:
    """Markers are written before results settle and a sibling can warm the
    mirror, so mirrored > complete is legitimate — and must not render as a
    negative backlog."""
    assert "not yet pulled" not in sh.render_stream_lag(10, 10, 10)
    assert "not yet pulled" not in sh.render_stream_lag(10, 12, 10)


# ---------------------------------------------------------------------------
# stream_disclosure — one shape for every consumer
# ---------------------------------------------------------------------------


def test_disclosure_projects_the_loop_counters() -> None:
    from hpc_agent.ops.monitor_flow import _LoopState

    state = _LoopState(
        stream_enabled=True,
        stream_pulls=3,
        stream_files=120,
        stream_bytes=4096,
        stream_tasks_mirrored=118,
    )
    assert sh.stream_disclosure(state) == {
        "enabled": True,
        "pulls": 3,
        "files_pulled": 120,
        "bytes_pulled": 4096,
        "tasks_mirrored": 118,
        "paused_reason": None,
        "last_error": None,
    }


def test_disclosure_of_a_never_streamed_run_is_all_zeros_and_disabled() -> None:
    from hpc_agent.ops.monitor_flow import _LoopState

    disclosure = sh.stream_disclosure(_LoopState())
    assert disclosure["enabled"] is False
    assert disclosure["pulls"] == 0
    assert disclosure["tasks_mirrored"] == 0
