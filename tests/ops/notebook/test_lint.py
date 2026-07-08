"""Tests for the ``notebook-lint`` primitive (notebook-audit / T4).

Each rule gets a fire-on-synthetic-violation test AND a passes-on-clean test:
structural completeness (missing slug / reordered slugs / clean subsequence),
executes-live (missing literal fires, existing literal passes, computed path
lands in ``unverifiable_paths``), and linked_sources (a resolving import reports
the right ``module_sha``; a non-resolvable import is ignored). Plus the
findings-are-never-raised envelope property and the malformed-input SpecInvalid
raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.notebook_lint import NotebookLintInput, NotebookLintResult
from hpc_agent.ops.notebook.lint import notebook_lint
from hpc_agent.state.audit_source import sha256_normalized

# ── fixtures / helpers ───────────────────────────────────────────────────────

_TEMPLATE = """\
# %%
# hpc-audit-section: load-data
DATA = "placeholder"

# %%
# hpc-audit-section: fit-model
MODEL = None

# %%
# hpc-audit-section: report
RESULT = None
"""


def _run(
    experiment_dir: Path,
    source: str,
    template: str,
    *,
    input_roots: list[str] | None = None,
    source_roots: list[str] | None = None,
    output_roots: list[str] | None = None,
) -> NotebookLintResult:
    (experiment_dir / "source.py").write_text(source, encoding="utf-8")
    (experiment_dir / "template.py").write_text(template, encoding="utf-8")
    spec = NotebookLintInput(
        source="source.py",
        template="template.py",
        input_roots=input_roots or [],
        source_roots=source_roots or [],
        output_roots=output_roots or [],
    )
    return notebook_lint(experiment_dir=experiment_dir, spec=spec)


def _rules(result: NotebookLintResult, rule: str) -> list:
    return [f for f in result.findings if f.rule == rule]


# ── rule 1: structural completeness ──────────────────────────────────────────


def test_structural_clean_subsequence_passes(tmp_path: Path) -> None:
    source = """\
# %%
# hpc-audit-section: load-data
df = 1

# %%
# a plain follow-on cell (no marker) still belongs to load-data
df = 2

# %%
# hpc-audit-section: fit-model
m = 1

# %%
# hpc-audit-section: report
r = 1
"""
    result = _run(tmp_path, source, _TEMPLATE)
    assert _rules(result, "structural_completeness") == []


def test_structural_missing_slug_fires(tmp_path: Path) -> None:
    source = """\
# %%
# hpc-audit-section: load-data
df = 1

# %%
# hpc-audit-section: report
r = 1
"""
    result = _run(tmp_path, source, _TEMPLATE)
    findings = _rules(result, "structural_completeness")
    assert [f.section for f in findings] == ["fit-model"]
    assert findings[0].evidence["kind"] == "missing"


def test_structural_reordered_slugs_fire(tmp_path: Path) -> None:
    # All three present, but fit-model appears AFTER report — not an
    # order-preserving subsequence of the template order.
    source = """\
# %%
# hpc-audit-section: load-data
df = 1

# %%
# hpc-audit-section: report
r = 1

# %%
# hpc-audit-section: fit-model
m = 1
"""
    result = _run(tmp_path, source, _TEMPLATE)
    findings = _rules(result, "structural_completeness")
    # The greedy two-pointer matches load-data then fit-model (at source index 2),
    # leaving `report` (source index 1, before the cursor) unreachable in order.
    assert [f.section for f in findings] == ["report"]
    assert findings[0].evidence["kind"] == "reordered"


# ── rule 2: executes-live ────────────────────────────────────────────────────


def test_executes_live_existing_literal_passes(tmp_path: Path) -> None:
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "data.csv").write_text("x", encoding="utf-8")
    source = """\
# %%
# hpc-audit-section: load-data
DATA = "inputs/data.csv"
"""
    result = _run(tmp_path, source, _TEMPLATE, input_roots=["inputs"])
    assert _rules(result, "executes_live") == []


def test_executes_live_missing_literal_fires_with_section(tmp_path: Path) -> None:
    source = """\
# %%
# hpc-audit-section: load-data
DATA = "inputs/missing.csv"
"""
    result = _run(tmp_path, source, _TEMPLATE, input_roots=["inputs"])
    findings = _rules(result, "executes_live")
    assert len(findings) == 1
    assert findings[0].section == "load-data"
    assert findings[0].evidence["path"] == "inputs/missing.csv"


def test_executes_live_bare_filename_under_root_passes(tmp_path: Path) -> None:
    # A separator-less literal that resolves under a declared root IS path-shaped
    # (second clause) but exists → no finding.
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "data.csv").write_text("x", encoding="utf-8")
    source = """\
# %%
# hpc-audit-section: load-data
NAME = "data.csv"
"""
    result = _run(tmp_path, source, _TEMPLATE, input_roots=["inputs"])
    assert _rules(result, "executes_live") == []


def test_executes_live_non_path_string_ignored(tmp_path: Path) -> None:
    # A plain string with no separator and no root resolution is not path-shaped.
    source = """\
# %%
# hpc-audit-section: load-data
LABEL = "hello world"
"""
    result = _run(tmp_path, source, _TEMPLATE, input_roots=["inputs"])
    assert _rules(result, "executes_live") == []
    assert result.unverifiable_paths == []


def test_executes_live_computed_fstring_is_unverifiable(tmp_path: Path) -> None:
    source = """\
# %%
# hpc-audit-section: load-data
name = "data"
DATA = f"inputs/{name}.csv"
"""
    result = _run(tmp_path, source, _TEMPLATE, input_roots=["inputs"])
    # No literal finding (the path is computed), but recorded as an honest gap.
    assert _rules(result, "executes_live") == []
    assert any("inputs/" in p for p in result.unverifiable_paths)


def test_executes_live_computed_concat_is_unverifiable(tmp_path: Path) -> None:
    source = """\
# %%
# hpc-audit-section: load-data
name = "data.csv"
DATA = "inputs/" + name
"""
    result = _run(tmp_path, source, _TEMPLATE, input_roots=["inputs"])
    assert _rules(result, "executes_live") == []
    assert any("inputs/" in p for p in result.unverifiable_paths)


def test_executes_live_output_literal_is_declared_not_flagged(tmp_path: Path) -> None:
    # A missing path literal UNDER a declared output_root is a WRITE target —
    # exempt from the not-exists flag, reported in declared_outputs instead.
    source = """\
# %%
# hpc-audit-section: report
OUT = "outputs/summary/table.json"
"""
    result = _run(tmp_path, source, _TEMPLATE, input_roots=["inputs"], output_roots=["outputs"])
    assert _rules(result, "executes_live") == []
    assert [(d.path, d.section) for d in result.declared_outputs] == [
        ("outputs/summary/table.json", "report")
    ]


def test_executes_live_missing_literal_outside_output_roots_still_flags(tmp_path: Path) -> None:
    # A literal under NO root keeps today's behavior: declared output_roots do
    # not exempt a missing literal that sits outside them.
    source = """\
# %%
# hpc-audit-section: load-data
DATA = "inputs/missing.csv"
"""
    result = _run(tmp_path, source, _TEMPLATE, input_roots=["inputs"], output_roots=["outputs"])
    findings = _rules(result, "executes_live")
    assert len(findings) == 1
    assert findings[0].evidence["path"] == "inputs/missing.csv"
    assert result.declared_outputs == []


def test_executes_live_existing_input_literal_not_a_declared_output(tmp_path: Path) -> None:
    # An existing input literal passes as before and never lands in
    # declared_outputs (it is under the input root, not an output root).
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "data.csv").write_text("x", encoding="utf-8")
    source = """\
# %%
# hpc-audit-section: load-data
DATA = "inputs/data.csv"
"""
    result = _run(tmp_path, source, _TEMPLATE, input_roots=["inputs"], output_roots=["outputs"])
    assert _rules(result, "executes_live") == []
    assert result.declared_outputs == []


def test_executes_live_existing_literal_under_output_root_is_declared(tmp_path: Path) -> None:
    # Existence does not change the classification: a literal under an
    # output_root is a declared output whether or not the file exists yet.
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "table.json").write_text("{}", encoding="utf-8")
    source = """\
# %%
# hpc-audit-section: report
OUT = "outputs/table.json"
"""
    result = _run(tmp_path, source, _TEMPLATE, output_roots=["outputs"])
    assert _rules(result, "executes_live") == []
    assert [(d.path, d.section) for d in result.declared_outputs] == [
        ("outputs/table.json", "report")
    ]


# ── rule 3: linked_sources ───────────────────────────────────────────────────


def test_linked_sources_reports_resolving_import_with_module_sha(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    helper_text = "def helper():\n    return 42\n"
    (lib / "helper.py").write_text(helper_text, encoding="utf-8")
    source = """\
# %%
# hpc-audit-section: load-data
from lib import helper
x = helper.helper()
"""
    result = _run(tmp_path, source, _TEMPLATE, source_roots=["."])
    assert len(result.linked_sources) == 1
    link = result.linked_sources[0]
    assert link.module in {"lib.helper", "lib"}
    assert link.file.replace("\\", "/").endswith("lib/helper.py")
    assert link.module_sha == sha256_normalized(helper_text)


def test_linked_sources_ignores_non_resolvable_import(tmp_path: Path) -> None:
    source = """\
# %%
# hpc-audit-section: load-data
import os
import sys
import numpy as np
"""
    result = _run(tmp_path, source, _TEMPLATE, source_roots=["lib"])
    assert result.linked_sources == []


# ── rule 4: template_import_shadowed ─────────────────────────────────────────

# Template whose imports are the "declared engines" (toy names — never domain
# vocabulary): a from-import, an aliased from-import, and a plain import.
_SHADOW_TEMPLATE = """\
# %%
# hpc-audit-section: load-data
from toy.engine import compute_stat, helper as calc
DATA = "placeholder"

# %%
# hpc-audit-section: fit-model
import toy.core
MODEL = None

# %%
# hpc-audit-section: report
RESULT = None
"""


def _shadow_findings(result: NotebookLintResult) -> list:
    return _rules(result, "template_import_shadowed")


def test_shadow_def_of_template_import_fires(tmp_path: Path) -> None:
    source = """\
# %%
# hpc-audit-section: load-data
from toy.engine import compute_stat, helper as calc
DATA = "placeholder"

# %%
# hpc-audit-section: fit-model
def compute_stat(x):
    return x + 1

# %%
# hpc-audit-section: report
RESULT = None
"""
    result = _run(tmp_path, source, _SHADOW_TEMPLATE)
    findings = _shadow_findings(result)
    assert len(findings) == 1
    assert findings[0].section == "fit-model"
    assert findings[0].evidence["name"] == "compute_stat"
    assert findings[0].evidence["template_slug"] == "load-data"
    assert findings[0].evidence["kind"] == "def"


def test_shadow_class_of_aliased_import_fires(tmp_path: Path) -> None:
    # `helper as calc` binds "calc" — a class named calc shadows the alias.
    source = """\
# %%
# hpc-audit-section: load-data
from toy.engine import compute_stat, helper as calc
DATA = "placeholder"

# %%
# hpc-audit-section: report
class calc:
    pass
"""
    result = _run(tmp_path, source, _SHADOW_TEMPLATE)
    findings = _shadow_findings(result)
    assert [(f.section, f.evidence["name"], f.evidence["kind"]) for f in findings] == [
        ("report", "calc", "class")
    ]


def test_shadow_toplevel_assignment_fires(tmp_path: Path) -> None:
    source = """\
# %%
# hpc-audit-section: report
compute_stat = lambda x: x
"""
    result = _run(tmp_path, source, _SHADOW_TEMPLATE)
    findings = _shadow_findings(result)
    assert [(f.section, f.evidence["name"], f.evidence["kind"]) for f in findings] == [
        ("report", "compute_stat", "assignment")
    ]


def test_shadow_different_origin_import_fires(tmp_path: Path) -> None:
    # Same bound name, DIFFERENT origin — a re-derivation, not a verbatim copy.
    source = """\
# %%
# hpc-audit-section: load-data
from other.place import compute_stat
"""
    result = _run(tmp_path, source, _SHADOW_TEMPLATE)
    findings = _shadow_findings(result)
    assert [(f.section, f.evidence["name"], f.evidence["kind"]) for f in findings] == [
        ("load-data", "compute_stat", "import")
    ]


def test_shadow_identical_import_copy_is_clean(tmp_path: Path) -> None:
    # The normal verbatim copy of the template's own import — same origin.
    source = """\
# %%
# hpc-audit-section: load-data
from toy.engine import compute_stat, helper as calc
DATA = "placeholder"

# %%
# hpc-audit-section: fit-model
import toy.core
MODEL = None
"""
    result = _run(tmp_path, source, _SHADOW_TEMPLATE)
    assert _shadow_findings(result) == []


def test_shadow_name_inside_function_body_is_clean(tmp_path: Path) -> None:
    # A binding inside a function body shadows nothing at module scope.
    source = """\
# %%
# hpc-audit-section: report
def wrapper(x):
    compute_stat = x
    return compute_stat
"""
    result = _run(tmp_path, source, _SHADOW_TEMPLATE)
    findings = _shadow_findings(result)
    # The def itself binds "wrapper" (not template-imported) — nothing fires.
    assert findings == []


def test_shadow_attribute_assignment_is_clean(tmp_path: Path) -> None:
    source = """\
# %%
# hpc-audit-section: report
obj = None
"""
    source += "obj.compute_stat = 1\n"
    result = _run(tmp_path, source, _SHADOW_TEMPLATE)
    assert _shadow_findings(result) == []


def test_shadow_unimported_name_is_clean(tmp_path: Path) -> None:
    source = """\
# %%
# hpc-audit-section: report
def unrelated():
    return 1
"""
    result = _run(tmp_path, source, _SHADOW_TEMPLATE)
    assert _shadow_findings(result) == []


def test_shadow_template_preamble_import_attributed_module_preamble(tmp_path: Path) -> None:
    template = """\
import toy.core
from toy.engine import compute_stat

# %%
# hpc-audit-section: report
RESULT = None
"""
    source = """\
# %%
# hpc-audit-section: report
compute_stat = 7
"""
    result = _run(tmp_path, source, template)
    findings = _shadow_findings(result)
    assert len(findings) == 1
    assert findings[0].evidence["template_slug"] == "module-preamble"


def test_shadow_findings_sorted_by_slug_then_name(tmp_path: Path) -> None:
    # Deterministic (slug, name) ordering — the downstream view_sha stability leg.
    source = """\
# %%
# hpc-audit-section: fit-model
compute_stat = 1
calc = 2

# %%
# hpc-audit-section: load-data
toy = 3
"""
    result = _run(tmp_path, source, _SHADOW_TEMPLATE)
    findings = _shadow_findings(result)
    assert [(f.section, f.evidence["name"]) for f in findings] == [
        ("fit-model", "calc"),
        ("fit-model", "compute_stat"),
        ("load-data", "toy"),
    ]


def test_shadow_syntax_error_section_contributes_nothing() -> None:
    # The rule itself is TOLERANT (mirrors audit_view._assertions): a section
    # that does not parse contributes no shadow findings. Exercised at the
    # helper level — the primitive's whole-file parse refuses such a source
    # upstream (SpecInvalid), so tolerance is the rule's own contract.
    from hpc_agent.ops.notebook.lint import _check_template_import_shadowed

    findings = _check_template_import_shadowed(
        [("report", "def compute_stat(:\n")],
        "",
        [("load-data", "from toy.engine import compute_stat\n")],
    )
    assert findings == []


# ── findings never raise / malformed input raises ────────────────────────────


def test_finding_laden_source_returns_envelope_never_raises(tmp_path: Path) -> None:
    # Missing slug + missing path literal at once — a result, not an exception.
    source = """\
# %%
# hpc-audit-section: load-data
DATA = "inputs/missing.csv"
"""
    result = _run(tmp_path, source, _TEMPLATE, input_roots=["inputs"])
    assert isinstance(result, NotebookLintResult)
    assert _rules(result, "structural_completeness")  # fit-model + report missing
    assert _rules(result, "executes_live")  # the missing literal


def test_missing_source_file_raises_spec_invalid(tmp_path: Path) -> None:
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    spec = NotebookLintInput(source="nope.py", template="template.py")
    with pytest.raises(errors.SpecInvalid):
        notebook_lint(experiment_dir=tmp_path, spec=spec)


def test_unparseable_source_raises_spec_invalid(tmp_path: Path) -> None:
    source = """\
# %%
# hpc-audit-section: load-data
def broken(:
"""
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    spec = NotebookLintInput(source="source.py", template="template.py")
    with pytest.raises(errors.SpecInvalid):
        notebook_lint(experiment_dir=tmp_path, spec=spec)
