"""U4 pull-lag disclosure on ``status-watch`` — the surface that spans the hours.

``status-watch`` is where a human actually sits through a long array: it is the
detached long-poll, and it is the successor ``submit-s3`` hands a
``watching_timeout`` to. During the 2026-07-30 trainwreck the run was watched
through this block for hours while 1741 finished results sat unread on cluster
scratch. So this is precisely the surface that must answer "how much of it is
home?" — a disclosure that existed only on the submit-s3 line would be absent
from the block the question is actually asked in.

Pinned here:

* the brief carries ``incremental_harvest`` verbatim from the monitor envelope,
  on EVERY watch stage (terminal / timeout / anomaly) — the timeout one most of
  all, since that is the "keep watching or stop?" boundary;
* the relay line renders the lag through the SAME ``_stream_phrase`` composer
  the submit relay uses (one definition, so the two surfaces cannot drift);
* a paused stream reads as PAUSED, not as merely behind;
* a run that streamed nothing renders byte-identically to before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

import hpc_agent.ops.status_blocks as blocks
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent._wire.workflows.status_blocks import StatusWatchSpec
from hpc_agent.ops.relay_render import render_relay

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "har_base_sweep_53c27e42"


def _watch_spec() -> StatusWatchSpec:
    # detach=False so the poll runs in-process; the detached path is pinned in
    # test_block_detach.py and never reaches the brief composer.
    return StatusWatchSpec(monitor=MonitorFlowSpec(run_id=_RUN_ID), detach=False)


def _harvest_block(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "enabled": True,
        "pulls": 14,
        "files_pulled": 1077,
        "bytes_pulled": 2_154_000,
        "tasks_mirrored": 359,
        "paused_reason": None,
        "last_error": None,
    }
    base.update(overrides)
    return base


def _monitor_result(
    *, lifecycle_state: str, incremental_harvest: dict[str, Any] | None = None
) -> Any:
    from hpc_agent.ops.monitor_flow import MonitorFlowResult

    return MonitorFlowResult(
        run_id=_RUN_ID,
        lifecycle_state=lifecycle_state,
        last_status={"complete": 2100, "running": 0, "pending": 0, "failed": 0},
        combined_waves=[],
        failed_waves=[],
        ticks=42,
        elapsed_seconds=8_400.0,
        escalation_reason=("budget elapsed" if lifecycle_state == "timeout" else None),
        incremental_harvest=(
            _harvest_block() if incremental_harvest is None else incremental_harvest
        ),
    )


def _run_watch(tmp_path: Path, result: Any) -> Any:
    with (
        mock.patch.object(blocks, "monitor_flow", return_value=result),
        mock.patch.object(blocks, "load_run", return_value=None),
    ):
        return blocks.status_watch(tmp_path, spec=_watch_spec())


# ── the brief carries the block on every stage ───────────────────────────────


@pytest.mark.parametrize(
    ("lifecycle", "stage"),
    [
        ("complete", "watch_terminal"),
        ("timeout", "watch_timeout"),
        ("failed", "watch_anomaly"),
    ],
)
def test_every_watch_stage_carries_the_pull_lag(tmp_path: Path, lifecycle: str, stage: str) -> None:
    """Whatever the watch settled as, the human learns how much came home."""
    out = _run_watch(tmp_path, _monitor_result(lifecycle_state=lifecycle))
    assert out.stage_reached == stage
    assert out.brief["incremental_harvest"] == _harvest_block()


def test_the_timeout_relay_names_the_lag_at_the_keep_watching_boundary(
    tmp_path: Path,
) -> None:
    """The trainwreck's own moment: a budget-elapsed watch asking "keep watching
    or stop?". The answer turns on how much is already readable, so the count
    has to be ON that line — not one verb away in a brief nobody opens."""
    out = _run_watch(tmp_path, _monitor_result(lifecycle_state="timeout"))
    assert "359 pulled locally" in out.relay
    assert "keep watching or stop?" in out.relay


def test_a_paused_stream_reads_as_paused_on_the_watch_line(tmp_path: Path) -> None:
    """A stalled byte-mover must not read as a merely-behind one — that silence
    is the failure this whole unit exists to remove."""
    out = _run_watch(
        tmp_path,
        _monitor_result(
            lifecycle_state="timeout",
            incremental_harvest=_harvest_block(paused_reason="ssh_circuit_open"),
        ),
    )
    assert "359 pulled locally" in out.relay
    assert "streaming PAUSED: ssh_circuit_open" in out.relay


def test_the_terminal_line_also_carries_the_lag(tmp_path: Path) -> None:
    """A 'complete' watch whose results are still mostly remote is exactly the
    trainwreck: 2100 complete, 359 home. Handing off to harvest without saying
    so is what left the human asking where the results were."""
    out = _run_watch(tmp_path, _monitor_result(lifecycle_state="complete"))
    assert "359 pulled locally" in out.relay
    assert "harvest guaranteed" in out.relay


# ── one definition, shared with the submit relay ─────────────────────────────


def test_the_watch_and_submit_relays_use_the_same_phrase(tmp_path: Path) -> None:
    """Both surfaces render the lag through ``_stream_phrase``. If either grew
    its own wording the two would eventually disagree about the same numbers."""
    block = _harvest_block()
    watch = render_relay(
        "watch", "watch_timeout", {"run_id": _RUN_ID, "incremental_harvest": block}
    )
    submit = render_relay(
        "s3",
        "watching_timeout",
        {"cluster": "hoffman2", "main_run_id": _RUN_ID, "incremental_harvest": block},
    )
    phrase = ", 359 pulled locally"
    assert phrase in watch
    assert phrase in submit


# ── silent when there is nothing to say ──────────────────────────────────────


def test_a_run_that_streamed_nothing_renders_byte_identically(tmp_path: Path) -> None:
    """Streaming off / opted out / pure-API: the line must be exactly today's."""
    brief_with = {
        "run_id": _RUN_ID,
        "summary": {"complete": 20},
        "incremental_harvest": {"enabled": False, "tasks_mirrored": 0},
    }
    brief_without = {"run_id": _RUN_ID, "summary": {"complete": 20}}
    for stage in ("watch_terminal", "watch_timeout", "watch_anomaly"):
        assert render_relay("watch", stage, brief_with) == render_relay(
            "watch", stage, brief_without
        )


def test_a_brief_predating_the_field_is_untouched(tmp_path: Path) -> None:
    """An older journal brief has no ``incremental_harvest`` key at all; the
    renderer must degrade silently rather than print a zero it cannot vouch for."""
    line = render_relay("watch", "watch_timeout", {"run_id": _RUN_ID})
    assert "pulled locally" not in line
    assert "keep watching or stop?" in line
