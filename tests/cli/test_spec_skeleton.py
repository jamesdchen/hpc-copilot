"""Refusals carry a valid skeleton — notebook-audit.md queue item 14 / Addendum 9.

A ``--spec`` (or MCP tool-call spec) that fails JSON-schema validation now
refuses with a ``spec_skeleton`` field: a code-generated MINIMAL VALID instance
of the schema. These tests pin (a) the pure ``build_spec_skeleton`` generator
against real verb schemas, (b) the run-#11 case (submit-s3 missing ``monitor``),
(c) the envelope wiring, and (d) the size cap.
"""

from __future__ import annotations

import json
from importlib.resources import files as _resource_files
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.contract.schema import validate as _schema_validate
from hpc_agent.cli._helpers import (
    _bounded_skeleton,
    _err_from_hpc,
    _merge_all_of,
    _resolve_local_ref,
    _validate_against_schema,
    build_spec_skeleton,
)

from ._helpers import parse_envelope as _parse_envelope
from ._helpers import run_cli as _run_cli


def _load_schema(name: str) -> dict[str, Any]:
    text = (_resource_files("hpc_agent.schemas") / f"{name}.input.json").read_text(encoding="utf-8")
    return dict(json.loads(text))


# ─── test-side schema walk: concretize the skeleton into a VALID instance ──
#
# The skeleton uses typed placeholders for required strings ("<string: ...>").
# To prove the skeleton is STRUCTURALLY a valid instance, we replace each
# placeholder with a pattern-correct dummy (walking the schema alongside the
# instance) and assert the result validates. Numbers / booleans / nested
# required objects the skeleton already fills concretely.


def _leaf(node: Any, root: dict[str, Any]) -> dict[str, Any]:
    """Resolve *node* to its effective leaf schema (ref / allOf / first union)."""
    if not isinstance(node, dict):
        return {}
    node = _resolve_local_ref(node, root)
    if "allOf" in node:
        node = _merge_all_of(node, root)
    for union_key in ("oneOf", "anyOf"):
        branches = node.get(union_key)
        if isinstance(branches, list) and branches:
            dicts = [b for b in branches if isinstance(b, dict)]
            non_null = [b for b in dicts if b.get("type") != "null"]
            chosen = (non_null or dicts)[0]
            return _leaf(chosen, root)
    return dict(node)


def _concretize(instance: Any, node: Any, root: dict[str, Any]) -> Any:
    node = _leaf(node, root)
    if isinstance(instance, dict):
        props = node.get("properties", {})
        return {k: _concretize(v, props.get(k, {}), root) for k, v in instance.items()}
    if isinstance(instance, list):
        return instance
    if isinstance(instance, str) and instance.startswith("<string:"):
        pattern = node.get("pattern", "")
        if "@" in pattern:  # ssh_target ^[^@]+@[^@]+$
            return "user@host"
        return "dummy"
    return instance


# ─── the pure generator ────────────────────────────────────────────────────


def test_build_spec_skeleton_submit_s3_shape() -> None:
    """The run-#11 case: submit-s3's skeleton names every required field,
    monitor resolves through its $ref to {run_id: <placeholder>}."""
    schema = _load_schema("submit_s3")
    skel = build_spec_skeleton(schema)

    assert isinstance(skel, dict)
    # Top-level required present, optional (canary_run_id, detach, ...) omitted.
    assert set(skel) == {"submit", "monitor", "invocation_argv"}
    assert "detach" not in skel  # optional-with-default → omitted
    # monitor is a $ref-resolved object whose only required member is run_id.
    assert isinstance(skel["monitor"], dict)
    assert set(skel["monitor"]) == {"run_id"}
    assert skel["monitor"]["run_id"].startswith("<string:")
    # submit → SubmitAndVerifySpec.submit → SubmitFlowSpec required-only.
    assert set(skel["submit"]) == {"submit"}
    inner = skel["submit"]["submit"]
    assert set(inner) == {
        "profile",
        "cluster",
        "ssh_target",
        "remote_path",
        "job_name",
        "run_id",
        "total_tasks",
        "backend",
        "script",
        "job_env",
    }
    assert inner["total_tasks"] == 1  # minimum-aware integer placeholder
    assert inner["job_env"] == {}  # required object, no required members
    assert isinstance(skel["invocation_argv"], str)


def test_skeleton_validates_after_placeholder_substitution() -> None:
    """For real verb schemas the generated skeleton is structurally a valid
    instance: fill the typed placeholders with type-correct dummies and it
    passes the same validator the CLI uses."""
    for name in ("submit_s3", "notebook_audit_view", "append_decision"):
        schema = _load_schema(name)
        skel = build_spec_skeleton(schema)
        concrete = _concretize(skel, schema, schema)
        # Must not raise — the skeleton had every required field in the right
        # place; only placeholder leaves needed a concrete dummy.
        _schema_validate(concrete, schema)


def test_skeleton_defaults_and_enum_are_concrete() -> None:
    """A declared default / enum surfaces as a real value, not a placeholder."""
    schema = _load_schema("append_decision")
    skel = build_spec_skeleton(schema)
    # scope_kind is an enum → first member, not a placeholder.
    assert skel["scope_kind"] == "run"
    # response / block / scope_id are required plain strings → placeholders.
    assert skel["response"].startswith("<string:")
    # evidence_digest / proposal are optional (have defaults) → omitted.
    assert "evidence_digest" not in skel
    assert "proposal" not in skel


def test_build_spec_skeleton_depth_cap_terminates() -> None:
    """A self-referential schema cannot blow the recursion — the depth cap
    returns None past the bound instead of recursing forever."""
    recursive = {
        "type": "object",
        "required": ["child"],
        "properties": {"child": {"$ref": "#/$defs/Node"}},
        "$defs": {
            "Node": {
                "type": "object",
                "required": ["child"],
                "properties": {"child": {"$ref": "#/$defs/Node"}},
            }
        },
    }
    skel = build_spec_skeleton(recursive)
    # Terminates; the deepest child bottoms out at None (the cap sentinel).
    depth = 0
    node: Any = skel
    while isinstance(node, dict) and "child" in node:
        node = node["child"]
        depth += 1
        assert depth < 50, "depth cap failed to terminate"
    assert node is None


# ─── the size cap ──────────────────────────────────────────────────────────


def test_bounded_skeleton_truncates_over_cap() -> None:
    """An oversized skeleton is truncated to its top-level keys with a note."""
    big = {f"field_{i}": "x" * 200 for i in range(60)}
    assert len(json.dumps(big).encode("utf-8")) > 4096
    out = _bounded_skeleton(big)
    assert "_truncated" in out
    assert all(out[k] == "<see schema>" for k in big)


def test_bounded_skeleton_passthrough_under_cap() -> None:
    """A small skeleton rides through unchanged."""
    small = {"run_id": "<string: Run Id>"}
    assert _bounded_skeleton(small) == small


# ─── envelope wiring (the wire contract the caller reads) ──────────────────


def _spec_invalid_from(name: str, payload: dict[str, Any]) -> errors.SpecInvalid:
    try:
        _validate_against_schema(payload, name)
    except errors.SpecInvalid as exc:
        return exc
    raise AssertionError(f"{name} spec unexpectedly validated: {payload!r}")


def test_validation_failure_attaches_skeleton() -> None:
    """A submit-s3 spec missing `monitor` raises SpecInvalid carrying a
    spec_skeleton whose monitor names run_id."""
    exc = _spec_invalid_from(
        "submit_s3",
        {"submit": {}, "invocation_argv": "x"},  # missing monitor (+ bad submit)
    )
    skel = getattr(exc, "spec_skeleton", None)
    assert isinstance(skel, dict)
    assert "monitor" in skel and "run_id" in skel["monitor"]


def test_err_envelope_carries_spec_skeleton(capsys: Any) -> None:
    """_err_from_hpc rides the attached skeleton into the refusal envelope."""
    exc = _spec_invalid_from("append_decision", {"scope_kind": "run"})  # missing required
    _err_from_hpc(exc)
    env = _parse_envelope(capsys.readouterr().out)
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    assert isinstance(env.get("spec_skeleton"), dict)
    # Names every required field the caller must supply.
    assert {"scope_kind", "scope_id", "block", "response"} <= set(env["spec_skeleton"])


def test_plain_spec_invalid_has_no_skeleton_field(capsys: Any) -> None:
    """A non-schema spec_invalid (no attached skeleton) emits no spec_skeleton."""
    _err_from_hpc(errors.SpecInvalid("bad json"))
    env = _parse_envelope(capsys.readouterr().out)
    assert env["error_code"] == "spec_invalid"
    assert "spec_skeleton" not in env


def test_valid_spec_produces_no_skeleton() -> None:
    """A spec that passes validation raises nothing — no refusal, no skeleton."""
    valid = {
        "scope_kind": "run",
        "scope_id": "run-abc",
        "block": "greenlight",
        "response": "y",
    }
    # Must not raise.
    _validate_against_schema(valid, "append_decision")


# ─── end-to-end over the CLI wire (subprocess) ─────────────────────────────


def test_cli_refusal_carries_spec_skeleton(tmp_path: Any) -> None:
    """Full wire: an under-specified --spec exits spec_invalid AND the JSON
    envelope on stdout carries a spec_skeleton the caller can fill in."""
    spec = tmp_path / "incomplete.json"
    spec.write_text(json.dumps({"profile": "x"}))  # missing most required fields
    rc, out, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
    )
    assert rc == 1
    env = _parse_envelope(out)
    assert env["error_code"] == "spec_invalid"
    skel = env.get("spec_skeleton")
    assert isinstance(skel, dict)
    # Names the required submit fields (job_ids is a required array → []).
    assert {"profile", "cluster", "ssh_target", "run_id", "total_tasks"} <= set(skel)
