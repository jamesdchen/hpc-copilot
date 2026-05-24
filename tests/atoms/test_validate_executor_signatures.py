"""Tests for ``hpc_agent.ops.validate.executor_signatures``.

Pattern: write a fake executor module to ``tmp_path/executor_pkg``
and add it to ``sys.path``; write a ``tasks.py`` to
``tmp_path/.hpc/``; call the validator and assert the findings.

Each test exercises one finding ``code`` so a future refactor that
breaks one path doesn't silently take out the others.
"""

from __future__ import annotations

import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from hpc_agent._wire.validators.validate_executor_signatures import (
    ValidateExecutorSignaturesSpec,
)
from hpc_agent.ops.validate.executor_signatures import (
    validate_executor_signatures,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_executor(tmp_path: Path, body: str, *, modname: str = "test_exec_mod") -> str:
    """Drop a Python module on the import path; return its dotted name."""
    pkg = tmp_path / "exec_pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / f"{modname}.py").write_text(body)
    sys.path.insert(0, str(tmp_path))
    return f"exec_pkg.{modname}"


def _write_tasks_py(tmp_path: Path, tasks: list[dict]) -> None:
    target = tmp_path / ".hpc" / "tasks.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"_TASKS = {tasks!r}\ndef total(): return len(_TASKS)\ndef resolve(i): return _TASKS[i]\n"
    )


@pytest.fixture(autouse=True)
def _cleanup_sys_path():
    """Restore sys.path after each test so module imports don't leak."""
    snapshot = list(sys.path)
    snapshot_modules = set(sys.modules)
    yield
    sys.path[:] = snapshot
    for mod in set(sys.modules) - snapshot_modules:
        sys.modules.pop(mod, None)


def _spec(modname: str, fn: str = "main") -> ValidateExecutorSignaturesSpec:
    return ValidateExecutorSignaturesSpec(executor_module=modname, executor_function=fn)


# ─── happy path ────────────────────────────────────────────────────────


def test_clean_signature_emits_no_findings(tmp_path: Path) -> None:
    modname = _write_executor(
        tmp_path,
        textwrap.dedent("""
            def main(horizon: int, seed: int) -> None:
                pass
        """).strip(),
    )
    _write_tasks_py(tmp_path, [{"horizon": 1, "seed": 42}, {"horizon": 5, "seed": 1337}])
    out = validate_executor_signatures(tmp_path, spec=_spec(modname))
    assert out.findings == []


def test_function_with_var_keywords_accepts_anything(tmp_path: Path) -> None:
    """A signature with ``**kwargs`` swallows unrecognised parameters
    by design — no missing_parameter findings."""
    modname = _write_executor(
        tmp_path,
        textwrap.dedent("""
            def main(seed: int, **kwargs) -> None:
                pass
        """).strip(),
    )
    _write_tasks_py(tmp_path, [{"seed": 1, "extra": "ok"}])
    out = validate_executor_signatures(tmp_path, spec=_spec(modname))
    assert out.findings == []


# ─── the SEGMENT_CHOICES bug class ─────────────────────────────────────


def test_literal_value_not_in_set_emits_finding(tmp_path: Path) -> None:
    """The headline bug class: tasks.py passes a string the executor's
    Literal annotation rejects."""
    modname = _write_executor(
        tmp_path,
        textwrap.dedent("""
            from typing import Literal
            def main(segment: Literal["train", "val", "test"]) -> None:
                pass
        """).strip(),
    )
    _write_tasks_py(tmp_path, [{"segment": "train"}, {"segment": "fabricated"}])
    out = validate_executor_signatures(tmp_path, spec=_spec(modname))
    codes = [f.code for f in out.findings]
    assert "literal_value_not_allowed" in codes
    finding = next(f for f in out.findings if f.code == "literal_value_not_allowed")
    assert finding.severity == "error"
    assert finding.evidence["task_id"] == 1
    assert finding.evidence["param_name"] == "segment"
    assert finding.evidence["value"] == "fabricated"
    assert sorted(finding.evidence["allowed"]) == ["test", "train", "val"]
    assert "fabricated" in finding.message
    assert finding.suggested_fix is not None


# ─── missing parameter ────────────────────────────────────────────────


def test_missing_parameter_emits_finding(tmp_path: Path) -> None:
    modname = _write_executor(
        tmp_path,
        textwrap.dedent("""
            def main(horizon: int) -> None:
                pass
        """).strip(),
    )
    _write_tasks_py(tmp_path, [{"horizon": 1, "extra_param": "no_such"}])
    out = validate_executor_signatures(tmp_path, spec=_spec(modname))
    finding = next(f for f in out.findings if f.code == "missing_parameter")
    assert finding.severity == "error"
    assert finding.evidence["param_name"] == "extra_param"
    assert "horizon" in finding.evidence["available_params"]


# ─── degraded paths ────────────────────────────────────────────────────


def test_missing_executor_module_emits_info_finding(tmp_path: Path) -> None:
    """Cannot import the executor module → info-level finding;
    signature check is skipped, NOT failed."""
    _write_tasks_py(tmp_path, [{"x": 1}])
    out = validate_executor_signatures(
        tmp_path,
        spec=_spec("nonexistent_module_xyz"),
    )
    finding = next(f for f in out.findings if f.code == "executor_module_import_error")
    assert finding.severity == "info"
    assert "nonexistent_module_xyz" in finding.message


def test_missing_function_on_executor_emits_error(tmp_path: Path) -> None:
    modname = _write_executor(tmp_path, "x = 1\n")
    _write_tasks_py(tmp_path, [{"x": 1}])
    out = validate_executor_signatures(tmp_path, spec=_spec(modname, fn="not_a_real_function"))
    finding = next(f for f in out.findings if f.code == "executor_function_not_found")
    assert finding.severity == "error"


def test_missing_tasks_py_emits_warning(tmp_path: Path) -> None:
    modname = _write_executor(tmp_path, "def main(): pass\n")
    out = validate_executor_signatures(tmp_path, spec=_spec(modname))
    finding = next(f for f in out.findings if f.code == "tasks_py_missing")
    assert finding.severity == "warning"


def test_resolve_returning_non_dict_emits_error(tmp_path: Path) -> None:
    """tasks.resolve(i) MUST return a dict (the framework **-unpacks
    it). A list/tuple/scalar surfaces immediately."""
    target = tmp_path / ".hpc" / "tasks.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "_TASKS = [('a', 1)]\ndef total(): return len(_TASKS)\ndef resolve(i): return _TASKS[i]\n"
    )
    modname = _write_executor(tmp_path, "def main(): pass\n")
    out = validate_executor_signatures(tmp_path, spec=_spec(modname))
    finding = next(f for f in out.findings if f.code == "resolve_returned_non_dict")
    assert finding.severity == "error"


def test_sample_n_tasks_caps_iteration(tmp_path: Path) -> None:
    """A 10-task campaign with ``sample_n_tasks=2`` only walks tasks
    0-1; a bug at task 5 is not flagged. Pinning so the cap doesn't
    silently change."""
    modname = _write_executor(
        tmp_path,
        textwrap.dedent("""
            from typing import Literal
            def main(s: Literal["a", "b"]) -> None:
                pass
        """).strip(),
    )
    tasks = [{"s": "a"}] * 5 + [{"s": "wrong"}] * 5
    _write_tasks_py(tmp_path, tasks)

    spec = ValidateExecutorSignaturesSpec(
        executor_module=modname, executor_function="main", sample_n_tasks=2
    )
    out = validate_executor_signatures(tmp_path, spec=spec)
    # Only tasks 0-1 walked; the bad value at task 5 is NOT flagged.
    assert all(f.code != "literal_value_not_allowed" for f in out.findings)
