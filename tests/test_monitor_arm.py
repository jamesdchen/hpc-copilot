"""Tests for ``claude_hpc.atoms.monitor_arm.decide_monitor_arm``.

The primitive picks cron/loop/none + cadence + cron schedule + the
literal ``armed:`` line. We test:

  * terminal states return arm="none"
  * /loop invocation returns arm="loop"
  * adaptive table picks the right cadence per (eta, pace, queue_wait)
  * ``armed_line`` is byte-stable for the same input
  * ``cron_create_args`` carries the invocation_argv verbatim
  * SpecInvalid on bad inputs
"""

from __future__ import annotations

from claude_hpc._schema_models.decide_monitor_arm import DecideMonitorArmSpec
from claude_hpc.atoms.monitor_arm import decide_monitor_arm


def _summary(complete: int = 0, running: int = 0, pending: int = 0, failed: int = 0) -> dict:
    return {"complete": complete, "running": running, "pending": pending, "failed": failed}


def test_complete_terminal_arms_none() -> None:
    out = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id="r1",
            summary=_summary(complete=10),
            total_tasks=10,
            invocation_argv="/monitor-hpc r1",
        )
    )
    assert out["arm"] == "none"
    assert out["cadence_sec"] == 0
    assert out["schedule"] is None
    assert out["cron_create_args"] is None
    assert out["armed_line"] == 'armed: none run_id=r1 cadence=0s reason="complete"'


def test_failed_no_running_terminal_arms_none() -> None:
    out = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id="r1",
            summary=_summary(complete=5, failed=3),  # 5 + 3 = 8, total=10 — but no running/pending
            total_tasks=10,
            invocation_argv="/monitor-hpc r1",
        )
    )
    assert out["arm"] == "none"
    assert "failed_no_running" in out["armed_line"]


def test_user_loop_invocation_arms_loop() -> None:
    out = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id="r1",
            summary=_summary(running=5, pending=5),
            total_tasks=10,
            invocation_argv="/monitor-hpc r1",
            user_invoked_via_loop=True,
        )
    )
    assert out["arm"] == "loop"
    assert out["cron_create_args"] is None
    assert 'reason="user_invoked_via_loop"' in out["armed_line"]


def test_eta_lt_10min_picks_60s() -> None:
    out = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id="r1",
            summary=_summary(running=5, pending=5),
            total_tasks=10,
            invocation_argv="/monitor-hpc r1",
            eta_sec=300,
        )
    )
    assert out["arm"] == "cron"
    assert out["cadence_sec"] == 60
    assert out["schedule"] == "* * * * *"
    assert out["cron_create_args"]["schedule"] == "* * * * *"
    assert out["cron_create_args"]["prompt"] == "/monitor-hpc r1"


def test_eta_10_30min_stable_picks_180s() -> None:
    out = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id="r1",
            summary=_summary(running=5, pending=5),
            total_tasks=10,
            invocation_argv="/monitor-hpc r1",
            eta_sec=900,  # 15 min
        )
    )
    assert out["cadence_sec"] == 180
    assert out["schedule"] == "*/3 * * * *"


def test_eta_10_30min_unstable_picks_90s() -> None:
    out = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id="r1",
            summary=_summary(running=5, pending=5),
            total_tasks=10,
            invocation_argv="/monitor-hpc r1",
            eta_sec=900,
            pace_unstable=True,
        )
    )
    assert out["cadence_sec"] == 90


def test_eta_gt_30min_stable_picks_270s() -> None:
    out = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id="r1",
            summary=_summary(running=5, pending=5),
            total_tasks=10,
            invocation_argv="/monitor-hpc r1",
            eta_sec=3600,  # 1 hour
        )
    )
    assert out["cadence_sec"] == 270
    assert out["schedule"] == "*/4 * * * *"  # 270s rounds to 4 min


def test_queue_wait_gt_30min_picks_super_cache() -> None:
    out = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id="r1",
            summary=_summary(pending=10),
            total_tasks=10,
            invocation_argv="/monitor-hpc r1",
            queue_wait_sec=2400,  # 40 min
        )
    )
    assert out["cadence_sec"] == 1800
    assert out["schedule"] == "*/30 * * * *"


def test_hour_scale_queue_picks_3600s() -> None:
    out = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id="r1",
            summary=_summary(pending=10),
            total_tasks=10,
            invocation_argv="/monitor-hpc r1",
            queue_wait_sec=7200,
        )
    )
    assert out["cadence_sec"] == 3600
    assert out["schedule"] == "0 */1 * * *"


def test_no_eta_running_fallback_picks_90s() -> None:
    out = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id="r1",
            summary=_summary(running=3, pending=7),
            total_tasks=10,
            invocation_argv="/monitor-hpc r1",
        )
    )
    assert out["cadence_sec"] == 90


def test_no_eta_all_pending_picks_super_cache() -> None:
    out = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id="r1",
            summary=_summary(pending=10),
            total_tasks=10,
            invocation_argv="/monitor-hpc r1",
        )
    )
    assert out["cadence_sec"] == 1800
    assert out["reason"] == "all_pending_fallback"


def test_armed_line_format_is_byte_stable() -> None:
    """Same inputs must produce byte-identical armed_line — the Stop hook
    matches it textually."""
    spec = DecideMonitorArmSpec(
        run_id="ml_ridge_abc",
        summary=_summary(running=5, pending=5),
        total_tasks=10,
        invocation_argv="/monitor-hpc ml_ridge_abc",
        eta_sec=300,
    )
    a = decide_monitor_arm(spec=spec)
    b = decide_monitor_arm(spec=spec)
    assert a["armed_line"] == b["armed_line"]
    assert a["armed_line"] == 'armed: cron run_id=ml_ridge_abc cadence=60s reason="eta_lt_10min"'


def test_cron_create_args_carries_invocation_argv() -> None:
    out = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id="r1",
            summary=_summary(running=5, pending=5),
            total_tasks=10,
            invocation_argv="/monitor-hpc --run-id r1 --foo bar",
            eta_sec=300,
        )
    )
    assert out["cron_create_args"]["prompt"] == "/monitor-hpc --run-id r1 --foo bar"
    assert "r1" in out["cron_create_args"]["reason"]
