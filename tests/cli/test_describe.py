"""hpc-agent describe — package-data reference resolver for delegated workers."""

from __future__ import annotations

from tests.cli._helpers import parse_envelope, run_cli


def test_describe_no_longer_serves_worker_procedures() -> None:
    # Worker-prompt procedures (submit / status / aggregate / campaign) were
    # deleted with the spawn transport in the §6 worker removal — a bare
    # workflow name no longer resolves to a `kind: "procedure"` payload.
    # (CI caught the stale expectation where a local run did not: describe
    # memoizes by (pkg_version, name), and the version string didn't change
    # with the §6 deletion, so a stale local cache kept serving the old
    # procedure payload. Clear ~/.claude/hpc/describe_cache when editing
    # describe's resolution behavior without a version bump.)
    rc, out, _ = run_cli("describe", "submit")
    assert rc == 1
    env = parse_envelope(out)
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"


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


def test_describe_schema_emits_resolved_input_schema_content() -> None:
    # Move 2 (proving-run-2-hardening §3): `--schema` returns the RESOLVED
    # input-schema JSON *content*, not the bare filename — so an agent never
    # `find /`s a schema file. Wire an existing verb with a known input schema.
    rc, out, _ = run_cli("describe", "append-decision", "--schema")
    assert rc == 0
    env = parse_envelope(out)
    assert env["ok"] is True
    assert env["data"]["kind"] == "input_schema"
    assert env["data"]["name"] == "append-decision"
    schema = env["data"]["schema"]
    # A real JSON Schema object, not a filename string.
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"
    assert "properties" in schema
    # append-decision's contract requires these fields.
    assert set(schema.get("required", [])) >= {"scope_kind", "scope_id", "block", "response"}


def test_describe_schema_rejects_an_unknown_name() -> None:
    rc, out, _ = run_cli("describe", "no-such-thing", "--schema")
    assert rc == 1
    env = parse_envelope(out)
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
