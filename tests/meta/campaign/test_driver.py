"""Tests for the headless driver's pure planning logic and injected seams.

``plan_action`` maps a ``delegate`` block to a concrete action intent;
it is the testable core of the loop. The subprocess-spawning halves
(``_run_cli_step`` / ``_run_agent_step``) are thin shells over it.

The ``step_table`` (which deterministic verb each step maps to) and the
judgement ``resolver`` (how an agent step is executed) are injected — the
loop mechanism is neutral; campaign supplies the policy via
``CampaignLoopConfig``. These tests cover both the campaign defaults and a
non-campaign injection.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent._kernel.lifecycle import drive as drive_mod
from hpc_agent._kernel.lifecycle.drive import _run_agent_step, drive_once
from hpc_agent.meta.campaign.driver import (
    CampaignLoopConfig,
    plan_action,
)

# The campaign step table, used wherever a test exercises the default mapping.
_CAMPAIGN = CampaignLoopConfig().step_table


def test_cli_monitor_step_maps_to_monitor_flow():
    plan = plan_action(
        {"kind": "cli", "step": "monitor", "run_id": "r1"},
        step_table=_CAMPAIGN,
        allow_agent_steps=False,
    )
    assert plan == {
        "action": "cli",
        "verb": "monitor-flow",
        "run_id": "r1",
        "step": "monitor",
    }


def test_cli_aggregate_step_maps_to_aggregate_flow():
    plan = plan_action(
        {"kind": "cli", "step": "aggregate", "run_id": "r2"},
        step_table=_CAMPAIGN,
        allow_agent_steps=False,
    )
    assert plan["verb"] == "aggregate-flow"


def test_cli_step_without_run_id_skips():
    plan = plan_action(
        {"kind": "cli", "step": "monitor", "run_id": None},
        step_table=_CAMPAIGN,
        allow_agent_steps=False,
    )
    assert plan["action"] == "skip"


def test_cli_step_with_unmapped_step_skips():
    plan = plan_action(
        {"kind": "cli", "step": "decide", "run_id": "r1"},
        step_table=_CAMPAIGN,
        allow_agent_steps=False,
    )
    assert plan["action"] == "skip"


def test_injected_non_campaign_step_table_routes_by_caller_policy():
    """The mechanism is neutral: a caller can map any step to any verb, and
    a step absent from the table skips — no campaign vocabulary is baked in."""
    table = {"reindex": "reindex-flow"}
    routed = plan_action(
        {"kind": "cli", "step": "reindex", "run_id": "r9"},
        step_table=table,
        allow_agent_steps=False,
    )
    assert routed == {
        "action": "cli",
        "verb": "reindex-flow",
        "run_id": "r9",
        "step": "reindex",
    }
    # campaign's own step is unknown under a non-campaign table -> skip.
    skipped = plan_action(
        {"kind": "cli", "step": "monitor", "run_id": "r9"},
        step_table=table,
        allow_agent_steps=False,
    )
    assert skipped["action"] == "skip"


def test_agent_step_skipped_without_flag():
    plan = plan_action(
        {"kind": "agent", "step": "submit"}, step_table=_CAMPAIGN, allow_agent_steps=False
    )
    assert plan["action"] == "skip"
    assert "--allow-agent-steps" in plan["reason"]


def test_agent_skip_reason_is_transport_neutral():
    """The neutral loop must not advertise `claude -p`: the transport is the
    injected resolver's concern (a `codex-cli`/`gemini-cli` worker, #305)."""
    reason = plan_action(
        {"kind": "agent", "step": "submit"}, step_table=_CAMPAIGN, allow_agent_steps=False
    )["reason"]
    assert "claude -p" not in reason
    assert "spawn a worker" in reason


def test_agent_step_allowed_with_flag():
    spawn_request = {"workflow": "submit", "experiment_dir": ".", "fields": {}}
    plan = plan_action(
        {"kind": "agent", "step": "submit", "spawn_request": spawn_request},
        step_table=_CAMPAIGN,
        allow_agent_steps=True,
    )
    assert plan == {
        "action": "agent",
        "spawn_request": spawn_request,
        "step": "submit",
    }


def test_agent_step_without_spawn_request_skips():
    plan = plan_action(
        {"kind": "agent", "step": "submit"}, step_table=_CAMPAIGN, allow_agent_steps=True
    )
    assert plan["action"] == "skip"
    assert "spawn_request" in plan["reason"]


def test_no_delegate_block_skips():
    assert plan_action(None, step_table=_CAMPAIGN, allow_agent_steps=True)["action"] == "skip"


def test_unknown_delegate_kind_skips():
    assert (
        plan_action({"kind": "weird"}, step_table=_CAMPAIGN, allow_agent_steps=True)["action"]
        == "skip"
    )


def test_injected_resolver_is_called_instead_of_run_workflow(capsys):
    """The agent step routes through the injected resolver, never a hardcoded
    ``run_workflow``/``claude -p``. A test double stands in — no credentials,
    no real spawn — and its report is what the tick prints."""

    class _FakeReport:
        def model_dump(self, *, mode: str):
            return {"ok": True, "stub": "report"}

    calls: list[tuple[dict, object]] = []

    def fake_resolver(spawn_request, experiment_dir):
        calls.append((spawn_request, experiment_dir))
        return _FakeReport(), 0

    spawn_request = {"workflow": "submit", "experiment_dir": ".", "fields": {"x": 1}}
    exit_code = _run_agent_step(spawn_request, experiment_dir="/tmp/exp", resolver=fake_resolver)

    assert exit_code == 0
    assert calls == [(spawn_request, "/tmp/exp")]
    # the resolver's report is printed verbatim as the per-tick record.
    printed = capsys.readouterr().out
    assert '"stub": "report"' in printed


def test_drive_once_dispatches_cli_step(monkeypatch):
    """The programmatic entry (no argv) plans and dispatches a cli step."""
    monkeypatch.setattr(
        drive_mod,
        "load_context",
        lambda _exp: {"delegate": {"kind": "cli", "step": "monitor", "run_id": "r1"}},
    )
    seen: list[tuple[str, str, object]] = []
    monkeypatch.setattr(
        drive_mod,
        "_run_cli_step",
        lambda verb, run_id, exp: seen.append((verb, run_id, exp)) or 7,
    )

    code = drive_once(Path("/tmp/exp"), step_table=_CAMPAIGN, resolver=_unused_resolver)

    assert code == 7
    assert seen == [("monitor-flow", "r1", Path("/tmp/exp"))]


def test_drive_once_dry_run_does_not_dispatch(monkeypatch):
    monkeypatch.setattr(
        drive_mod,
        "load_context",
        lambda _exp: {"delegate": {"kind": "cli", "step": "monitor", "run_id": "r1"}},
    )

    def _boom(*_a, **_k):
        raise AssertionError("dry_run must not dispatch")

    monkeypatch.setattr(drive_mod, "_run_cli_step", _boom)

    code = drive_once(
        Path("/tmp/exp"), step_table=_CAMPAIGN, resolver=_unused_resolver, dry_run=True
    )
    assert code == 0


def test_drive_once_agent_step_uses_injected_resolver(monkeypatch):
    monkeypatch.setattr(
        drive_mod,
        "load_context",
        lambda _exp: {
            "delegate": {
                "kind": "agent",
                "step": "submit",
                "spawn_request": {"workflow": "submit", "fields": {}},
            }
        },
    )

    class _R:
        def model_dump(self, *, mode):
            return {"ok": True}

    used: list[dict] = []

    def resolver(spawn_request, _exp):
        used.append(spawn_request)
        return _R(), 0

    code = drive_once(
        Path("/tmp/exp"), step_table=_CAMPAIGN, resolver=resolver, allow_agent_steps=True
    )
    assert code == 0
    assert used == [{"workflow": "submit", "fields": {}}]


def test_drive_wrapper_parses_argv_and_delegates(monkeypatch):
    """The argparse wrapper only translates argv into drive_once's kwargs."""
    captured: dict = {}

    def fake_drive_once(experiment_dir, *, step_table, resolver, allow_agent_steps, dry_run):
        captured.update(
            experiment_dir=experiment_dir,
            step_table=step_table,
            allow_agent_steps=allow_agent_steps,
            dry_run=dry_run,
        )
        return 3

    monkeypatch.setattr(drive_mod, "drive_once", fake_drive_once)

    code = drive_mod.drive(
        ["--experiment-dir", "/x", "--allow-agent-steps", "--dry-run"],
        step_table=_CAMPAIGN,
        resolver=_unused_resolver,
        prog="p",
        description="d",
    )

    assert code == 3
    assert captured["experiment_dir"] == Path("/x")
    assert captured["allow_agent_steps"] is True
    assert captured["dry_run"] is True
    assert captured["step_table"] is _CAMPAIGN


def test_campaign_config_step_table_is_immutable():
    """`CampaignLoopConfig` is frozen; its step_table default must be genuinely
    immutable, not a mutable alias of the shared module global."""
    import pytest

    config = CampaignLoopConfig()
    with pytest.raises(TypeError):
        config.step_table["sneak"] = "x"  # type: ignore[index]


def _unused_resolver(_spawn_request, _experiment_dir):  # pragma: no cover - never called
    raise AssertionError("resolver must not be called for a cli/dry-run path")
