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
) -> NotebookLintResult:
    (experiment_dir / "source.py").write_text(source, encoding="utf-8")
    (experiment_dir / "template.py").write_text(template, encoding="utf-8")
    spec = NotebookLintInput(
        source="source.py",
        template="template.py",
        input_roots=input_roots or [],
        source_roots=source_roots or [],
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
