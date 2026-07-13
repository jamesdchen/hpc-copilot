"""Unit tests for ``_run_scoped_results_subdir`` (run-#12 finding 19 leg B).

The per-task pulls must scope to the run's OWN results subtree — the static
prefix of its ``result_dir_template`` — because the scp fallback cannot
include-filter and pulling the whole ``results/`` root drags every prior
run's outputs through the transfer (the live 1800s timeout).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hpc_agent.ops.aggregate_flow import _run_scoped_results_subdir

_EXP = Path("unused")  # the record carries the template; no sidecar read needed


def _rec(template: str | None) -> SimpleNamespace:
    return SimpleNamespace(result_dir_template=template)


def test_sweep_template_scopes_to_static_prefix() -> None:
    scoped = _run_scoped_results_subdir(
        _EXP,
        "run-1",
        _rec("results/causal_tune_linear/{estimator}/{exog_bucket}/chunk_{chunk_start}"),
        "results",
    )
    assert scoped == "results/causal_tune_linear"


def test_placeholder_mid_component_trims_to_last_full_dir() -> None:
    scoped = _run_scoped_results_subdir(_EXP, "run-1", _rec("results/causal_{x}/y"), "results")
    assert scoped == "results"


def test_fixed_template_scopes_to_itself() -> None:
    scoped = _run_scoped_results_subdir(_EXP, "run-1", _rec("results/fixed_dir/"), "results")
    assert scoped == "results/fixed_dir"


def test_absent_template_falls_back_to_root() -> None:
    assert _run_scoped_results_subdir(_EXP, "run-1", _rec(None), "results") == "results"


def test_template_escaping_the_root_falls_back() -> None:
    # A template outside the configured root must never widen/redirect the pull.
    scoped = _run_scoped_results_subdir(_EXP, "run-1", _rec("elsewhere/{task_id}"), "results")
    assert scoped == "results"


def test_template_with_no_directory_falls_back() -> None:
    scoped = _run_scoped_results_subdir(_EXP, "run-1", _rec("{task_id}"), "results")
    assert scoped == "results"
