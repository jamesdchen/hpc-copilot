"""Tests for the headless campaign driver's pure planning logic.

``plan_action`` maps a ``delegate`` block to a concrete action intent;
it is the testable core of the driver. The subprocess-spawning halves
(``_run_cli_step`` / ``_run_agent_step``) are thin shells over it.
"""

from __future__ import annotations

from hpc_agent.campaign.driver import plan_action


def test_cli_monitor_step_maps_to_monitor_flow():
    plan = plan_action(
        {"kind": "cli", "step": "monitor", "run_id": "r1"}, allow_agent_steps=False
    )
    assert plan == {
        "action": "cli",
        "verb": "monitor-flow",
        "run_id": "r1",
        "step": "monitor",
    }


def test_cli_aggregate_step_maps_to_aggregate_flow():
    plan = plan_action(
        {"kind": "cli", "step": "aggregate", "run_id": "r2"}, allow_agent_steps=False
    )
    assert plan["verb"] == "aggregate-flow"


def test_cli_step_without_run_id_skips():
    plan = plan_action(
        {"kind": "cli", "step": "monitor", "run_id": None}, allow_agent_steps=False
    )
    assert plan["action"] == "skip"


def test_cli_step_with_unmapped_step_skips():
    plan = plan_action(
        {"kind": "cli", "step": "decide", "run_id": "r1"}, allow_agent_steps=False
    )
    assert plan["action"] == "skip"


def test_agent_step_skipped_without_flag():
    plan = plan_action(
        {"kind": "agent", "step": "submit", "prompt": "go"}, allow_agent_steps=False
    )
    assert plan["action"] == "skip"
    assert "--allow-agent-steps" in plan["reason"]


def test_agent_step_allowed_with_flag():
    plan = plan_action(
        {"kind": "agent", "step": "submit", "prompt": "go"}, allow_agent_steps=True
    )
    assert plan == {"action": "agent", "prompt": "go", "step": "submit"}


def test_no_delegate_block_skips():
    assert plan_action(None, allow_agent_steps=True)["action"] == "skip"


def test_unknown_delegate_kind_skips():
    assert plan_action({"kind": "weird"}, allow_agent_steps=True)["action"] == "skip"
