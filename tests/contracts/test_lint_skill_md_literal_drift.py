"""Cross-file lint: every ``<field>: "<literal>"`` in SKILL.md / worker
prompts validates against the corresponding Pydantic Literal.

The bug class that motivated this lint: a Pydantic spec declares
``classified_by: Literal["interview", "recall", "manual"]`` while
the hpc-classify-axis SKILL.md prescribes
``classified_by: "agent"`` at the autonomous-classification step.
The schema rejects the SKILL.md-prescribed value at the boundary,
hard-failing the entire path with ``spec_invalid``. There were no
tests of that path end-to-end (the test mocks bypassed the schema),
so the drift sat for releases.

This lint scans SKILL.md (and the worker prompts) for literal-field
assignments and, for each field in :data:`_KNOWN_LITERAL_FIELDS`,
constructs the corresponding Pydantic spec with that value and
asserts validation succeeds. Add an entry to ``_KNOWN_LITERAL_FIELDS``
whenever a new ``Literal[...]`` field becomes a prose-prescription
point in any SKILL.md or worker procedure.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from hpc_agent._wire.actions.classify_axis import ClassifyAxisInput
from hpc_agent._wire.fixtures.axes import _ExecutorEntry
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _scan_dirs() -> list[Path]:
    out: list[Path] = []
    for rel in (
        "src/slash_commands",
        "src/hpc_agent/_kernel/extension/worker_prompts",
    ):
        d = REPO_ROOT / rel
        if d.is_dir():
            out.extend(p for p in d.rglob("*.md") if p.is_file())
    return out


# Map a Literal-typed Pydantic field to the spec models it appears on
# AND the minimum kwargs needed to construct a valid instance besides
# the field under test. When the lint walks ``<field>: "<value>"``,
# it looks up the field name here, then for every (model, base_kwargs)
# entry constructs ``model(**base_kwargs, **{field: value})``; failure
# means the prose prescribes a value the schema rejects.
#
# Add an entry whenever a new ``Literal[...]`` field appears in any
# prose surface (SKILL.md, worker_prompts/*.md, slash_commands/*.md).
_KNOWN_LITERAL_FIELDS: dict[str, list[tuple[type[BaseModel], dict[str, Any]]]] = {
    "classified_by": [
        (
            ClassifyAxisInput,
            {
                "run_name": "demo_run",
                "run_signature_sha": "a" * 64,
                "data_axis": {"kind": "sequential"},
            },
        ),
        (
            _ExecutorEntry,
            {
                "run_signature_sha": "a" * 64,
                "data_axis": {"kind": "sequential"},
                "classified_at": "2026-05-27T00:00:00+00:00",
            },
        ),
    ],
    "mode": [
        (
            AggregateFlowSpec,
            {"run_id": "demo_run"},
        ),
    ],
}

# Pattern: a field name (alphanumeric / underscore), then ``:``, then a
# double-quoted literal value. Matches inside JSON blocks, YAML
# examples, and inline prose. Captures (field_name, value).
_FIELD_VALUE_RE = re.compile(r'"?([a-z_][a-z0-9_]*)"?\s*:\s*"([^"\\]+)"')


def _extract_field_values(text: str) -> list[tuple[int, str, str]]:
    """Return ``(line_no, field, value)`` for every match in *text*."""
    out: list[tuple[int, str, str]] = []
    for m in _FIELD_VALUE_RE.finditer(text):
        field = m.group(1)
        if field not in _KNOWN_LITERAL_FIELDS:
            continue
        ln = text[: m.start()].count("\n") + 1
        out.append((ln, field, m.group(2)))
    return out


def _validates_against(
    model: type[BaseModel], base_kwargs: dict[str, Any], field: str, value: str
) -> bool:
    try:
        model.model_validate({**base_kwargs, field: value})
    except ValidationError:
        return False
    return True


def test_prose_literal_values_validate_against_spec() -> None:
    """Every prose-prescribed ``<field>: "<value>"`` must be accepted
    by at least one of the spec models declared for that field. If a
    prose value is rejected by every declared model, the prose
    prescribes a value the schema doesn't accept — the next call site
    that copies that prescription will hard-fail at the boundary."""
    failures: list[str] = []
    for md_path in _scan_dirs():
        text = md_path.read_text(encoding="utf-8")
        rel = md_path.relative_to(REPO_ROOT).as_posix()
        for ln, field, value in _extract_field_values(text):
            specs = _KNOWN_LITERAL_FIELDS[field]
            if not any(_validates_against(model, base, field, value) for model, base in specs):
                model_names = ", ".join(m.__name__ for m, _ in specs)
                failures.append(
                    f"  {rel}:{ln}: {field}={value!r} rejected by "
                    f"every declared spec ({model_names})"
                )

    if failures:
        raise AssertionError(
            "prose-prescribed literal values rejected by their Pydantic "
            "spec — the next caller that copies the prose will hard-fail "
            "at the schema boundary:\n" + "\n".join(failures) + "\n\nFix options:\n"
            "  * If the prose is right, add the value to the spec's "
            "Literal[...].\n"
            "  * If the spec is right, fix the prose to use an "
            "accepted value.\n"
            "  * If the field isn't a Pydantic Literal at all "
            "(prose-only enum), remove its entry from "
            "_KNOWN_LITERAL_FIELDS in this lint."
        )


def test_known_literal_fields_actually_exist_on_their_models() -> None:
    """Sanity: every ``_KNOWN_LITERAL_FIELDS`` entry references a real
    field name on its declared model. Catches stale entries when a
    model field gets renamed."""
    failures: list[str] = []
    for field, specs in _KNOWN_LITERAL_FIELDS.items():
        for model, _ in specs:
            if field not in model.model_fields:
                failures.append(
                    f"  {field!r} is in _KNOWN_LITERAL_FIELDS but not on "
                    f"{model.__name__}; rename or drop."
                )
    if failures:
        raise AssertionError("\n".join(failures))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
