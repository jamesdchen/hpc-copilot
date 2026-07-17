"""Boundary contract for ``cite-check``: COMPARE a cited number to a SEALED number
for transcription fidelity — never re-derive, never interpret a metric, never an
LLM in the render path, never a domain-vocabulary wire field.

cite-check is verify-relay's sibling: it audits the human's MANUSCRIPT against the
SEALED corpus. It lives or dies on one line (engineering-principles Q1, "substrate,
not semantics"): it READS the sealed ``aggregated_metrics`` values (comparing a
cited digit to the sealed digit is its whole job — the load-bearing difference from
``extract-recipe``, which is FORBIDDEN from reading them) but never RE-DERIVES them
(no reducer / combine / live-task-tree read) and never INTERPRETS a metric (no
"best", no metric NAME reaching a wire field or the rendered line). The moment the
op re-runs a reducer, the render path imports an LLM/prose module or reaches
``_wire``, a wire field name is drawn from the domain-semantics set, or the
authority pool is built from anything but the sealed artifact, cite-check has
crossed from COMPARISON-under-tolerance into narrating the caller's semantics.

House style: mirrors ``test_extract_recipe_boundary.py`` (AST + a closed
authoritative set kept inline so drift surfaces here).
"""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
from pathlib import Path

from tests._paths import SRC_DIR

_OP_FILE = SRC_DIR / "hpc_agent" / "ops" / "cite_check.py"
_RENDER_FILE = SRC_DIR / "hpc_agent" / "ops" / "cite_render.py"

# Domain-semantics vocabulary the wire must never NAME (field names only) — the
# same closed set the extract-recipe boundary pins.
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

# Modules that would mean cite-check RE-DERIVED the sealed numbers (ran a reducer /
# combiner / walked a live task tree) instead of reading the sealed artifact.
_REDERIVE_IMPORT_MARKERS = ("aggregate_flow", "mapreduce", "combine", "reduce")


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


def test_wire_models_expose_no_domain_vocabulary() -> None:
    """No cite-check wire model has a field NAME drawn from domain semantics."""
    from hpc_agent._wire.queries.cite_check import (
        CiteCheckInput,
        CiteCheckResult,
        CiteFinding,
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

    for model in (CiteCheckInput, CiteCheckResult, CiteFinding):
        names = _property_names(model.model_json_schema())
        leaked = names & _FORBIDDEN_FIELD_NAMES
        assert not leaked, (
            f"{model.__name__} exposes domain-semantics field name(s) {sorted(leaked)}. "
            "cite-check COMPARES a number to a number; a metric-named field is the "
            "substrate-vs-semantics leak (engineering-principles Q1)."
        )


def test_render_path_imports_nothing_llm_adjacent_and_no_wire() -> None:
    """The render module imports nothing LLM-adjacent and never reaches ``_wire``."""
    mods = _imported_modules(_tree(_RENDER_FILE))
    for mod in mods:
        low = mod.lower()
        assert not any(marker in low for marker in _LLM_IMPORT_MARKERS), (
            f"cite_render.py imports {mod!r} — the render path must not reach for "
            "LLM/prose generation; it deterministically formats records."
        )
        assert not low.startswith("hpc_agent._wire"), (
            f"cite_render.py imports {mod!r} from _wire — the render path is "
            "wire-free (the ops op owns the Pydantic boundary)."
        )


def test_render_entry_point_takes_no_free_prose_parameter() -> None:
    """``render_cite_check`` accepts no free-prose input parameter."""
    from hpc_agent.ops.cite_render import render_cite_check

    params = set(inspect.signature(render_cite_check).parameters)
    forbidden = {"prose", "summary", "narrative", "text", "note", "commentary"}
    assert not (params & forbidden), (
        "render_cite_check exposes a free-prose parameter — the render path must not "
        "accept generated narrative."
    )


def test_op_never_re_derives_the_sealed_numbers() -> None:
    """The op reads the SEALED artifact; it never imports a reducer / combiner.

    Re-running the reduce would let cite-check disagree with the sealed table it is
    meant to audit against. It reads ``metrics_aggregate.json`` from disk (via its
    own ``_read_json``), never through the aggregate/combine machinery.
    """
    mods = _imported_modules(_tree(_OP_FILE))
    for mod in mods:
        low = mod.lower()
        assert not any(marker in low for marker in _REDERIVE_IMPORT_MARKERS), (
            f"cite_check.py imports {mod!r} — a reducer/combiner import means the op "
            "RE-DERIVED the sealed numbers instead of reading the sealed artifact."
        )


def test_op_reads_the_sealed_values_and_never_interprets_them() -> None:
    """cite-check MUST read the sealed values — and never reads a metric's NAME.

    A number cited from the sealed ``aggregated_metrics`` body comes back ``matched``
    (proving the op READ the value — the opposite of extract-recipe, which must NOT).
    A metric NAME present only as a key in the sealed body never appears in the
    output (the op compares values, never interprets a metric).
    """
    from hpc_agent._wire.queries.cite_check import CiteCheckInput
    from hpc_agent.ops.cite_check import cite_check

    with tempfile.TemporaryDirectory() as d:
        exp = Path(d)
        agg = exp / "_aggregated" / "run-cc-01" / "metrics_aggregate.json"
        agg.parent.mkdir(parents=True, exist_ok=True)
        sealed_value = "0.9427"
        secret_metric_name = "qlike_zzz_secret"
        agg.write_text(
            json.dumps(
                {
                    "aggregated_metrics": {"run-cc-01": {secret_metric_name: sealed_value}},
                    "provenance": {"source": "local_reduce", "contributing_run_ids": []},
                }
            ),
            encoding="utf-8",
        )
        manuscript = f"Our headline figure is {sealed_value}, an improvement."
        result = cite_check(
            exp, spec=CiteCheckInput(manuscript_text=manuscript, run_id="run-cc-01")
        )

        # It READ the sealed value: the cited digit is matched.
        kinds = {(f["claim"], f["kind"]) for f in result["findings"]}
        assert (sealed_value, "matched") in kinds, (
            "cite-check did not read the sealed value — the cited digit should be "
            f"matched. findings={result['findings']}"
        )
        # It never INTERPRETS the metric: the metric NAME never leaks into the output.
        assert secret_metric_name not in json.dumps(result), (
            "a sealed metric NAME leaked into the cite-check output (it must compare "
            "values, never name/interpret a metric)."
        )


def test_pool_built_only_from_the_sealed_artifact() -> None:
    """A number present only OUTSIDE the sealed artifact is uncitable, not matched.

    The authority is the sealed ``aggregated_metrics`` values ALONE. A number that
    lives only in, say, a run sidecar or a live record — never in the sealed table —
    must not back a manuscript digit.
    """
    from hpc_agent._wire.queries.cite_check import CiteCheckInput
    from hpc_agent.ops.cite_check import cite_check

    with tempfile.TemporaryDirectory() as d:
        exp = Path(d)
        agg = exp / "_aggregated" / "run-cc-02" / "metrics_aggregate.json"
        agg.parent.mkdir(parents=True, exist_ok=True)
        agg.write_text(
            json.dumps({"aggregated_metrics": {"run-cc-02": {"m": "0.5000"}}}),
            encoding="utf-8",
        )
        # 0.8123 is NOT in the sealed table — it must be uncitable.
        result = cite_check(
            exp,
            spec=CiteCheckInput(manuscript_text="We report 0.8123 accuracy.", run_id="run-cc-02"),
        )
        uncitable = {f["claim"] for f in result["findings"] if f["kind"] == "uncitable"}
        assert "0.8123" in uncitable, (
            f"a number absent from the sealed table should be uncitable: {result['findings']}"
        )
