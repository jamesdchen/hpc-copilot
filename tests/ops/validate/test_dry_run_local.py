"""Tests for ``hpc_agent.ops.validate.dry_run_local`` (#205).

Pattern (mirrors ``test_validate_executor_signatures.py``): write a stub
``.hpc/tasks.py`` exposing ``total()`` / ``resolve(i)`` over a list of
kwarg dicts, call the validator, assert the findings.

Two layers under test:

1. Template-render (default-on): rendering ``result_dir_template`` for the
   sampled ids catches an unfilled placeholder and a cross-id collision.
2. Smoke-exec (opt-in, ``smoke=true``): running the executor locally under
   the dispatcher's ``HPC_KW_*`` env catches an ImportError and passes a
   healthy executor.

Each test exercises one finding ``code`` so a refactor that breaks one
path doesn't silently take out the others.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from hpc_agent._wire.validators.dry_run_local import DryRunLocalSpec
from hpc_agent.ops.validate.dry_run_local import dry_run_local

if TYPE_CHECKING:
    from pathlib import Path

# Smoke-exec tests below issue `python -c '…'` with POSIX single-quoting; the
# dispatcher's `subprocess.run(shell=True)` contract uses /bin/sh on POSIX and
# cmd.exe on Windows — and cmd.exe treats the single quotes as part of the
# argument, so the stderr signature differs and the smoke layer can't
# distinguish import / nonzero / override paths. Same POSIX-shell constraint
# as the test_dispatch #163 skips.
_smoke_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="smoke-exec uses POSIX-shell single-quoted `python -c '…'` (see #163)",
)


def _write_tasks_py(tmp_path: Path, tasks: list[dict]) -> None:
    """Drop a stub tasks.py exposing total()/resolve(i) over *tasks*."""
    target = tmp_path / ".hpc" / "tasks.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"_TASKS = {tasks!r}\ndef total(): return len(_TASKS)\ndef resolve(i): return _TASKS[i]\n"
    )


# ─── Layer 1: template render (default-on) ─────────────────────────────


def test_distinct_templates_emit_no_findings(tmp_path: Path) -> None:
    """A template that renders a distinct, fully-filled dir per id passes."""
    _write_tasks_py(tmp_path, [{"seed": 1}, {"seed": 2}, {"seed": 3}])
    out = dry_run_local(tmp_path, spec=DryRunLocalSpec(result_dir_template="results/seed_{seed}"))
    assert out.findings == []


def test_task_id_and_run_id_are_reserved_keys(tmp_path: Path) -> None:
    """``{task_id}`` / ``{run_id}`` are injected by the dispatcher, so a
    template referencing them must NOT be flagged as unfilled."""
    _write_tasks_py(tmp_path, [{"seed": 1}, {"seed": 2}])
    out = dry_run_local(
        tmp_path,
        spec=DryRunLocalSpec(result_dir_template="runs/{run_id}/task_{task_id}_seed_{seed}"),
    )
    assert out.findings == []


def test_unfilled_placeholder_field_emits_error(tmp_path: Path) -> None:
    """The headline broken-grid bug: the template references a key
    ``resolve(i)`` never supplies — a per-task KeyError on the cluster."""
    _write_tasks_py(tmp_path, [{"seed": 1}])
    out = dry_run_local(
        tmp_path,
        spec=DryRunLocalSpec(result_dir_template="results/{horizon}/seed_{seed}"),
    )
    finding = next(f for f in out.findings if f.code == "template_unfilled_field")
    assert finding.severity == "error"
    assert finding.evidence["task_id"] == 0
    assert finding.evidence["missing_fields"] == ["horizon"]
    assert "horizon" in finding.message
    assert finding.suggested_fix is not None


def test_cross_id_collision_emits_error(tmp_path: Path) -> None:
    """Two distinct ids that render the SAME dir = silent overwrite of the
    first task's metrics.json. The template ignores the differing kwarg."""
    _write_tasks_py(tmp_path, [{"seed": 1}, {"seed": 2}])
    out = dry_run_local(tmp_path, spec=DryRunLocalSpec(result_dir_template="results/fixed_dir"))
    finding = next(f for f in out.findings if f.code == "result_dir_collision")
    assert finding.severity == "error"
    assert finding.evidence["task_ids"] == [0, 1]
    assert finding.evidence["result_dir"] == "results/fixed_dir"


def test_collision_resolved_by_task_id_passes(tmp_path: Path) -> None:
    """Adding ``{task_id}`` to an otherwise-colliding template makes every
    dir distinct — no collision finding."""
    _write_tasks_py(tmp_path, [{"seed": 1}, {"seed": 2}])
    out = dry_run_local(
        tmp_path, spec=DryRunLocalSpec(result_dir_template="results/fixed_{task_id}")
    )
    assert out.findings == []


def test_sample_n_tasks_caps_the_render_loop(tmp_path: Path) -> None:
    """``sample_n_tasks`` bounds the walk: a collision only reachable past
    the sample window is not flagged (pins the cap so it can't drift)."""
    # ids 0..4 render distinct dirs; ids 5..9 collide with id 0's "fixed".
    tasks = [{"seed": i} for i in range(5)] + [{"seed": "fixed"}] * 5
    _write_tasks_py(tmp_path, tasks)
    spec = DryRunLocalSpec(result_dir_template="results/seed_{seed}", sample_n_tasks=3)
    out = dry_run_local(tmp_path, spec=spec)
    assert out.findings == []


def test_missing_tasks_py_emits_warning(tmp_path: Path) -> None:
    """No tasks.py yet → warning (not error); the gate can't render."""
    out = dry_run_local(tmp_path, spec=DryRunLocalSpec(result_dir_template="results/{seed}"))
    finding = next(f for f in out.findings if f.code == "tasks_py_missing")
    assert finding.severity == "warning"


def test_resolve_returning_non_dict_emits_error(tmp_path: Path) -> None:
    """resolve(i) must return a dict (kwargs are **-unpacked + format ctx)."""
    target = tmp_path / ".hpc" / "tasks.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "_T = [('a', 1)]\ndef total(): return len(_T)\ndef resolve(i): return _T[i]\n"
    )
    out = dry_run_local(tmp_path, spec=DryRunLocalSpec(result_dir_template="results/{seed}"))
    finding = next(f for f in out.findings if f.code == "resolve_returned_non_dict")
    assert finding.severity == "error"


# ─── Layer 2: executor smoke-exec (opt-in) ─────────────────────────────


def test_smoke_off_by_default_does_not_run_executor(tmp_path: Path) -> None:
    """Without ``smoke=true`` a broken executor is never run — only the
    template layer fires. Proves the smoke layer is genuinely opt-in."""
    _write_tasks_py(tmp_path, [{"seed": 1}])
    out = dry_run_local(
        tmp_path,
        spec=DryRunLocalSpec(
            result_dir_template="results/seed_{seed}",
            executor="python -c 'import a_module_that_does_not_exist_xyz'",
        ),
    )
    assert all(f.code.startswith("smoke_") is False for f in out.findings)
    assert out.findings == []


@_smoke_posix_only
def test_smoke_catches_import_error(tmp_path: Path) -> None:
    """The motivating case: an executor that fails to import is caught
    LOCALLY, before any SSH, with the stderr tail captured."""
    _write_tasks_py(tmp_path, [{"seed": 1}])
    out = dry_run_local(
        tmp_path,
        spec=DryRunLocalSpec(
            result_dir_template="results/seed_{seed}",
            smoke=True,
            executor=f"{sys.executable} -c 'import a_module_that_does_not_exist_xyz'",
        ),
    )
    finding = next(f for f in out.findings if f.code == "smoke_import_error")
    assert finding.severity == "error"
    assert finding.evidence["task_id"] == 0
    # The captured stderr tail must carry the real traceback verbatim so the
    # cascade can surface it (the whole point — like verify-canary).
    assert "ModuleNotFoundError" in finding.evidence["stderr_tail"]


@_smoke_posix_only
def test_smoke_healthy_executor_passes_and_sees_hpc_kw_env(tmp_path: Path) -> None:
    """A healthy executor exits 0 → no findings. The body also asserts the
    dispatcher's env contract (``HPC_KW_SEED`` + bare ``SEED``) is exported,
    so a green result proves the env wiring, not just a no-op command."""
    _write_tasks_py(tmp_path, [{"seed": 42}])
    probe = "import os; assert os.environ['HPC_KW_SEED']=='42'; assert os.environ['SEED']=='42'"
    out = dry_run_local(
        tmp_path,
        spec=DryRunLocalSpec(
            result_dir_template="results/seed_{seed}",
            smoke=True,
            executor=f"{sys.executable} -c {probe!r}",
        ),
    )
    assert out.findings == []


@_smoke_posix_only
def test_smoke_command_override_runs_instead_of_executor(tmp_path: Path) -> None:
    """``smoke_command`` (a cheap import/--help probe) runs in place of the
    real ``executor`` — the executor here would exit 1, the probe exits 0."""
    _write_tasks_py(tmp_path, [{"seed": 1}])
    out = dry_run_local(
        tmp_path,
        spec=DryRunLocalSpec(
            result_dir_template="results/seed_{seed}",
            smoke=True,
            executor=f"{sys.executable} -c 'raise SystemExit(1)'",
            smoke_command=f"{sys.executable} -c 'pass'",
        ),
    )
    assert out.findings == []


@_smoke_posix_only
def test_smoke_nonzero_exit_emits_error(tmp_path: Path) -> None:
    """A non-import runtime failure surfaces as ``smoke_nonzero_exit`` with
    the exit code recorded."""
    _write_tasks_py(tmp_path, [{"seed": 1}])
    out = dry_run_local(
        tmp_path,
        spec=DryRunLocalSpec(
            result_dir_template="results/seed_{seed}",
            smoke=True,
            executor=f"{sys.executable} -c 'raise SystemExit(7)'",
        ),
    )
    finding = next(f for f in out.findings if f.code == "smoke_nonzero_exit")
    assert finding.severity == "error"
    assert finding.evidence["returncode"] == 7


def test_smoke_without_executor_emits_misconfig(tmp_path: Path) -> None:
    """``smoke=true`` with no ``executor`` is a caller misconfig caught
    before any spawn."""
    _write_tasks_py(tmp_path, [{"seed": 1}])
    out = dry_run_local(
        tmp_path,
        spec=DryRunLocalSpec(result_dir_template="results/seed_{seed}", smoke=True),
    )
    finding = next(f for f in out.findings if f.code == "smoke_executor_missing")
    assert finding.severity == "error"


def test_smoke_refuses_dispatcher_self_recursion(tmp_path: Path) -> None:
    """An executor that IS the dispatcher would self-recurse (#162); the
    gate refuses it before spawning anything."""
    _write_tasks_py(tmp_path, [{"seed": 1}])
    out = dry_run_local(
        tmp_path,
        spec=DryRunLocalSpec(
            result_dir_template="results/seed_{seed}",
            smoke=True,
            executor="python3 .hpc/_hpc_dispatch.py",
        ),
    )
    finding = next(f for f in out.findings if f.code == "smoke_executor_is_dispatcher")
    assert finding.severity == "error"


# ─── envelope shape ────────────────────────────────────────────────────


def test_envelope_shape_is_findings_list(tmp_path: Path) -> None:
    """The result is a DryRunLocalResult whose ``findings`` is a list of
    ValidatorFinding with the standard fields populated."""
    from hpc_agent._wire.validators.dry_run_local import DryRunLocalResult
    from hpc_agent._wire.workflows.validate_campaign import ValidatorFinding

    _write_tasks_py(tmp_path, [{"seed": 1}, {"seed": 2}])
    out = dry_run_local(tmp_path, spec=DryRunLocalSpec(result_dir_template="results/fixed"))
    assert isinstance(out, DryRunLocalResult)
    assert isinstance(out.findings, list)
    assert all(isinstance(f, ValidatorFinding) for f in out.findings)
    f = out.findings[0]
    assert f.validator == "dry-run-local"
    assert f.code and f.message and f.severity in {"error", "warning", "info"}
