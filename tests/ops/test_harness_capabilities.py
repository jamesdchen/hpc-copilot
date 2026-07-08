"""``harness-capabilities`` — the detection-as-negotiation query verb.

Covers the detection paths (settings.json with / without each hook entry; the
utterance-log namespace present / absent), fail-open on an unreadable settings
file, the ``"unknown"`` trusted-display non-answer, the elicitation server/client
evidence split, and the empty-spec / bogus-key wire contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from hpc_agent._wire.queries.harness_capabilities import HarnessCapabilitiesSpec
from hpc_agent.agent_assets import (
    _ANSWER_CAPTURE_NEEDLE,
    _RELAY_AUDIT_NEEDLE,
    _UTTERANCE_CAPTURE_NEEDLE,
)
from hpc_agent.ops.harness_capabilities import harness_capabilities


def _hook_entry(needle: str) -> dict:
    """A settings.json hook entry whose command mentions *needle* (module path)."""
    return {"hooks": [{"type": "command", "command": f"python -m {needle}"}]}


def _write_settings(claude_dir: Path, hooks: dict) -> None:
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


@pytest.fixture
def claude_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A hermetic ``CLAUDE_CONFIG_DIR`` the verb reads settings.json from."""
    d = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    return d


def test_all_channels_installed(claude_dir: Path, tmp_path: Path) -> None:
    _write_settings(
        claude_dir,
        {
            "UserPromptSubmit": [_hook_entry(_UTTERANCE_CAPTURE_NEEDLE)],
            "PostToolUse": [_hook_entry(_ANSWER_CAPTURE_NEEDLE)],
            "Stop": [_hook_entry(_RELAY_AUDIT_NEEDLE)],
        },
    )
    result = harness_capabilities(experiment_dir=tmp_path, spec=HarnessCapabilitiesSpec())
    caps = result.capabilities

    assert caps["utterance_log"].present is True
    assert caps["utterance_log"].evidence["utterance_capture_hook"] is True
    assert caps["utterance_log"].evidence["answer_capture_hook"] is True
    assert caps["relay_enforcement"].present is True
    # Backgrounding is core-side — always present regardless of config.
    assert caps["backgrounding"].present is True
    # Trusted display has no detection seam — the honest non-answer.
    assert caps["trusted_display"].present == "unknown"


def test_no_channels_installed(claude_dir: Path, tmp_path: Path) -> None:
    _write_settings(claude_dir, {})
    result = harness_capabilities(experiment_dir=tmp_path, spec=HarnessCapabilitiesSpec())
    caps = result.capabilities

    assert caps["utterance_log"].present is False
    assert caps["utterance_log"].evidence["utterance_capture_hook"] is False
    assert caps["utterance_log"].evidence["answer_capture_hook"] is False
    assert caps["relay_enforcement"].present is False
    # Still always present (core machinery), and still unknown for display.
    assert caps["backgrounding"].present is True
    assert caps["trusted_display"].present == "unknown"


def test_partial_channels(claude_dir: Path, tmp_path: Path) -> None:
    # Only the utterance-capture channel; no relay hook.
    _write_settings(claude_dir, {"UserPromptSubmit": [_hook_entry(_UTTERANCE_CAPTURE_NEEDLE)]})
    result = harness_capabilities(experiment_dir=tmp_path, spec=HarnessCapabilitiesSpec())
    assert result.capabilities["utterance_log"].present is True
    assert result.capabilities["utterance_log"].evidence["answer_capture_hook"] is False
    assert result.capabilities["relay_enforcement"].present is False


def test_missing_settings_file_fails_open(claude_dir: Path, tmp_path: Path) -> None:
    # claude_dir never created -> settings.json absent -> "no channels", no error.
    result = harness_capabilities(experiment_dir=tmp_path, spec=HarnessCapabilitiesSpec())
    assert result.capabilities["utterance_log"].present is False
    assert result.capabilities["relay_enforcement"].present is False


def test_unreadable_settings_fails_open(claude_dir: Path, tmp_path: Path) -> None:
    # A settings.json that is not valid JSON -> fail-open to no channels.
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text("{ this is not json", encoding="utf-8")
    result = harness_capabilities(experiment_dir=tmp_path, spec=HarnessCapabilitiesSpec())
    assert result.capabilities["utterance_log"].present is False


def test_non_object_settings_fails_open(claude_dir: Path, tmp_path: Path) -> None:
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text("[1, 2, 3]", encoding="utf-8")
    result = harness_capabilities(experiment_dir=tmp_path, spec=HarnessCapabilitiesSpec())
    assert result.capabilities["relay_enforcement"].present is False


def test_utterance_log_namespace_present(
    claude_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """log_present_for_repo reflects the repo's utterance-log file existence
    (non-creating read via state.utterances)."""
    from hpc_agent.state.utterances import utterances_path

    journal = tmp_path / "journal"
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(journal))
    _write_settings(claude_dir, {})

    exp_dir = tmp_path / "repo"
    exp_dir.mkdir()

    # Absent first.
    r0 = harness_capabilities(experiment_dir=exp_dir, spec=HarnessCapabilitiesSpec())
    assert r0.capabilities["utterance_log"].evidence["log_present_for_repo"] is False

    # Now materialize the namespace + log.
    path = utterances_path(exp_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")

    r1 = harness_capabilities(experiment_dir=exp_dir, spec=HarnessCapabilitiesSpec())
    assert r1.capabilities["utterance_log"].evidence["log_present_for_repo"] is True


def test_elicitation_flag_reported(claude_dir: Path, tmp_path: Path) -> None:
    # The server bit is identity with the imported flag (which flips as the pump
    # lands — assert identity, never a literal). The client bit is "per-session":
    # a separate-process probe cannot witness a live session's negotiation.
    from hpc_agent._kernel.extension.mcp_server import ELICITATION_SERVER_IMPLEMENTED

    _write_settings(claude_dir, {})
    result = harness_capabilities(experiment_dir=tmp_path, spec=HarnessCapabilitiesSpec())
    evidence = result.capabilities["utterance_log"].evidence
    assert evidence["elicitation_server"] is ELICITATION_SERVER_IMPLEMENTED
    assert evidence["elicitation_client"] == "per-session"


def test_tier_consequences_present_for_every_capability(claude_dir: Path, tmp_path: Path) -> None:
    _write_settings(claude_dir, {})
    result = harness_capabilities(experiment_dir=tmp_path, spec=HarnessCapabilitiesSpec())
    # Every capability names the tier its absence degrades to.
    assert set(result.tier_consequences) == set(result.capabilities)
    assert "_harness_human_texts" in result.tier_consequences["utterance_log"]
    assert result.tier_consequences["relay_enforcement"]


def test_spec_accepts_empty_rejects_bogus_key() -> None:
    # {} is the valid empty spec (all-optional).
    HarnessCapabilitiesSpec.model_validate({})
    # A bogus key is rejected (extra="forbid") — the EMPTY_SPEC_OVERRIDES probe.
    with pytest.raises(ValidationError):
        HarnessCapabilitiesSpec.model_validate({"contract-probe-bogus-key": 1})


def test_spec_none_defaults_to_empty(claude_dir: Path, tmp_path: Path) -> None:
    # spec_required=False path: no --spec -> dispatch passes spec=None.
    _write_settings(claude_dir, {})
    result = harness_capabilities(experiment_dir=tmp_path, spec=None)
    assert "utterance_log" in result.capabilities
