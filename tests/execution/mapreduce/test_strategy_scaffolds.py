"""Tests for the shipped campaign-strategy scaffolds.

These files are TEMPLATES copied into a user's ``.hpc/tasks.py`` — they are
never imported by the package itself. The load-bearing invariant is
**cluster-safety**: the cluster-side dispatcher imports the materialized
tasks.py and calls ``resolve(task_id)`` on the compute node, so no external
optimizer may be imported at module scope (the compute node has no optuna and
must not re-``ask``). We assert that structurally (AST) and by loading each
scaffold with no optimizer available.
"""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path

import pytest

from hpc_agent import _PACKAGE_ROOT

_SCAFFOLDS = Path(_PACKAGE_ROOT) / "execution" / "mapreduce" / "templates" / "scaffolds"
_STRATEGY_FILES = ("optuna_strategy.py", "pbt_strategy.py")


def _top_level_imports(path: Path) -> set[str]:
    """Root module names imported at MODULE scope (not inside functions)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:  # module scope only — function-body imports excluded
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"_scaffold_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", _STRATEGY_FILES)
def test_scaffold_parses_and_defines_contract(name: str) -> None:
    module = _load(_SCAFFOLDS / name)
    assert callable(module.total)
    assert callable(module.resolve)
    assert isinstance(module.FLAGS, dict) and module.FLAGS


@pytest.mark.parametrize("name", _STRATEGY_FILES)
def test_scaffold_does_not_import_optimizer_at_module_scope(name: str) -> None:
    """Cluster-safety: the compute node imports the scaffold and runs resolve()
    — it must not pull in optuna (absent there) at module scope."""
    assert "optuna" not in _top_level_imports(_SCAFFOLDS / name)


def test_scaffolds_load_without_optuna_installed() -> None:
    """Importing either scaffold must succeed even when optuna is unavailable
    (the orchestrator-only ask path imports it lazily inside a function)."""
    for name in _STRATEGY_FILES:
        # No HPC_CAMPAIGN_ID → history is empty → no proposal/ask on import.
        os.environ.pop("HPC_CAMPAIGN_ID", None)
        module = _load(_SCAFFOLDS / name)
        assert module.total() >= 0


def test_pbt_generation_zero_resolve_is_deterministic_and_fresh() -> None:
    """With no completed generations, PBT seeds fresh members deterministically
    (no checkpoint clone, distinct lrs across the population)."""
    os.environ.pop("HPC_CAMPAIGN_ID", None)
    pbt = _load(_SCAFFOLDS / "pbt_strategy.py")
    assert pbt.total() == pbt._POP
    first = [pbt.resolve(i) for i in range(pbt._POP)]
    again = [pbt.resolve(i) for i in range(pbt._POP)]
    assert first == again  # pure function of (empty) prior state
    assert all(t["init_ckpt"] == "" for t in first)  # gen 0 → fresh init
    assert first[0]["trial_token"] == [0, 0]
    # lrs are spread across the configured range, low → high.
    lrs = [t["lr"] for t in first]
    assert lrs == sorted(lrs)
    assert lrs[0] >= pbt._LR_LO
    assert lrs[-1] <= pbt._LR_HI


def test_pbt_perturb_is_seeded_and_in_range() -> None:
    pbt = _load(_SCAFFOLDS / "pbt_strategy.py")
    a = pbt._perturb(1e-3, generation=2, member=1)
    b = pbt._perturb(1e-3, generation=2, member=1)
    assert a == b  # reproducible on orchestrator and cluster
    assert pbt._LR_LO <= a <= pbt._LR_HI
