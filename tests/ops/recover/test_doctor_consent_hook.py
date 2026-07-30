"""``doctor``'s consent-forwarding-hook drift check (attended-latency plan).

The hook that forwards a journaled greenlight to the harness permission layer
fails SILENTLY: when its ``settings.json`` entry is missing, stale, or
duplicated, nothing breaks — the human is simply re-asked for consent they
already typed, which reads as ordinary friction rather than a defect. Making
that visible is what ``doctor`` is for.

Pinned here: each drift shape is reported, a healthy install is silent, a
config that is not ours is silent (the anti-nag scope), and the check never
flips ``needs_attention`` (a re-ask is friction, not a stalled driver) and
never breaks the scan.

The autouse fixture in this package's ``conftest.py`` gives every test a
hermetic ``CLAUDE_CONFIG_DIR``; these tests populate it explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent._wire.queries.doctor import DoctorSpec
from hpc_agent.agent_assets import install_agent_assets
from hpc_agent.ops.recover.doctor import _consent_forward_hook_probe, doctor

_MODULE = "hpc_agent._kernel.hooks.consent_forward"
_NOW = "2026-07-30T00:00:00+00:00"


@pytest.fixture
def claude_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A hermetic harness config dir the probe resolves to."""
    d = tmp_path / "claude_cfg"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    return d


def _experiment(tmp_path: Path) -> Path:
    exp = tmp_path / "exp"
    exp.mkdir(exist_ok=True)
    return exp


def _messages(alerts: list) -> str:
    return " ".join(a.message for a in alerts)


# ─── silent when there is nothing to report ─────────────────────────────────


def test_healthy_install_is_silent(claude_dir: Path) -> None:
    install_agent_assets(claude_dir=claude_dir)
    assert _consent_forward_hook_probe(_NOW) == []


def test_absent_config_is_silent(claude_dir: Path) -> None:
    """No settings.json at all — hpc-agent was never installed into a harness here."""
    assert not (claude_dir / "settings.json").exists()
    assert _consent_forward_hook_probe(_NOW) == []


def test_foreign_config_is_silent(claude_dir: Path) -> None:
    """Someone else's settings.json is not our install — nagging about it is noise."""
    (claude_dir / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": "ruff check"}]}]}}),
        encoding="utf-8",
    )
    assert _consent_forward_hook_probe(_NOW) == []


def test_unparseable_config_is_silent(claude_dir: Path) -> None:
    (claude_dir / "settings.json").write_text("{ not json", encoding="utf-8")
    assert _consent_forward_hook_probe(_NOW) == []


# ─── each drift shape reports ───────────────────────────────────────────────


def test_absent_entry_beside_our_other_hooks_reports(claude_dir: Path) -> None:
    """hpc-agent hooks ARE installed but ours is gone — the upgrade-gap shape."""
    install_agent_assets(claude_dir=claude_dir)
    path = claude_dir / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["hooks"]["PreToolUse"] = [
        e for e in settings["hooks"]["PreToolUse"] if _MODULE not in e["hooks"][0]["command"]
    ]
    path.write_text(json.dumps(settings), encoding="utf-8")

    alerts = _consent_forward_hook_probe(_NOW)
    assert len(alerts) == 1
    assert alerts[0].ts == _NOW
    text = alerts[0].message
    assert "ABSENT" in text
    # The alert states the CONSEQUENCE and the remedy, not just the fact.
    assert "re-asks" in text
    assert "install-commands" in text


def test_stale_entry_reports(claude_dir: Path) -> None:
    install_agent_assets(claude_dir=claude_dir)
    path = claude_dir / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    for entry in settings["hooks"]["PreToolUse"]:
        if _MODULE in entry["hooks"][0]["command"]:
            entry["hooks"][0]["command"] = f"/moved/python -m {_MODULE}"
    path.write_text(json.dumps(settings), encoding="utf-8")

    assert "STALE" in _messages(_consent_forward_hook_probe(_NOW))


def test_duplicate_entries_report(claude_dir: Path) -> None:
    """The 539c1cdc shape: two entries racing on one event."""
    install_agent_assets(claude_dir=claude_dir)
    path = claude_dir / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    ours = next(e for e in settings["hooks"]["PreToolUse"] if _MODULE in e["hooks"][0]["command"])
    settings["hooks"]["PreToolUse"].append(json.loads(json.dumps(ours)))
    path.write_text(json.dumps(settings), encoding="utf-8")

    assert "2 entries" in _messages(_consent_forward_hook_probe(_NOW))


def test_a_reinstall_clears_the_alert(claude_dir: Path) -> None:
    """The remedy the alert names actually works — the loop closes."""
    install_agent_assets(claude_dir=claude_dir)
    path = claude_dir / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    for entry in settings["hooks"]["PreToolUse"]:
        if _MODULE in entry["hooks"][0]["command"]:
            entry["hooks"][0]["command"] = f"/moved/python -m {_MODULE}"
    path.write_text(json.dumps(settings), encoding="utf-8")
    assert _consent_forward_hook_probe(_NOW) != []

    install_agent_assets(claude_dir=claude_dir)
    assert _consent_forward_hook_probe(_NOW) == []


# ─── envelope wiring ────────────────────────────────────────────────────────


def test_drift_rides_the_envelope_without_flipping_needs_attention(
    tmp_path: Path, claude_dir: Path
) -> None:
    """A re-ask is friction, not a stalled driver — same posture as the jsonschema probe."""
    install_agent_assets(claude_dir=claude_dir)
    path = claude_dir / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["hooks"]["PreToolUse"] = [
        e for e in settings["hooks"]["PreToolUse"] if _MODULE not in e["hooks"][0]["command"]
    ]
    path.write_text(json.dumps(settings), encoding="utf-8")

    out = doctor(experiment_dir=_experiment(tmp_path), spec=DoctorSpec())
    assert any("consent-forwarding" in a["message"] for a in out["alerts"])
    assert out["needs_attention"] is False


def test_healthy_install_leaves_the_envelope_alert_free(tmp_path: Path, claude_dir: Path) -> None:
    install_agent_assets(claude_dir=claude_dir)
    out = doctor(experiment_dir=_experiment(tmp_path), spec=DoctorSpec())
    assert out["alerts"] == []


def test_probe_never_breaks_the_scan(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-OPEN, unlike the hook it checks: an install probe is not a permission decision."""
    import hpc_agent.agent_assets as assets

    def _boom(**_kwargs: object) -> dict:
        raise RuntimeError("config dir is on a dead network mount")

    monkeypatch.setattr(assets, "consent_forward_hook_status", _boom)
    assert _consent_forward_hook_probe(_NOW) == []
    out = doctor(experiment_dir=_experiment(tmp_path), spec=DoctorSpec())
    assert out["alerts"] == []
