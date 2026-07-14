"""Detach-by-contract seam on ``status-watch`` (connection-broker.md 2026-07-07).

``status-watch`` was the last UNGATED in-code chain hop that dialed the cluster
SYNCHRONOUSLY on an unattended cron tick (the ``snapshot→watch`` hop). Detaching
it moves the ONE cold dial into a durable child so no unattended path dials
inline. These pin:

* the detached handle envelope (started / watch=journal / detached_pid,
  stage=detached) — which ``block_drive._chain`` exits on, making the hop
  spawn-and-return;
* the child's spec carries ``detach=False`` (no fork storm), verb=status-watch;
* the idempotent-replay of a recorded terminal (worker_exited → block-drive tick);
* the live-worker handle (the cron tick re-fires mid-watch — no second spawn);
* the dead-lease re-spawn seam + the single-lease keying;
* ZERO inline ssh on the unattended tick (the enforcement guard);
* terminal recording covers watch_terminal/watch_anomaly but NOT watch_timeout;
* ``detach=False`` still runs the current in-process poll (tests / CI).

Cluster-free: the detached launcher / monitor-flow are patched at their source,
so nothing is spawned and no ssh is opened.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

import hpc_agent.ops.status_blocks as blocks
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent._wire.workflows.status_blocks import StatusBlockResult, StatusWatchSpec

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "ml_run_abcd1234"
_LAUNCH_PATH = "hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached"


class _FakeLaunch:
    run_id = _RUN_ID
    pid = 4242
    log_path = "/x/status-watch.log"


def _watch_spec(*, detach: bool = True) -> StatusWatchSpec:
    return StatusWatchSpec(monitor=MonitorFlowSpec(run_id=_RUN_ID), detach=detach)


def _monitor_result(*, lifecycle_state: str, last_status: dict[str, Any] | None = None):
    from hpc_agent.ops.monitor_flow import MonitorFlowResult

    return MonitorFlowResult(
        run_id=_RUN_ID,
        lifecycle_state=lifecycle_state,
        last_status=last_status if last_status is not None else {"complete": 10},
        combined_waves=[0],
        failed_waves=[],
        ticks=1,
        elapsed_seconds=1.0,
        escalation_reason=None,
    )


def _write_lease(*, pid: int) -> None:
    from hpc_agent.state.run_record import _current_homedir

    detached_dir = _current_homedir() / "_detached"
    detached_dir.mkdir(parents=True, exist_ok=True)
    (detached_dir / f"status-watch-{_RUN_ID}.lease.json").write_text(
        json.dumps(
            {
                "run_id": _RUN_ID,
                "block": "status-watch",
                "pid": pid,
                "log_path": "/x/status-watch.log",
            }
        ),
        encoding="utf-8",
    )


# ── detach-by-default handle ──────────────────────────────────────────────────


def test_watch_detaches_by_default(tmp_path: Path) -> None:
    with (
        mock.patch(_LAUNCH_PATH, return_value=_FakeLaunch()) as m_launch,
        mock.patch.object(blocks, "monitor_flow") as m_mon,
    ):
        result = blocks.status_watch(tmp_path, spec=_watch_spec())  # detach default True

    # The synchronous monitor poll (the ssh spine) never ran in-process.
    m_mon.assert_not_called()
    m_launch.assert_called_once()
    assert m_launch.call_args.kwargs["verb"] == "status-watch"
    # The child gets the SAME verb with detach forced OFF (no fork storm).
    assert m_launch.call_args.kwargs["spec"]["detach"] is False
    # Handle envelope.
    assert result.stage_reached == "detached"
    assert result.started is True
    assert result.watch == "journal"
    assert result.detached_pid == 4242
    assert result.needs_decision is False
    assert result.next_block is None
    assert "detached" in result.relay


def test_watch_synchronous_when_detach_false(tmp_path: Path) -> None:
    """detach=False runs the monitor poll in-process (the attended / CI path)."""
    with (
        mock.patch(_LAUNCH_PATH) as m_launch,
        mock.patch.object(
            blocks, "monitor_flow", return_value=_monitor_result(lifecycle_state="complete")
        ) as m_mon,
    ):
        result = blocks.status_watch(tmp_path, spec=_watch_spec(detach=False))

    m_launch.assert_not_called()
    m_mon.assert_called_once()
    assert result.stage_reached == "watch_terminal"


# ── zero inline ssh on the unattended tick (the enforcement guard) ────────────


def test_unattended_watch_tick_does_zero_inline_ssh(tmp_path: Path) -> None:
    """THE enforcement guard: an unattended detached watch tick spawns the child
    and returns WITHOUT ever calling the monitor-flow ssh spine in-process — the
    'no unattended ssh' invariant (engineering-principles.md). ``monitor_flow`` is
    the ONLY ssh path inside status-watch; asserting it never fires is the
    transport/engine-seam zero-ssh count."""
    with (
        mock.patch(_LAUNCH_PATH, return_value=_FakeLaunch()) as m_launch,
        mock.patch.object(blocks, "monitor_flow") as m_mon,
        mock.patch.object(blocks, "reconcile") as m_reconcile,
    ):
        result = blocks.status_watch(tmp_path, spec=_watch_spec())

    assert m_mon.call_count == 0, "the unattended watch tick dialed the cluster inline"
    assert m_reconcile.call_count == 0
    m_launch.assert_called_once()
    assert result.started is True


def test_snapshot_chain_hop_into_watch_spawns_and_returns(tmp_path: Path) -> None:
    """The three ungated hops (snapshot_clean / watch_timeout / submit-s3
    watching_timeout) all target status-watch; the detached handle is what
    ``block_drive._chain`` exits on, so the hop is spawn-and-return, never a
    synchronous dial."""
    from hpc_agent._kernel.lifecycle import block_drive
    from hpc_agent.infra import block_chain

    assert block_chain.successor_verb("status-snapshot", "snapshot_clean") == "status-watch"
    assert block_chain.successor_verb("status-watch", "watch_timeout") == "status-watch"
    assert block_chain.successor_verb("submit-s3", "watching_timeout") == "status-watch"

    with (
        mock.patch(_LAUNCH_PATH, return_value=_FakeLaunch()),
        mock.patch.object(blocks, "monitor_flow"),
    ):
        result = blocks.status_watch(tmp_path, spec=_watch_spec())

    # The handle block_drive treats as "a detached child owns the poll — exit".
    assert block_drive._is_detached(result.model_dump(mode="json")) is True


# ── idempotent replay (worker_exited → one block-drive tick) ──────────────────


def test_watch_replays_recorded_terminal_no_respawn(tmp_path: Path) -> None:
    from hpc_agent.state.block_terminal import record_terminal
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id=_RUN_ID,
        cmd_sha="deadbeef",
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{task_id}",
        task_count=10,
        tasks_py_sha="",
    )
    recorded = StatusBlockResult(
        block="watch",
        stage_reached="watch_terminal",
        needs_decision=False,
        reason="run complete; terminal harvest guaranteed.",
        run_id=_RUN_ID,
        brief={"run_id": _RUN_ID},
    )
    record_terminal(
        tmp_path,
        run_id=_RUN_ID,
        block="status-watch",
        cmd_sha="deadbeef",
        result_dump=recorded.model_dump(mode="json"),
    )

    with (
        mock.patch(_LAUNCH_PATH) as m_launch,
        mock.patch.object(blocks, "monitor_flow") as m_mon,
    ):
        result = blocks.status_watch(tmp_path, spec=_watch_spec())

    # Replayed the recorded terminal: no re-spawn, no re-dial.
    m_launch.assert_not_called()
    m_mon.assert_not_called()
    assert result.stage_reached == "watch_terminal"
    assert result.needs_decision is False


def test_watch_stale_cmd_sha_does_not_replay(tmp_path: Path) -> None:
    """A moved cmd_sha (a nudge re-resolved the run) refuses the replay."""
    from hpc_agent.state.block_terminal import record_terminal
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id=_RUN_ID,
        cmd_sha="NEWsha",
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{task_id}",
        task_count=10,
        tasks_py_sha="",
    )
    record_terminal(
        tmp_path,
        run_id=_RUN_ID,
        block="status-watch",
        cmd_sha="OLDsha",
        result_dump={"block": "watch", "stage_reached": "watch_terminal", "needs_decision": False},
    )

    with (
        mock.patch(_LAUNCH_PATH, return_value=_FakeLaunch()) as m_launch,
        mock.patch.object(blocks, "monitor_flow"),
    ):
        result = blocks.status_watch(tmp_path, spec=_watch_spec())

    m_launch.assert_called_once()  # stale terminal ignored → spawn fresh
    assert result.stage_reached == "detached"


# ── live-worker handle + dead-lease re-spawn (the cron re-fire cases) ─────────


def test_watch_returns_live_worker_handle_no_second_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unattended cron tick re-fires block-drive while the worker still polls:
    a LIVE lease returns its handle (no second spawn), never DetachedLeaseHeld."""
    _write_lease(pid=7777)
    monkeypatch.setattr("hpc_agent._kernel.lifecycle.detached.pid_alive", lambda _p: True)

    with (
        mock.patch(_LAUNCH_PATH) as m_launch,
        mock.patch.object(blocks, "monitor_flow") as m_mon,
    ):
        result = blocks.status_watch(tmp_path, spec=_watch_spec())

    m_launch.assert_not_called()
    m_mon.assert_not_called()
    assert result.stage_reached == "detached"
    assert result.started is True
    assert result.detached_pid == 7777


def test_watch_dead_lease_respawns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A DEAD lease (worker crashed/exited) is reclaimed → a fresh worker is
    spawned (the re-spawn seam), never re-dialed inline."""
    _write_lease(pid=999_999)
    monkeypatch.setattr("hpc_agent._kernel.lifecycle.detached.pid_alive", lambda _p: False)

    with (
        mock.patch(_LAUNCH_PATH, return_value=_FakeLaunch()) as m_launch,
        mock.patch.object(blocks, "monitor_flow") as m_mon,
    ):
        result = blocks.status_watch(tmp_path, spec=_watch_spec())

    m_launch.assert_called_once()
    m_mon.assert_not_called()
    assert result.stage_reached == "detached"


# ── lease-single keying (a live watch refuses a sibling at the launcher) ──────


def test_guard_single_lease_keys_the_watch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A LIVE ``(run_id, status-watch)`` lease refuses a second launch; a DEAD one
    is reclaimed. Pins the lease key so a live watch refuses a sibling watch."""
    from hpc_agent._kernel.lifecycle import detached
    from hpc_agent.state.run_record import _current_homedir

    detached_dir = _current_homedir() / "_detached"
    detached_dir.mkdir(parents=True, exist_ok=True)
    lease = detached_dir / "status-watch-somerun.lease.json"
    lease.write_text(json.dumps({"pid": 4242}), encoding="utf-8")

    # Live pid → refuse the sibling. ``_guard_single_lease`` reads the
    # intra-module ``_pid_alive`` seam (kept), not the public ``pid_alive``.
    monkeypatch.setattr(detached, "_pid_alive", lambda _p: True)
    with pytest.raises(detached.DetachedLeaseHeld):
        detached._guard_single_lease(detached_dir, "status-watch", "somerun")

    # Dead pid → reclaimable (returns the lease path, no raise).
    monkeypatch.setattr(detached, "_pid_alive", lambda _p: False)
    assert detached._guard_single_lease(detached_dir, "status-watch", "somerun") == lease


def test_spawn_detached_lock_acquire_is_bounded_and_names_the_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run-#12 finding 16 (G4): the decide→spawn→stamp lease LOCK acquire is
    bounded, and on expiry the refusal NAMES the wedged holder's pid (read from
    the sibling lease.json) instead of freezing a successor worker silently."""
    from hpc_agent._kernel.lifecycle import detached
    from hpc_agent.infra import io

    detached_dir = tmp_path / "_detached"
    detached_dir.mkdir(parents=True, exist_ok=True)
    log_path = detached_dir / "status-watch-wedged.log"
    lock_path = detached_dir / "status-watch-wedged.lease.lock"
    lease_path = detached_dir / "status-watch-wedged.lease.json"
    lease_path.write_text(json.dumps({"pid": 31337}), encoding="utf-8")

    # Shrink the bound so the test does not actually wait a minute.
    monkeypatch.setattr(detached, "_LEASE_LOCK_TIMEOUT_SEC", 0.2)

    # A concurrent holder wedged inside the critical section: hold the lock for
    # the whole attempt (a fresh FileLock instance contends cross-/same-process).
    with (
        io.advisory_flock(lock_path),
        pytest.raises(detached.DetachedLeaseHeld) as excinfo,
    ):
        detached._spawn_detached(
            run_id="wedged",
            block="status-watch",
            argv=["hpc-agent", "status-watch"],
            log_path=log_path,
            cwd=str(tmp_path),
        )
    msg = str(excinfo.value)
    assert "31337" in msg  # the holder pid is named
    assert "0s" in msg or "0.2" in msg or "within" in msg  # bounded, named window


# ── terminal recording: final states recorded, timeout is NOT ─────────────────


def test_watch_records_terminal_on_complete(tmp_path: Path) -> None:
    from hpc_agent.state.block_terminal import read_terminal

    with mock.patch.object(
        blocks, "monitor_flow", return_value=_monitor_result(lifecycle_state="complete")
    ):
        blocks.status_watch(tmp_path, spec=_watch_spec(detach=False))

    assert read_terminal(tmp_path, _RUN_ID, "status-watch") is not None


def test_watch_records_terminal_on_anomaly(tmp_path: Path) -> None:
    from hpc_agent.state.block_terminal import read_terminal

    with mock.patch.object(
        blocks, "monitor_flow", return_value=_monitor_result(lifecycle_state="failed")
    ):
        blocks.status_watch(tmp_path, spec=_watch_spec(detach=False))

    assert read_terminal(tmp_path, _RUN_ID, "status-watch") is not None


def test_watch_timeout_is_not_recorded_as_terminal(tmp_path: Path) -> None:
    """A watch_timeout is a keep-watching continuation — recording it would replay
    a stale timeout and wedge the self-loop instead of re-spawning a fresh watch."""
    from hpc_agent.state.block_terminal import read_terminal

    with (
        mock.patch.object(
            blocks, "monitor_flow", return_value=_monitor_result(lifecycle_state="timeout")
        ),
        mock.patch.object(blocks, "load_run", return_value=None),
    ):
        result = blocks.status_watch(tmp_path, spec=_watch_spec(detach=False))

    assert result.stage_reached == "watch_timeout"
    assert read_terminal(tmp_path, _RUN_ID, "status-watch") is None
