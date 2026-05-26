"""hpc-agent describe — package-data reference resolver for delegated workers."""

from __future__ import annotations

from tests.cli._helpers import parse_envelope, run_cli


def test_describe_resolves_a_procedure() -> None:
    # Worker-prompt procedures (submit / status / aggregate / campaign)
    # take precedence over the inline-skill lookup; describe surfaces
    # the same body the spawn pipeline inlines.
    rc, out, _ = run_cli("describe", "submit")
    assert rc == 0
    env = parse_envelope(out)
    assert env["ok"] is True
    assert env["data"]["kind"] == "procedure"
    assert env["data"]["name"] == "submit"
    assert "load-context" in env["data"]["content"]


def test_describe_resolves_a_skill() -> None:
    # Inline skills are the agent-autonomous surface; hpc-build-executor
    # is one of two real skills the host still ships.
    rc, out, _ = run_cli("describe", "hpc-build-executor")
    assert rc == 0
    env = parse_envelope(out)
    assert env["ok"] is True
    assert env["data"]["kind"] == "skill"
    assert env["data"]["name"] == "hpc-build-executor"
    assert env["data"]["content"], "skill body should be non-empty"


def test_describe_resolves_a_primitive() -> None:
    rc, out, _ = run_cli("describe", "submit-flow")
    assert rc == 0
    env = parse_envelope(out)
    assert env["data"]["kind"] == "primitive"
    assert env["data"]["content"]["name"] == "submit-flow"


def test_describe_rejects_an_unknown_name() -> None:
    rc, out, _ = run_cli("describe", "no-such-thing")
    assert rc == 1
    env = parse_envelope(out)
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"


def test_describe_rejects_path_traversal() -> None:
    rc, out, _ = run_cli("describe", "../etc/passwd")
    assert rc == 1
    assert parse_envelope(out)["ok"] is False
