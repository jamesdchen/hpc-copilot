"""Tests for the ``scaffold-strategy`` primitive (Surface 4).

``scaffold-strategy`` is the sibling of ``build-executor``: it copies a
correctly-wired campaign strategy template into the experiment repo as
``.hpc/tasks.py`` so an agent never has to ``Read`` the framework's
``optuna_strategy.py`` to learn the ask/tell contract. These tests pin:

* **byte-faithful** materialization (the destination is identical to the
  shipped template — no transformation),
* the materialized strategy is **importable** and exposes the
  ``resolve`` / ``total`` / ``_propose`` surface the campaign loop calls,
* an unknown ``--name`` is refused with ``SpecInvalid`` (no write),
* re-running is idempotent (refuse-without-force, byte-faithful with
  ``--force``).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from hpc_agent import _PACKAGE_ROOT, errors
from hpc_agent.incorporation.scaffold_strategy import scaffold_strategy

_SCAFFOLDS = Path(_PACKAGE_ROOT) / "execution" / "mapreduce" / "templates" / "scaffolds"
_DEST_REL = Path(".hpc") / "tasks.py"


def _template_text(name: str) -> str:
    return (_SCAFFOLDS / f"{name}_strategy.py").read_text(encoding="utf-8")


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"_scaffolded_{path.parent.parent.name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["optuna", "pbt"])
def test_materializes_byte_faithful_strategy(name: str, tmp_path: Path) -> None:
    """The destination .hpc/tasks.py is byte-identical to the shipped template."""
    result = scaffold_strategy(output_dir=tmp_path, name=name)

    dest = tmp_path / _DEST_REL
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == _template_text(name)

    assert result["name"] == name
    assert Path(result["path"]) == dest.resolve()
    assert Path(result["source"]) == _SCAFFOLDS / f"{name}_strategy.py"
    assert Path(result["output_dir"]) == tmp_path.resolve()


@pytest.mark.parametrize("name", ["optuna", "pbt"])
def test_materialized_strategy_is_importable_with_expected_surface(
    name: str, tmp_path: Path
) -> None:
    """The scaffolded strategy imports cleanly (no optuna at module scope)
    and exposes the campaign contract surface: resolve / total / FLAGS.

    Importing + calling ``total()`` must work even without optuna installed
    (the ask path is lazily imported inside ``_propose`` and only fires on
    the orchestrator). We do NOT call ``resolve()`` on optuna here: with no
    completed iterations its proposal path goes through ``_propose`` →
    ``import optuna``, which is the orchestrator-only path — see the
    optimizer-free ``test_resolve_carries_reserved_trial_token`` (pbt) for
    a resolve() invocation."""
    scaffold_strategy(output_dir=tmp_path, name=name)
    # No HPC_CAMPAIGN_ID → empty history → no ask on import (load-idempotent).
    os.environ.pop("HPC_CAMPAIGN_ID", None)
    module = _load(tmp_path / _DEST_REL)

    assert callable(module.resolve)
    assert callable(module.total)
    assert isinstance(module.FLAGS, dict) and module.FLAGS
    assert module.total() >= 0


def test_optuna_strategy_exposes_propose(tmp_path: Path) -> None:
    """The optuna template carries the orchestrator-only _propose helper —
    the load-bearing ask/tell entry point the contract names."""
    scaffold_strategy(output_dir=tmp_path, name="optuna")
    os.environ.pop("HPC_CAMPAIGN_ID", None)
    module = _load(tmp_path / _DEST_REL)
    assert callable(module._propose)


def test_resolve_carries_reserved_trial_token(tmp_path: Path) -> None:
    """resolve()'s dict carries the reserved trial_token bookkeeping key the
    framework strips from cmd_sha but still exports (invariant 2)."""
    scaffold_strategy(output_dir=tmp_path, name="pbt")
    os.environ.pop("HPC_CAMPAIGN_ID", None)
    module = _load(tmp_path / _DEST_REL)
    assert "trial_token" in module.resolve(0)


def test_refuses_unknown_name(tmp_path: Path) -> None:
    """An unknown --name is SpecInvalid and writes nothing."""
    with pytest.raises(errors.SpecInvalid):
        scaffold_strategy(output_dir=tmp_path, name="hyperband")
    assert not (tmp_path / _DEST_REL).exists()


def test_refuses_missing_output_dir(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        scaffold_strategy(output_dir=tmp_path / "nope", name="optuna")


def test_idempotent_refuse_then_force(tmp_path: Path) -> None:
    """Re-run without --force refuses to clobber; with --force it is a
    byte-faithful re-materialization (idempotency_key=output_dir)."""
    scaffold_strategy(output_dir=tmp_path, name="optuna")
    dest = tmp_path / _DEST_REL

    # A customized strategy must not be silently overwritten.
    dest.write_text("# my customized search space\n", encoding="utf-8")
    with pytest.raises(errors.SpecInvalid):
        scaffold_strategy(output_dir=tmp_path, name="optuna")
    assert dest.read_text(encoding="utf-8") == "# my customized search space\n"

    # --force re-materializes byte-faithfully from the single template source.
    result = scaffold_strategy(output_dir=tmp_path, name="optuna", force=True)
    assert dest.read_text(encoding="utf-8") == _template_text("optuna")
    assert result["name"] == "optuna"


def test_registered_as_agent_facing_scaffold() -> None:
    """The primitive auto-registers via the package walk as an agent-facing
    scaffold verb (the contract test_scaffolds_are_agent_facing enforces the
    flag; this pins the registration is reachable)."""
    from hpc_agent._kernel.registry.primitive import get_meta, register_primitives

    register_primitives()
    meta = get_meta("scaffold-strategy")
    assert meta.verb == "scaffold"
    assert meta.agent_facing is True
    assert meta.idempotency_key == "output_dir"
