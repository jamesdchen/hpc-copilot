"""Tests for the typed exception hierarchy in slash_commands.errors.

The contract these enforce: every documented error_code value has a
matching HpcError subclass, every subclass has the required attributes,
and the CLI envelope picks them up consistently. Adding a new error_code
without updating these tests is a contract break that should fail CI.
"""

from __future__ import annotations

import json
import re

import pytest

from hpc_agent import errors

# error_codes documented in docs/reference/cli-spec.md and shipped as the envelope
# JSON Schema enum. Source of truth for the contract.
DOCUMENTED_ERROR_CODES = frozenset(
    {
        "ssh_unreachable",
        "ssh_circuit_open",
        "model_endpoint_error",
        "scheduler_throttled",
        "spec_invalid",
        "executor_not_found",
        "cluster_unknown",
        "journal_corrupt",
        "remote_command_failed",
        "config_invalid",
        "combiner_failed",
        "cluster_timeout",
        "cluster_partially_degraded",
        "outputs_missing",
        "schema_incompat",
        "preempted",
        "precondition_failed",
        "internal",
    }
)

# Subclasses we expect (the "internal" code is the HpcError default and has
# no dedicated subclass — it's the catch-all for unclassified failures).
EXPECTED_SUBCLASSES = {
    "ssh_unreachable": errors.SshUnreachable,
    "ssh_circuit_open": errors.SshCircuitOpen,
    "model_endpoint_error": errors.ModelEndpointError,
    "scheduler_throttled": errors.SchedulerThrottled,
    "spec_invalid": errors.SpecInvalid,
    "executor_not_found": errors.ExecutorNotFound,
    "cluster_unknown": errors.ClusterUnknown,
    "journal_corrupt": errors.JournalCorrupt,
    "remote_command_failed": errors.RemoteCommandFailed,
    "config_invalid": errors.ConfigInvalid,
    "combiner_failed": errors.CombinerFailed,
    "cluster_timeout": errors.ClusterTimeout,
    "cluster_partially_degraded": errors.ClusterPartiallyDegraded,
    "outputs_missing": errors.OutputsMissing,
    "schema_incompat": errors.SchemaIncompat,
    "preempted": errors.Preempted,
    "precondition_failed": errors.PreconditionFailed,
}


@pytest.mark.parametrize("code,cls", sorted(EXPECTED_SUBCLASSES.items()))
def test_subclass_has_required_attributes(code: str, cls: type[errors.HpcError]) -> None:
    """Every subclass must declare error_code, retry_safe, category, remediation."""
    assert cls.error_code == code
    assert isinstance(cls.retry_safe, bool)
    assert cls.category in {"user", "cluster", "network", "internal"}
    # remediation is the agent-actionable hint; required for every documented code.
    assert cls.remediation is not None
    assert isinstance(cls.remediation, str)
    assert cls.remediation, "remediation must be non-empty"


def test_envelope_schema_enum_matches_subclass_inventory() -> None:
    """The error_code enum in envelope.json must match the documented set.

    The schema is now Pydantic-emitted: ``oneOf`` references
    ``#/$defs/ErrorEnvelope`` and ``#/$defs/SuccessEnvelope``
    instead of inlining the variants. Resolve the ref to find the
    error variant before reading its ``error_code`` enum.
    """
    from tests._paths import SCHEMAS_DIR

    schema_path = SCHEMAS_DIR / "envelope.json"
    schema = json.loads(schema_path.read_text())
    defs = schema.get("$defs", {})

    def _resolve(node: dict) -> dict:
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            resolved = defs[ref.removeprefix("#/$defs/")]
            assert isinstance(resolved, dict)
            return resolved
        return node

    error_variant = next(
        resolved
        for v in schema["oneOf"]
        for resolved in [_resolve(v)]
        if resolved.get("properties", {}).get("ok", {}).get("const") is False
    )
    enum_in_schema = frozenset(error_variant["properties"]["error_code"]["enum"])
    assert enum_in_schema == DOCUMENTED_ERROR_CODES, (
        f"envelope.json error_code enum drifted from documented set:\n"
        f"  schema: {sorted(enum_in_schema)}\n"
        f"  docs:   {sorted(DOCUMENTED_ERROR_CODES)}"
    )


def test_subclass_inventory_covers_documented_codes() -> None:
    """No documented error_code should lack a subclass (except 'internal')."""
    expected_classes = DOCUMENTED_ERROR_CODES - {"internal"}
    actual_classes = frozenset(EXPECTED_SUBCLASSES)
    assert expected_classes == actual_classes


def test_per_call_remediation_override() -> None:
    """Instances may override remediation when they have host-specific context."""
    exc = errors.SshUnreachable("conn refused", remediation="check VPN")
    assert exc.remediation == "check VPN"
    # Without override, the class default applies.
    bare = errors.SshUnreachable("conn refused")
    assert bare.remediation == errors.SshUnreachable.remediation


def test_hpc_error_is_exception() -> None:
    """All typed errors must be raisable; pytest.raises(HpcError) must catch them."""
    for cls in EXPECTED_SUBCLASSES.values():
        with pytest.raises(errors.HpcError):
            raise cls("test")


def test_remediation_strings_are_actionable() -> None:
    """Remediation hints should describe a concrete fix, not just describe the failure.

    Heuristic: contains an imperative verb (verify, check, run, set, install,
    inspect, validate, ...). Keeps remediation strings useful to agents.
    """
    imperative_verbs = re.compile(
        r"\b(verify|check|run|set|install|inspect|validate|serialize|forward|ensure|configure|update|resubmit)\b",
        re.IGNORECASE,
    )
    for cls in EXPECTED_SUBCLASSES.values():
        assert cls.remediation is not None
        assert imperative_verbs.search(cls.remediation), (
            f"{cls.__name__}.remediation must include an imperative verb; got: {cls.remediation!r}"
        )
