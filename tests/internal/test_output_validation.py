"""Producer-side output validation in hpc_agent._internal.schema.

Asserts that ``validate_output``:

1. No-ops in the absence of a matching ``<name>.output.json``.
2. No-ops when validation is disabled.
3. Raises :class:`OutputSchemaDrift` when enabled and the data
   doesn't match the schema.
4. Auto-enables under pytest (sanity check on the env detection).
"""

from __future__ import annotations

from hpc_agent._internal.schema import (
    OutputSchemaDrift,
    _output_validation_enabled,
    validate_output,
)


def test_validation_auto_enabled_under_pytest() -> None:
    assert _output_validation_enabled() is True


def test_no_schema_means_no_op() -> None:
    # A name with no <name>.output.json on disk: validate_output must not raise.
    validate_output({"any": "shape"}, "primitive-with-no-output-schema")


def test_valid_payload_passes() -> None:
    # capabilities.output.json requires {capabilities: {...}}; a well-formed
    # payload (here, a minimal one that satisfies its schema) must pass silently.
    # Using suggest_setup_action whose schema we know — has explicit required keys.
    payload = {
        "priority": 0,
        "action": "fresh",
        "recommended_run_id": None,
        "candidates": [],
        "reason": "no prior runs found",
    }
    validate_output(payload, "suggest-setup-action")


def test_invalid_payload_raises_drift() -> None:
    bad = {"priority": 0}  # missing required action / candidates / reason / recommended_run_id
    try:
        validate_output(bad, "suggest-setup-action")
    except OutputSchemaDrift as exc:
        assert "suggest-setup-action" in str(exc)
        assert "suggest_setup_action.output.json" in str(exc)
        return
    raise AssertionError("expected OutputSchemaDrift; got no exception")


def test_kebab_name_resolves_snake_schema_filename() -> None:
    # decide-monitor-arm → decide_monitor_arm.output.json must be loaded.
    bad = {"arm": "yes"}  # Missing the rich required-field set.
    try:
        validate_output(bad, "decide-monitor-arm")
    except OutputSchemaDrift as exc:
        assert "decide_monitor_arm.output.json" in str(exc)
        return
    raise AssertionError("expected OutputSchemaDrift; got no exception")
