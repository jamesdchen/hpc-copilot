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
import sys
import types
from pathlib import Path
from typing import Any

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


# ─── async-refill variant selection (#362, plan §1.5) ───────────────────────


def test_async_refill_emits_async_optuna_variant(tmp_path: Path) -> None:
    """``--async-refill`` materializes the async optuna template byte-faithfully."""
    result = scaffold_strategy(output_dir=tmp_path, name="optuna", async_refill=True)
    dest = tmp_path / _DEST_REL
    assert dest.read_text(encoding="utf-8") == _template_text("optuna_async")
    assert result["async_refill"] is True
    assert result["name"] == "optuna"
    assert Path(result["source"]).name == "optuna_async_strategy.py"


def test_default_optuna_is_synchronous(tmp_path: Path) -> None:
    """Without the flag the default optuna (synchronous) template is emitted —
    the default path is byte-identical to before this feature."""
    result = scaffold_strategy(output_dir=tmp_path, name="optuna")
    dest = tmp_path / _DEST_REL
    assert dest.read_text(encoding="utf-8") == _template_text("optuna")
    assert result["async_refill"] is False


def test_async_refill_is_noop_for_pbt(tmp_path: Path) -> None:
    """pbt has no separate async asset (it already batches) → --async-refill
    falls back to the synchronous pbt template, never errors."""
    result = scaffold_strategy(output_dir=tmp_path, name="pbt", async_refill=True)
    dest = tmp_path / _DEST_REL
    assert dest.read_text(encoding="utf-8") == _template_text("pbt")
    assert Path(result["source"]).name == "pbt_strategy.py"


# ─── async _propose behavior (#362, plan §1.5) ──────────────────────────────


def _make_fake_optuna() -> types.ModuleType:
    """A minimal in-memory optuna stand-in (optuna isn't a test dependency).

    Records sampler kwargs and persists studies by name so successive
    ``create_study(load_if_exists=True)`` calls return the SAME study — the way
    the real sqlite store persists trial state across ``_propose`` re-imports.
    """
    studies: dict[str, Any] = {}
    sampler_calls: list[dict[str, Any]] = []

    class TrialState:
        RUNNING = "running"
        COMPLETE = "complete"

    class Trial:
        def __init__(self, number: int) -> None:
            self.number = number
            self.state = TrialState.RUNNING

        def suggest_float(self, name: str, low: float, high: float, log: bool = False) -> float:
            # Deterministic + distinct per trial number so proposals differ.
            return low * (self.number + 1)

    class Study:
        def __init__(self) -> None:
            self.trials: list[Trial] = []

        def ask(self) -> Trial:
            t = Trial(len(self.trials))
            self.trials.append(t)
            return t

        def tell(self, trial: Trial, value: float) -> None:
            trial.state = TrialState.COMPLETE

    class TPESampler:
        def __init__(self, **kwargs: Any) -> None:
            sampler_calls.append(kwargs)

    def create_study(
        *,
        storage: str,
        study_name: str,
        direction: str,
        sampler: Any = None,
        load_if_exists: bool = False,
    ) -> Any:
        if load_if_exists and study_name in studies:
            return studies[study_name]
        s = Study()
        studies[study_name] = s
        return s

    mod = types.ModuleType("optuna")
    mod.create_study = create_study  # type: ignore[attr-defined]
    mod.trial = types.SimpleNamespace(TrialState=TrialState)  # type: ignore[attr-defined]
    mod.samplers = types.SimpleNamespace(TPESampler=TPESampler)  # type: ignore[attr-defined]
    mod._studies = studies  # type: ignore[attr-defined]
    mod._sampler_calls = sampler_calls  # type: ignore[attr-defined]
    return mod


def _load_async_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Materialize + load the async optuna template, wired to a fake optuna and
    tmp dirs, with ``_history`` controllable via the returned ``history`` list."""
    scaffold_strategy(output_dir=tmp_path, name="optuna", async_refill=True)
    module = _load(tmp_path / _DEST_REL)
    fake = _make_fake_optuna()
    monkeypatch.setitem(sys.modules, "optuna", fake)
    monkeypatch.setattr(module, "_CID", "test")
    monkeypatch.setattr(module, "_CAMPAIGN_DIR", tmp_path)
    monkeypatch.setattr(module, "_PROPOSALS_DIR", tmp_path / "proposals")
    return module, fake


def test_async_propose_asks_distinct_trials_and_uses_constant_liar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successive submit indices ask distinct trials; the sampler is constant_liar."""
    module, fake = _load_async_module(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "_history", list)  # always empty history

    p0 = module._propose(0)
    p1 = module._propose(1)

    assert p0["trial_token"] == 0
    assert p1["trial_token"] == 1  # the ask advanced — distinct proposals
    assert (tmp_path / "proposals" / "iter_00000.json").exists()
    assert (tmp_path / "proposals" / "iter_00001.json").exists()
    # The decorrelating sampler is wired on (the correctness half of refill).
    assert fake._sampler_calls
    assert fake._sampler_calls[0].get("constant_liar") is True


def test_async_propose_is_idempotent_on_proposal_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-proposing the SAME index (a re-import within one submit) reuses the
    persisted proposal — no phantom trial is minted."""
    module, fake = _load_async_module(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "_history", list)

    first = module._propose(0)
    second = module._propose(0)
    assert first == second
    assert len(fake._studies["test"].trials) == 1  # exactly one ask happened


def test_async_propose_tells_finished_trials_out_of_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trial that finishes out of order is told BY ITS trial_token, not by
    record position — the load-bearing async-tell correctness."""
    module, fake = _load_async_module(tmp_path, monkeypatch)
    history: list[dict[str, Any]] = []
    monkeypatch.setattr(module, "_history", lambda: history)

    # Three iterations submitted (trials 0,1,2 asked), none finished yet.
    module._propose(0)
    module._propose(1)
    module._propose(2)
    study = fake._studies["test"]
    assert [t.number for t in study.trials] == [0, 1, 2]

    # Trial 2 finishes FIRST (out of order); 0 and 1 are still in flight.
    history.extend(
        [
            {"complete": False, "metrics": {}, "trial_tokens": [0]},
            {"complete": False, "metrics": {}, "trial_tokens": [1]},
            {"complete": True, "metrics": {"val_loss": 0.4}, "trial_tokens": [2]},
        ]
    )

    # The next submit tells trial 2 (by token), leaves 0/1 RUNNING, asks trial 3.
    module._propose(3)
    assert study.trials[2].state == "complete"  # told by token, not by position
    assert study.trials[0].state == "running"
    assert study.trials[1].state == "running"
    assert [t.number for t in study.trials] == [0, 1, 2, 3]


# ─── grid shape (--shape grid): fixed non-adaptive sweep skeleton ────────────

_TASKS_TEXT = (_SCAFFOLDS / "grid_strategy.py").read_text(encoding="utf-8")


def test_grid_materializes_tasks_and_config_stubs(tmp_path: Path) -> None:
    """--shape grid writes .hpc/tasks.py (byte-faithful skeleton) + N config stubs."""
    result = scaffold_strategy(output_dir=tmp_path, shape="grid", arms=3)

    dest = tmp_path / _DEST_REL
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == _TASKS_TEXT

    config_dir = tmp_path / "configs"
    stubs = sorted(p.name for p in config_dir.glob("*.yaml"))
    assert stubs == ["arm_00.yaml", "arm_01.yaml", "arm_02.yaml"]

    assert result["shape"] == "grid"
    assert result["arms"] == 3
    assert result["name"] is None
    assert result["async_refill"] is False
    assert len(result["config_paths"]) == 3
    assert Path(result["path"]) == dest.resolve()
    assert Path(result["source"]).name == "grid_strategy.py"


def test_grid_config_stub_marks_the_knob_as_a_hole_not_filled(tmp_path: Path) -> None:
    """The varied knob is a MARKED HOLE — structure only, never a nominated key."""
    scaffold_strategy(output_dir=tmp_path, shape="grid", arms=2)
    stub = (tmp_path / "configs" / "arm_00.yaml").read_text(encoding="utf-8")
    assert "HOLE:" in stub
    # The stem is the arm id; no domain-specific knob is invented for the caller.
    assert "arm_00" in stub


def test_grid_tasks_importable_with_total_and_resolve(tmp_path: Path) -> None:
    """The grid skeleton imports (no yaml needed) and total()/resolve() work:
    total() == arm count; resolve(i) returns the arm id keyed as ``arm``."""
    scaffold_strategy(output_dir=tmp_path, shape="grid", arms=2)
    module = _load(tmp_path / _DEST_REL)
    assert callable(module.total)
    assert callable(module.resolve)
    assert isinstance(module.FLAGS, dict) and module.FLAGS
    assert module.total() == 2
    assert module.resolve(0) == {"arm": "arm_00"}
    assert module.resolve(1) == {"arm": "arm_01"}


def test_grid_arm_stems_zero_padded_for_stable_sort(tmp_path: Path) -> None:
    """Ten+ arms are zero-padded so a lexical sort stays a numeric sort
    (arm_09 < arm_10) — task_id ↔ arm never drifts."""
    scaffold_strategy(output_dir=tmp_path, shape="grid", arms=11)
    module = _load(tmp_path / _DEST_REL)
    assert module.total() == 11
    # arm 9 then 10 in numeric order (would be 1,10,11,...,9 without padding).
    assert module.resolve(9) == {"arm": "arm_09"}
    assert module.resolve(10) == {"arm": "arm_10"}


def test_grid_refuses_fewer_than_two_arms(tmp_path: Path) -> None:
    """A one-arm 'grid' is just a single run — refused with SpecInvalid, no write."""
    with pytest.raises(errors.SpecInvalid):
        scaffold_strategy(output_dir=tmp_path, shape="grid", arms=1)
    assert not (tmp_path / _DEST_REL).exists()
    assert not (tmp_path / "configs").exists()


def test_grid_refuses_overwrite_without_force_all_or_nothing(tmp_path: Path) -> None:
    """A collision on ANY destination refuses the whole materialization (no
    partial write); --force re-materializes byte-faithfully."""
    scaffold_strategy(output_dir=tmp_path, shape="grid", arms=2)
    dest = tmp_path / _DEST_REL
    dest.write_text("# customized grid\n", encoding="utf-8")

    with pytest.raises(errors.SpecInvalid):
        scaffold_strategy(output_dir=tmp_path, shape="grid", arms=2)
    assert dest.read_text(encoding="utf-8") == "# customized grid\n"

    result = scaffold_strategy(output_dir=tmp_path, shape="grid", arms=2, force=True)
    assert dest.read_text(encoding="utf-8") == _TASKS_TEXT
    assert result["shape"] == "grid"


def test_unknown_shape_refused(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        scaffold_strategy(output_dir=tmp_path, shape="lattice")


def test_strategy_shape_still_defaults_and_requires_name(tmp_path: Path) -> None:
    """The default shape is 'strategy'; it still requires --name (byte-faithful
    with the pre-feature behaviour) and the return now carries shape='strategy'."""
    result = scaffold_strategy(output_dir=tmp_path, name="optuna")
    assert result["shape"] == "strategy"
    assert (tmp_path / _DEST_REL).read_text(encoding="utf-8") == _template_text("optuna")

    # --shape strategy with no --name (dir exists, so it reaches the name check).
    with pytest.raises(errors.SpecInvalid):
        scaffold_strategy(output_dir=tmp_path, shape="strategy")
