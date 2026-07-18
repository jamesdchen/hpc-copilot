"""Rule 5 — executor↔notebook module-sha drift (notebook-audit interactivity, slice 2).

Fires when a module imported by BOTH the interview.json executor and the audited
source resolves to DIFFERENT ``module_shas``; stays silent on same-sha, no
executor, and non-shared modules. Advisory (rides ``findings``), never a refusal,
never section-attributed.
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent._wire.actions.notebook_lint import NotebookLintInput, NotebookLintResult
from hpc_agent.ops.notebook.lint import notebook_lint

_TEMPLATE = "# %%\n# hpc-audit-section: model\nX = 0\n"
_DRIFT_RULE = "executor_module_drift"


def _setup(
    tmp_path: Path,
    *,
    source_imports: str,
    executor_imports: str | None,
    shadow: bool,
    materialized: bool = True,
) -> None:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "engine.py").write_text("VERSION = 'fresh'\n", encoding="utf-8")
    (tmp_path / "src" / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    (tmp_path / "source.py").write_text(
        f"# %%\n# hpc-audit-section: model\n{source_imports}\n", encoding="utf-8"
    )
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    if executor_imports is not None:
        (tmp_path / "exec").mkdir(exist_ok=True)
        if shadow:
            # A STALE local copy beside the executor shadows the shared src one.
            (tmp_path / "exec" / "engine.py").write_text("VERSION = 'stale'\n", encoding="utf-8")
        (tmp_path / "exec" / "run.py").write_text(f"{executor_imports}\n", encoding="utf-8")
        if materialized:
            entry = {"kind": "shell_command", "wrapper_path": "exec/run.py"}
            doc = {"_materialized": {"entry_point": entry}}
            (tmp_path / "interview.json").write_text(json.dumps(doc), encoding="utf-8")


def _run(tmp_path: Path) -> NotebookLintResult:
    return notebook_lint(
        experiment_dir=tmp_path,
        spec=NotebookLintInput(source="source.py", template="template.py", source_roots=["src"]),
    )


def _drift(result: NotebookLintResult) -> list:
    return [f for f in result.findings if f.rule == _DRIFT_RULE]


def test_drift_fires_on_same_module_different_sha(tmp_path: Path) -> None:
    # Executor resolves `engine` to its LOCAL stale copy; source to the fresh src copy.
    _setup(tmp_path, source_imports="import engine", executor_imports="import engine", shadow=True)
    findings = _drift(_run(tmp_path))
    assert len(findings) == 1
    f = findings[0]
    assert f.section is None  # module-scoped — never flips a tier
    assert f.evidence["module"] == "engine"
    assert f.evidence["executor"] == "exec/run.py"
    assert f.evidence["executor_module_sha"] != f.evidence["source_module_sha"]


def test_drift_silent_on_same_sha(tmp_path: Path) -> None:
    # No local shadow → both resolve to src/engine.py → identical sha → silent.
    _setup(tmp_path, source_imports="import engine", executor_imports="import engine", shadow=False)
    assert _drift(_run(tmp_path)) == []


def test_drift_silent_when_no_executor(tmp_path: Path) -> None:
    # No interview.json entry point → executor undetectable → fail-open (nothing).
    _setup(
        tmp_path,
        source_imports="import engine",
        executor_imports="import engine",
        shadow=True,
        materialized=False,
    )
    assert _drift(_run(tmp_path)) == []


def test_drift_silent_on_non_shared_module(tmp_path: Path) -> None:
    # Executor imports a DIFFERENT module than the source → no shared module → silent.
    _setup(tmp_path, source_imports="import engine", executor_imports="import other", shadow=True)
    assert _drift(_run(tmp_path)) == []


def test_drift_never_raises_and_is_reported(tmp_path: Path) -> None:
    # The advisory rides the findings channel; a lint with a drift finding still
    # returns a normal result (never an exception, never a refusal).
    _setup(tmp_path, source_imports="import engine", executor_imports="import engine", shadow=True)
    result = _run(tmp_path)
    assert isinstance(result, NotebookLintResult)
    assert any(f.rule == _DRIFT_RULE for f in result.findings)
