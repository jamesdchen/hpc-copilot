"""Boundary contract for ``extract-recipe``: identity/ordering/counting over
opaque records — never a metric, never a "best" run, never an LLM in the render
path (the ``run_story`` / ``trace`` posture).

The clean-reproduction recipe walks a citable artifact back to its minimal
run-set. It lives or dies on one line (engineering-principles Q1, "substrate, not
semantics"): the recipe knows WHICH runs, at WHICH shas, in WHICH order to
re-derive — and NOTHING about what any metric MEANS. The moment a fingerprint
grows a metric field, a metric VALUE reaches the rendered line, an LLM touches
the render path, or the pack CSV's content is parsed, the recipe has crossed from
IDENTITY+ORDERING+COUNTING into narrating the caller's semantics.

House style: mirrors ``test_run_story_boundary.py`` (AST + a closed authoritative
set kept inline so drift surfaces here).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from tests._paths import SRC_DIR

_OP_FILE = SRC_DIR / "hpc_agent" / "ops" / "extract_recipe.py"
_RENDER_FILE = SRC_DIR / "hpc_agent" / "ops" / "recipe_render.py"

# The identity-only fingerprint legs the recipe projects per run — params / code /
# data / env / the wheel / cluster / profile. A metric name here is the leak.
_IDENTITY_FINGERPRINT = frozenset(
    {
        "cmd_sha",
        "tasks_py_sha",
        "data_sha",
        "data_manifest_sha",
        "env_hash",
        "hpc_agent_version",
        "cluster",
        "profile",
    }
)

# Domain-semantics vocabulary the wire must never NAME (field names only).
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "control",
        "controls",
        "unit",
        "units",
        "metric",
        "metrics",
        "holdout",
        "treatment",
        "baseline",
        "significance",
        "placebo",
        "anchor",
        "accuracy",
        "loss",
    }
)

_LLM_IMPORT_MARKERS = ("anthropic", "openai", "llm", "prompt", "claude_", "generat")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(tree: ast.Module) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_fingerprint_fields_are_identity_only_no_metric() -> None:
    """The projected fingerprint is the identity set — no metric value leg.

    Both the op and the render module key on the SAME closed set; a metric name
    among them would put a caller's semantics into the recipe.
    """
    from hpc_agent.ops.extract_recipe import _FINGERPRINT_FIELDS as op_fields
    from hpc_agent.ops.recipe_render import _FINGERPRINT_FIELDS as render_fields

    assert frozenset(op_fields) == _IDENTITY_FINGERPRINT
    assert frozenset(render_fields) == _IDENTITY_FINGERPRINT
    assert not (_IDENTITY_FINGERPRINT & _FORBIDDEN_FIELD_NAMES)


def test_wire_models_expose_no_domain_vocabulary() -> None:
    """No wire model has a field NAME drawn from domain semantics."""
    from hpc_agent._wire.queries.extract_recipe import (
        ExtractRecipeInput,
        ExtractRecipeResult,
    )

    def _property_names(schema: dict) -> set[str]:
        names: set[str] = set()

        def walk(node: object) -> None:
            if isinstance(node, dict):
                props = node.get("properties")
                if isinstance(props, dict):
                    names.update(k for k in props if isinstance(k, str))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(schema)
        return names

    for model in (ExtractRecipeInput, ExtractRecipeResult):
        names = _property_names(model.model_json_schema())
        leaked = names & _FORBIDDEN_FIELD_NAMES
        assert not leaked, (
            f"{model.__name__} exposes domain-semantics field name(s) {sorted(leaked)}. "
            "The recipe describes IDENTITY + ORDERING + COUNTING; a metric-named "
            "field is the substrate-vs-semantics leak (engineering-principles Q1)."
        )


def test_render_path_imports_nothing_llm_adjacent_and_no_wire() -> None:
    """The render module imports nothing LLM-adjacent and never reaches ``_wire``."""
    mods = _imported_modules(_tree(_RENDER_FILE))
    for mod in mods:
        low = mod.lower()
        assert not any(marker in low for marker in _LLM_IMPORT_MARKERS), (
            f"recipe_render.py imports {mod!r} — the render path must not reach for "
            "LLM/prose generation; it deterministically formats records."
        )
        assert not low.startswith("hpc_agent._wire"), (
            f"recipe_render.py imports {mod!r} from _wire — the render path is "
            "wire-free (the ops op owns the Pydantic boundary)."
        )


def test_render_entry_point_takes_no_free_prose_parameter() -> None:
    """``render_recipe`` accepts no free-prose input parameter."""
    from hpc_agent.ops.recipe_render import render_recipe

    params = set(inspect.signature(render_recipe).parameters)
    forbidden = {"prose", "summary", "narrative", "text", "note", "commentary"}
    assert not (params & forbidden), (
        "render_recipe exposes a free-prose parameter — the render path must not "
        "accept generated narrative."
    )


def test_op_never_reads_the_aggregated_metrics_body() -> None:
    """A metric VALUE in the aggregate's ``aggregated_metrics`` never reaches the recipe.

    The op reads the provenance block for ``contributing_run_ids`` and each run's
    identity fingerprint — never the reduced numbers. A crafted secret metric in
    the aggregated_metrics body must not appear anywhere in the recipe output.
    """
    import json
    import tempfile

    from hpc_agent._wire.queries.extract_recipe import ExtractRecipeInput
    from hpc_agent.ops.extract_recipe import extract_recipe

    with tempfile.TemporaryDirectory() as d:
        exp = Path(d)
        agg = exp / "_aggregated" / "run-metric-01" / "metrics_aggregate.json"
        agg.parent.mkdir(parents=True, exist_ok=True)
        secret = "0.13371337133713"
        agg.write_text(
            json.dumps(
                {
                    "aggregated_metrics": {"run-metric-01": {"accuracy": secret}},
                    "provenance": {"source": "local_reduce", "contributing_run_ids": []},
                }
            ),
            encoding="utf-8",
        )
        recipe = extract_recipe(exp, spec=ExtractRecipeInput(aggregate_path=str(agg)))
        assert secret not in json.dumps(recipe), "a metric value leaked into the recipe"


def test_render_path_never_reads_the_aggregated_tree_bytes() -> None:
    """The render module never touches the ``_aggregated`` tree (it formats records only)."""
    text = _RENDER_FILE.read_text(encoding="utf-8")
    assert "_aggregated" not in text
