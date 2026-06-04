"""Tests for the ``interview`` primitive and its CLI adapter.

Spec scope:

- The primitive validates an agent-written tasks.py against a structured
  intent and persists interview.json + (conditionally) meta.json.
- It is *experiment-agnostic at the schema level*: the schema does not
  enumerate search-space shapes (logspace / grid / seeds_x / …) and any
  dict-shaped tasks.py — hyperparameter sweeps, eval matrices, RL
  rollouts, benchmark sweeps — round-trips equally. The dict requirement
  is inherited from hpc-agent's pre-existing tasks.py contract
  (compute_cmd_sha enforces it because kwargs get **-unpacked into the
  user's task function); the interview adds no further structure.
- It is the spine for future cmd_recall queries (intent + provenance +
  cmd_sha persistence).
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.interview import InterviewSpec
from hpc_agent.ops.memory.interview import record_interview

if TYPE_CHECKING:
    from pathlib import Path


# ─── helpers ───────────────────────────────────────────────────────────────


_HPARAM_TASKS_PY = """\
_TASKS = [{"lr": 1e-4}, {"lr": 1e-3}, {"lr": 1e-2}]
def total(): return len(_TASKS)
def resolve(i): return _TASKS[i]
"""

_EVAL_TASKS_PY = """\
_TASKS = [
    {"model": "opus-4.7", "dataset": "mmlu-pro"},
    {"model": "sonnet-4.6", "dataset": "mmlu-pro"},
    {"model": "haiku-4.5", "dataset": "mmlu-pro"},
]
def total(): return len(_TASKS)
def resolve(i): return _TASKS[i]
"""

_RL_TASKS_PY = """\
_ENVS = ["cartpole", "lunarlander"]
_SEEDS = [0, 1, 2, 3, 4]
_TASKS = [{"env": e, "seed": s} for e in _ENVS for s in _SEEDS]
def total(): return len(_TASKS)
def resolve(i): return _TASKS[i]
"""


def _write_tasks(campaign_dir, src):
    """Write the canonical .hpc/tasks.py (creating .hpc/); return the path."""
    tasks = campaign_dir / ".hpc" / "tasks.py"
    tasks.parent.mkdir(parents=True, exist_ok=True)
    tasks.write_text(src)
    return tasks


def _minimal_intent(task_count: int, **overrides) -> dict:
    intent = {
        "goal": "spike test",
        "task_count": task_count,
        "produced_by": {"kind": "human", "operator": "test"},
    }
    intent.update(overrides)
    return intent


# ─── round-trip across experiment families (the agnosticism guard) ────────


@pytest.mark.parametrize(
    "tasks_src,expected_count",
    [
        (_HPARAM_TASKS_PY, 3),
        (_EVAL_TASKS_PY, 3),
        (_RL_TASKS_PY, 10),
    ],
    ids=["ml-hparam", "llm-eval", "rl-rollout"],
)
def test_round_trip_across_experiment_families(
    tmp_path: Path,
    tasks_src: str,
    expected_count: int,
) -> None:
    """The interview primitive accepts any dict-shaped tasks.py — no
    enumeration over hyperparameter-sweep / eval-grid / RL-rollout."""
    _write_tasks(tmp_path, tasks_src)
    intent = _minimal_intent(expected_count, task_kind="example-family")

    data = record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)

    assert data["total_tasks"] == expected_count
    # hpc-agent's tasks.py contract requires resolve(i) to return a dict
    # (kwargs get **-unpacked into the user's task function and must be
    # JSON-serializable for cmd_sha). The interview inherits that constraint;
    # it does NOT add further structure (no `lr` field, no `n` field, etc.).
    assert isinstance(data["preview"]["first"], dict)
    assert (tmp_path / "interview.json").is_file()


def test_non_dict_tasks_py_fails_with_existing_contract_error(tmp_path: Path) -> None:
    """Sentinel: if tasks.py returns a non-dict (forbidden by hpc-agent's
    pre-existing contract), the failure happens at compute_cmd_sha — surfaced
    as a TypeError. Locking this so that loosening the dict requirement later
    is a deliberate, multi-place change rather than an accident."""
    _write_tasks(
        tmp_path,
        "_TASKS = [('a', 1), ('b', 2)]\n"
        "def total(): return len(_TASKS)\n"
        "def resolve(i): return _TASKS[i]\n",
    )
    with pytest.raises(TypeError, match="must return a dict"):
        record_interview(InterviewSpec.model_validate(_minimal_intent(2)), campaign_dir=tmp_path)


# ─── persistence shape ────────────────────────────────────────────────────


def test_interview_json_round_trips_intent_verbatim(tmp_path: Path) -> None:
    """Intent fields are persisted as-is (modulo the _materialized block)."""
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    intent = _minimal_intent(
        3,
        task_kind="ml-hparam-sweep",
        budget={"gpu_hours": 200, "wall_clock_max_h": 12},
        abort_if={"metric": "val_loss", "above": 5.0, "after_tasks": 2},
        notes="LR range chosen from prior 'narrow sweep' findings",
    )

    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)

    persisted = json.loads((tmp_path / "interview.json").read_text())
    for key in ("goal", "task_count", "task_kind", "budget", "abort_if", "notes", "produced_by"):
        assert persisted[key] == intent[key], f"intent.{key} did not round-trip"
    assert "_materialized" in persisted
    assert persisted["_materialized"]["total_tasks"] == 3
    assert "cmd_sha" in persisted["_materialized"]


def test_meta_json_only_written_when_intent_supplies_relevant_fields(tmp_path: Path) -> None:
    """No cluster_target and no budget → no meta.json update.

    (``.claude/settings.json`` IS always written for the bare-worker allow rule
    — #190 — so assert meta.json's absence specifically rather than pin the
    whole artifacts list.)
    """
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    data = record_interview(InterviewSpec.model_validate(_minimal_intent(3)), campaign_dir=tmp_path)
    assert "meta.json" not in data["artifacts"]
    assert "interview.json" in data["artifacts"]
    assert not (tmp_path / "meta.json").exists()


def test_meta_json_merge_preserves_existing_keys(tmp_path: Path) -> None:
    """Pre-existing meta.json keys win on conflict; total_tasks is overridden."""
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    (tmp_path / "meta.json").write_text(
        json.dumps(
            {
                "experiment_id": "exp-001",  # operator-set, must survive
                "cluster": "old-cluster",  # operator-set, must win on conflict
                "total_tasks": 999,  # stale, must be overridden by tasks.total()
            }
        )
    )
    intent = _minimal_intent(
        3,
        cluster_target={"cluster": "new-cluster", "profile": "gpu-a100"},
    )

    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)

    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["experiment_id"] == "exp-001"  # preserved
    assert meta["cluster"] == "old-cluster"  # operator wins on conflict
    assert meta["profile"] == "gpu-a100"  # net-new from intent
    assert meta["total_tasks"] == 3  # tasks.total() always authoritative


# ─── cross-checks ─────────────────────────────────────────────────────────


def test_task_count_mismatch_raises(tmp_path: Path) -> None:
    """Cross-check: intent.task_count must equal tasks.total()."""
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)  # 3 tasks
    intent = _minimal_intent(99)  # operator says 99
    with pytest.raises(errors.SpecInvalid, match="task_count = 99 but tasks.total"):
        record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    # On mismatch, interview.json must NOT be written (atomicity).
    assert not (tmp_path / "interview.json").exists()


def test_missing_tasks_py_raises(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="missing .hpc/tasks.py"):
        record_interview(InterviewSpec.model_validate(_minimal_intent(1)), campaign_dir=tmp_path)


def test_empty_tasks_py_raises(tmp_path: Path) -> None:
    """tasks.total() == 0 is rejected explicitly with a clear error rather
    than slipping through to a divide-by-zero downstream."""
    _write_tasks(tmp_path, "def total(): return 0\ndef resolve(i): raise IndexError\n")
    with pytest.raises(errors.SpecInvalid, match="no tasks to dispatch"):
        record_interview(InterviewSpec.model_validate(_minimal_intent(1)), campaign_dir=tmp_path)


# ─── task_generator: typed materializer ────────────────────────────────────


def test_generator_enumerated(tmp_path: Path) -> None:
    """Most agnostic shape: items list verbatim. Covers eval / RL / etc."""
    intent = _minimal_intent(
        3,
        task_kind="llm-eval",
        task_generator={
            "kind": "enumerated",
            "params": {
                "items": [
                    {"model": "opus-4.7", "dataset": "mmlu-pro"},
                    {"model": "sonnet-4.6", "dataset": "mmlu-pro"},
                    {"model": "haiku-4.5", "dataset": "mmlu-pro"},
                ]
            },
        },
    )
    data = record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    assert data["total_tasks"] == 3
    assert ".hpc/tasks.py" in data["artifacts"]
    assert (tmp_path / ".hpc" / "tasks.py").is_file()
    assert data["preview"]["first"] == {"model": "opus-4.7", "dataset": "mmlu-pro"}


def test_generator_cartesian_product(tmp_path: Path) -> None:
    """Cross-product over named axes. resolve(i) is dict-shaped."""
    intent = _minimal_intent(
        6,
        task_generator={
            "kind": "cartesian_product",
            "params": {"axes": {"lr": [1e-4, 1e-3, 1e-2], "batch_size": [16, 32]}},
        },
    )
    data = record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    assert data["total_tasks"] == 6
    assert set(data["preview"]["first"]) == {"lr", "batch_size"}


# ─── #195: fixed (non-axis) params baked into every task ────────────────────


def test_fixed_params_baked_into_every_resolve(tmp_path: Path) -> None:
    """A required executor param the user didn't sweep (e.g. ``samples``) is
    declared as fixed_params and lands in EVERY task's kwargs — so the cluster
    exports HPC_KW_SAMPLES and the executor command is complete (#195)."""
    intent = _minimal_intent(
        3,
        task_generator={"kind": "cartesian_product", "params": {"axes": {"seed": [0, 1, 2]}}},
        entry_point={
            "kind": "shell_command",
            "run_name": "monte_carlo_pi",
            "argv": ["python3", "mc.py", "--seed", "{seed}", "--samples", "{samples}"],
            "signature": {"seed": "int", "samples": "int"},
            "fixed_params": {"samples": 10000},
        },
    )
    data = record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    # Baked into every task; the axis varies, the fixed value is constant, and
    # the int type is preserved (not stringified) through repr().
    assert data["preview"]["first"] == {"seed": 0, "samples": 10000}
    assert data["preview"]["last"] == {"seed": 2, "samples": 10000}


def test_fixed_params_requires_task_generator(tmp_path: Path) -> None:
    """Like frozen_configs, fixed_params can only be threaded into a
    framework-materialized tasks.py — refuse it on a hand-written one."""
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    intent = _minimal_intent(
        3,
        entry_point={
            "kind": "register_run",
            "run_name": "run",
            "fixed_params": {"samples": 10000},
        },
    )
    with pytest.raises(errors.SpecInvalid, match="fixed_params requires task_generator"):
        record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)


def test_fixed_params_rejects_non_identifier_key() -> None:
    """fixed_params become kwargs on resolve(i); keys must be identifiers."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="fixed_params"):
        InterviewSpec.model_validate(
            _minimal_intent(
                1,
                task_generator={"kind": "cartesian_product", "params": {"axes": {"seed": [0]}}},
                entry_point={
                    "kind": "register_run",
                    "run_name": "run",
                    "fixed_params": {"not a valid name": 1},
                },
            )
        )


def test_generator_items_x_seeds(tmp_path: Path) -> None:
    """Cross items × seeds; seed key on items is overwritten by the cross."""
    intent = _minimal_intent(
        4,
        task_generator={
            "kind": "items_x_seeds",
            "params": {
                "items": [{"env": "cartpole"}, {"env": "lunarlander"}],
                "seeds": [0, 1],
            },
        },
    )
    data = record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    assert data["total_tasks"] == 4
    first = data["preview"]["first"]
    assert "env" in first and "seed" in first


def test_generator_items_x_seeds_default_items(tmp_path: Path) -> None:
    """``items`` defaults to ``[{}]`` so the no-frozen-config case is just
    ``{"kind": "items_x_seeds", "params": {"seeds": [...]}}``. Common when a
    user just wants to sweep seeds with no extra frozen kwargs."""
    intent = _minimal_intent(
        3,
        task_generator={
            "kind": "items_x_seeds",
            "params": {"seeds": [0, 1, 2]},
        },
    )
    data = record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    assert data["total_tasks"] == 3
    first = data["preview"]["first"]
    assert first == {"seed": 0}


def test_generator_numeric_logspace(tmp_path: Path) -> None:
    """Logspace covers low→high inclusive at endpoints."""
    intent = _minimal_intent(
        5,
        task_generator={
            "kind": "numeric_logspace",
            "params": {"param": "lr", "low": 1e-5, "high": 1e-1, "n": 5},
        },
    )
    data = record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    assert data["total_tasks"] == 5
    assert abs(data["preview"]["first"]["lr"] - 1e-5) < 1e-12
    assert abs(data["preview"]["last"]["lr"] - 1e-1) < 1e-12


def test_generator_numeric_linspace(tmp_path: Path) -> None:
    intent = _minimal_intent(
        4,
        task_generator={
            "kind": "numeric_linspace",
            "params": {"param": "alpha", "low": 0.0, "high": 1.0, "n": 4},
        },
    )
    data = record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    assert data["total_tasks"] == 4
    assert data["preview"]["first"]["alpha"] == 0.0
    assert data["preview"]["last"]["alpha"] == 1.0


def test_generator_count_mismatch_does_not_write_tasks_py(tmp_path: Path) -> None:
    """Recipe says 5 tasks; intent says 99. Refuse before any disk write."""
    intent = _minimal_intent(
        99,  # operator-stated count
        task_generator={
            "kind": "numeric_linspace",
            "params": {"param": "x", "low": 0, "high": 1, "n": 5},  # actually 5 tasks
        },
    )
    with pytest.raises(errors.SpecInvalid, match="recipe and stated count disagree"):
        record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    assert not (tmp_path / ".hpc" / "tasks.py").exists()
    assert not (tmp_path / "interview.json").exists()


def test_generator_regenerate_is_byte_equivalent(tmp_path: Path) -> None:
    """Generator mode is idempotent: tasks.py bytes don't change on re-run."""
    intent = _minimal_intent(
        3,
        task_generator={
            "kind": "enumerated",
            "params": {"items": [{"a": 1}, {"a": 2}, {"a": 3}]},
        },
    )
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    first = (tmp_path / ".hpc" / "tasks.py").read_bytes()
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    second = (tmp_path / ".hpc" / "tasks.py").read_bytes()
    assert first == second


# ─── entry_point: shell_command wrapper materialization ──────────────────


def _seed_yaml(campaign_dir: Path, rel: str, body: str) -> Path:
    p = campaign_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def test_entry_point_shell_command_writes_wrapper_with_register_run(tmp_path: Path) -> None:
    """A shell_command entry_point materializes ``.hpc/wrappers/<name>.py``
    decorated with @register_run, with the declared signature plus
    ``**kwargs`` for framework-injected fields."""
    intent = _minimal_intent(
        3,
        entry_point={
            "kind": "shell_command",
            "run_name": "forecast",
            "argv": ["python3", "main.py", "--seed", "{seed}"],
            "signature": {"seed": "int"},
            "frozen_configs": [],
        },
        task_generator={
            "kind": "enumerated",
            "params": {"items": [{"seed": 0}, {"seed": 1}, {"seed": 2}]},
        },
    )
    result = record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)

    wrapper = tmp_path / ".hpc" / "wrappers" / "forecast.py"
    assert wrapper.is_file()
    assert ".hpc/wrappers/forecast.py" in result["artifacts"]
    body = wrapper.read_text()
    # The wrapper carries @register_run + the declared signature + **kwargs.
    assert "@register_run" in body
    assert "def forecast(seed: int, **kwargs)" in body
    # subprocess.check_call with the argv, every placeholder str()-wrapped.
    assert "subprocess.check_call(['python3', 'main.py', '--seed', str(seed)])" in body


def test_entry_point_frozen_configs_threaded_into_kwargs(tmp_path: Path) -> None:
    """frozen_configs are hashed; ``<basename>_sha`` lands in every task's
    kwargs so cmd_sha distinguishes content versions."""
    _seed_yaml(tmp_path, "configs/exp_42.yaml", "lr: 1e-3\n")
    intent = _minimal_intent(
        2,
        entry_point={
            "kind": "shell_command",
            "run_name": "forecast",
            "argv": ["python3", "main.py", "--config", "{config}", "--seed", "{seed}"],
            "signature": {"config": "str", "seed": "int"},
            "frozen_configs": ["configs/exp_42.yaml"],
        },
        task_generator={
            "kind": "enumerated",
            "params": {
                "items": [
                    {"config": "configs/exp_42.yaml", "seed": 0},
                    {"config": "configs/exp_42.yaml", "seed": 1},
                ]
            },
        },
    )
    result = record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    first = result["preview"]["first"]
    last = result["preview"]["last"]
    # exp_42_sha is present and equal across tasks (one frozen config → constant).
    assert "exp_42_sha" in first
    assert first["exp_42_sha"] == last["exp_42_sha"]
    # The user's own kwargs survive.
    assert first["config"] == "configs/exp_42.yaml" and first["seed"] == 0


def test_entry_point_yaml_edit_changes_cmd_sha(tmp_path: Path) -> None:
    """The headline contract: editing the frozen YAML changes cmd_sha so
    submit-flow no longer dedups against the prior submit."""
    counter = {"n": 0}

    def _run(yaml_body: str) -> str:
        # Fresh campaign dir per invocation so this is hermetic; index is
        # the disambiguator (two identical YAML bodies share content but
        # need distinct campaign dirs to test the comparison).
        counter["n"] += 1
        sub = tmp_path / f"campaign_{counter['n']}"
        sub.mkdir()
        _seed_yaml(sub, "configs/exp.yaml", yaml_body)
        intent = _minimal_intent(
            2,
            entry_point={
                "kind": "shell_command",
                "run_name": "r",
                "argv": ["python3", "main.py", "--config", "{config}"],
                "signature": {"config": "str"},
                "frozen_configs": ["configs/exp.yaml"],
            },
            task_generator={
                "kind": "enumerated",
                "params": {"items": [{"config": "x"}, {"config": "y"}]},
            },
        )
        return record_interview(InterviewSpec.model_validate(intent), campaign_dir=sub)["cmd_sha"]

    a = _run("lr: 1e-3\n")
    b = _run("lr: 1e-3\n")  # identical content
    c = _run("lr: 1e-2\n")  # one-character edit
    assert a == b, "identical YAML content must produce identical cmd_sha"
    assert a != c, "edited YAML must produce a different cmd_sha (defeats false dedup)"


def test_entry_point_argv_typo_fails_at_spec_validation(tmp_path: Path) -> None:
    """A placeholder in argv that doesn't match any signature param
    fails at Pydantic validation — before any disk write."""
    from pydantic import ValidationError

    intent = _minimal_intent(
        1,
        entry_point={
            "kind": "shell_command",
            "run_name": "r",
            "argv": ["python3", "main.py", "--config", "{cnfig}"],  # typo
            "signature": {"config": "str"},
            "frozen_configs": [],
        },
    )
    with pytest.raises(ValidationError, match="references parameters not in signature"):
        InterviewSpec.model_validate(intent)


def test_entry_point_missing_frozen_config_rejected(tmp_path: Path) -> None:
    """A frozen_configs entry whose path doesn't exist surfaces as
    spec_invalid before the wrapper or tasks.py gets written."""
    intent = _minimal_intent(
        1,
        entry_point={
            "kind": "shell_command",
            "run_name": "r",
            "argv": ["python3", "main.py"],
            "signature": {},
            "frozen_configs": ["configs/does_not_exist.yaml"],
        },
        task_generator={
            "kind": "enumerated",
            "params": {"items": [{"a": 1}]},
        },
    )
    with pytest.raises(errors.SpecInvalid, match="is not a file"):
        record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    # Atomicity: no wrapper, no tasks.py left behind on the failed path.
    assert not (tmp_path / ".hpc" / "wrappers").exists()
    assert not (tmp_path / ".hpc" / "tasks.py").exists()


def test_entry_point_path_traversal_rejected(tmp_path: Path) -> None:
    """A frozen_configs path that escapes campaign_dir is rejected —
    the framework's mental model is configs live inside the rsynced
    experiment dir."""
    intent = _minimal_intent(
        1,
        entry_point={
            "kind": "shell_command",
            "run_name": "r",
            "argv": ["python3", "main.py"],
            "signature": {},
            "frozen_configs": ["../../etc/passwd"],
        },
        task_generator={"kind": "enumerated", "params": {"items": [{"a": 1}]}},
    )
    with pytest.raises(errors.SpecInvalid, match="resolves outside campaign_dir"):
        record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)


def test_entry_point_wrapper_is_importable_python(tmp_path: Path) -> None:
    """End-to-end: the generated wrapper must actually import (the framework
    later loads it via discover_runs / validate-executor-signatures)."""
    import importlib.util

    intent = _minimal_intent(
        1,
        entry_point={
            "kind": "shell_command",
            "run_name": "demo_run",
            "argv": ["echo", "{message}"],
            "signature": {"message": "str"},
            "frozen_configs": [],
        },
        task_generator={
            "kind": "enumerated",
            "params": {"items": [{"message": "hi"}]},
        },
    )
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    wrapper = tmp_path / ".hpc" / "wrappers" / "demo_run.py"
    spec = importlib.util.spec_from_file_location("hpc_wrapper_under_test", wrapper)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # @register_run injects compute() into the module namespace.
    assert hasattr(mod, "demo_run")
    assert hasattr(mod, "compute")


def test_entry_point_argv_with_mixed_literal_and_placeholder(tmp_path: Path) -> None:
    """A token like ``--seed={seed}`` is one argv element; render as an
    f-string so substitution preserves the literal prefix."""
    intent = _minimal_intent(
        1,
        entry_point={
            "kind": "shell_command",
            "run_name": "r",
            "argv": ["python3", "main.py", "--seed={seed}"],
            "signature": {"seed": "int"},
            "frozen_configs": [],
        },
        task_generator={"kind": "enumerated", "params": {"items": [{"seed": 7}]}},
    )
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    body = (tmp_path / ".hpc" / "wrappers" / "r.py").read_text()
    # Mixed token renders as f-string. Plain placeholder renders as str(name).
    assert "f'--seed={seed}'" in body


def test_entry_point_register_run_kind_does_not_materialize_wrapper(tmp_path: Path) -> None:
    """The register_run kind is a pure pointer — no wrapper file written.
    Existing tasks.py-or-hand-rolled flow applies."""
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    # Seed a discoverable @register_run function so the validator passes.
    nb = tmp_path / "notebooks"
    nb.mkdir()
    (nb / "forecast.py").write_text(
        "from hpc_agent.experiment_kit import register_run\n"
        "@register_run\n"
        "def forecast(seed: int = 0) -> dict:\n"
        "    return {'loss': 0.0}\n"
    )
    intent = _minimal_intent(
        3,
        entry_point={"kind": "register_run", "run_name": "forecast"},
    )
    result = record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    assert "tasks.py" not in result["artifacts"]  # not regenerated
    assert "interview.json" in result["artifacts"]
    assert not (tmp_path / ".hpc" / "wrappers").exists()
    # The materialized entry_point block records the pointer for downstream readers.
    doc = json.loads((tmp_path / "interview.json").read_text())
    assert doc["_materialized"]["entry_point"] == {
        "kind": "register_run",
        "run_name": "forecast",
    }


def test_entry_point_register_run_rejects_missing_run(tmp_path: Path) -> None:
    """A register_run pointer to a non-existent function is rejected at intake."""
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    intent = _minimal_intent(
        3,
        entry_point={"kind": "register_run", "run_name": "ghost"},
    )
    with pytest.raises(errors.SpecInvalid, match="no @register_run function"):
        record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)


def test_entry_point_python_module_rejects_missing_module(tmp_path: Path) -> None:
    """A python_module pointer that doesn't import is rejected at intake."""
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    intent = _minimal_intent(
        3,
        entry_point={
            "kind": "python_module",
            "module": "no_such_pkg_xyz_definitely_not_real",
            "function": "main",
        },
    )
    with pytest.raises(errors.SpecInvalid, match="does not import"):
        record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)


def test_entry_point_python_module_rejects_missing_function(tmp_path: Path) -> None:
    """A python_module whose module imports but lacks the function is rejected."""
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    intent = _minimal_intent(
        3,
        entry_point={
            "kind": "python_module",
            "module": "json",  # real module
            "function": "definitely_not_a_real_function",
        },
    )
    with pytest.raises(errors.SpecInvalid, match="has no attribute"):
        record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)


def test_entry_point_python_module_accepts_valid(tmp_path: Path) -> None:
    """python_module pointer to an importable function: accepted; no wrapper."""
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    intent = _minimal_intent(
        3,
        entry_point={"kind": "python_module", "module": "json", "function": "dumps"},
    )
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    assert not (tmp_path / ".hpc" / "wrappers").exists()
    doc = json.loads((tmp_path / "interview.json").read_text())
    assert doc["_materialized"]["entry_point"] == {
        "kind": "python_module",
        "module": "json",
        "function": "dumps",
    }


def test_entry_point_shell_command_frozen_configs_without_generator_rejected(
    tmp_path: Path,
) -> None:
    """shell_command + frozen_configs requires task_generator; the framework
    can't safely edit a hand-written tasks.py to thread the shas."""
    _seed_yaml(tmp_path, "configs/exp_42.yaml", "lr: 1e-3\n")
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    intent = _minimal_intent(
        3,
        entry_point={
            "kind": "shell_command",
            "run_name": "r",
            "argv": ["python3", "main.py", "--config", "{config}"],
            "signature": {"config": "str"},
            "frozen_configs": ["configs/exp_42.yaml"],
        },
        # No task_generator — hand-written tasks.py.
    )
    with pytest.raises(errors.SpecInvalid, match="frozen_configs requires task_generator"):
        record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    # No residue from the rejected spec.
    assert not (tmp_path / ".hpc" / "wrappers").exists()


def test_entry_point_shell_command_data_axis_hint_persisted(tmp_path: Path) -> None:
    """When data_axis_hint is supplied, it lands in _materialized.entry_point.data_axis
    so classify-axis can record it directly without introspection."""
    intent = _minimal_intent(
        2,
        entry_point={
            "kind": "shell_command",
            "run_name": "r",
            "argv": ["python3", "main.py", "--seed", "{seed}"],
            "signature": {"seed": "int"},
            "data_axis_hint": {"kind": "independent"},
        },
        task_generator={
            "kind": "enumerated",
            "params": {"items": [{"seed": 0}, {"seed": 1}]},
        },
    )
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    doc = json.loads((tmp_path / "interview.json").read_text())
    assert doc["_materialized"]["entry_point"]["data_axis"] == {"kind": "independent"}


def test_entry_point_shell_command_data_axis_hint_bounded_halo(tmp_path: Path) -> None:
    """bounded_halo data_axis_hint round-trips with its halo expression."""
    intent = _minimal_intent(
        2,
        entry_point={
            "kind": "shell_command",
            "run_name": "r",
            "argv": ["python3", "main.py", "--window", "{window}"],
            "signature": {"window": "int"},
            "data_axis_hint": {
                "kind": "bounded_halo",
                "halo": {"expr": "window * 48"},
            },
        },
        task_generator={
            "kind": "enumerated",
            "params": {"items": [{"window": 1}, {"window": 2}]},
        },
    )
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    doc = json.loads((tmp_path / "interview.json").read_text())
    assert doc["_materialized"]["entry_point"]["data_axis"] == {
        "kind": "bounded_halo",
        "halo": {"expr": "window * 48"},
    }


def test_entry_point_shell_command_executor_cmd_in_materialized(tmp_path: Path) -> None:
    """_materialized.entry_point.executor_cmd is the shell command callers
    (slash commands, submit-flow orchestrators) feed into submit-flow's
    ``executor`` so the wrapper actually runs on the cluster."""
    intent = _minimal_intent(
        2,
        entry_point={
            "kind": "shell_command",
            "run_name": "forecast",
            "argv": ["python3", "main.py", "--seed", "{seed}"],
            "signature": {"seed": "int"},
        },
        task_generator={
            "kind": "enumerated",
            "params": {"items": [{"seed": 0}, {"seed": 1}]},
        },
    )
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    doc = json.loads((tmp_path / "interview.json").read_text())
    ep_mat = doc["_materialized"]["entry_point"]
    assert ep_mat["wrapper_path"] == ".hpc/wrappers/forecast.py"
    # The executor_cmd builds an args namespace from HPC_KW_* env vars and
    # invokes compute(). Pin the contract by checking key tokens.
    assert "python3 -c" in ep_mat["executor_cmd"]
    assert ".hpc/wrappers/forecast.py" in ep_mat["executor_cmd"]
    assert "HPC_KW_" in ep_mat["executor_cmd"]
    assert "compute" in ep_mat["executor_cmd"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="runs the cluster-contract executor_cmd locally: needs python3 and echo "
    "as real PATH binaries plus POSIX shell quoting (the Linux cluster's runtime, "
    "absent on win32) — the materialized executor_cmd itself is now cross-platform",
)
def test_entry_point_shell_command_executor_cmd_actually_invokes_wrapper(tmp_path: Path) -> None:
    """End-to-end: the executor_cmd persisted to interview.json, when run with
    HPC_KW_* env vars (the dispatcher's contract), actually invokes the
    wrapper which then subprocess-invokes the argv. Closes the
    orphan-wrapper gap — proves the materialized executor_cmd is a
    real shell command the dispatcher can use."""
    import os
    import subprocess

    intent = _minimal_intent(
        1,
        entry_point={
            "kind": "shell_command",
            "run_name": "demo",
            "argv": ["echo", "{message}"],
            "signature": {"message": "str"},
        },
        task_generator={
            "kind": "enumerated",
            "params": {"items": [{"message": "WRAPPER_CHAIN_OK"}]},
        },
    )
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    doc = json.loads((tmp_path / "interview.json").read_text())
    executor_cmd = doc["_materialized"]["entry_point"]["executor_cmd"]

    # Simulate the dispatcher: REPO_DIR points at the campaign, HPC_KW_*
    # carries the kwargs the wrapper signature expects.
    env = {**os.environ, "REPO_DIR": str(tmp_path), "HPC_KW_MESSAGE": "WRAPPER_CHAIN_OK"}
    proc = subprocess.run(
        executor_cmd, shell=True, env=env, capture_output=True, text=True, timeout=15
    )
    assert proc.returncode == 0, f"executor_cmd failed: stderr={proc.stderr!r}"
    # The wrapper subprocess-called `echo WRAPPER_CHAIN_OK`; stdout proves
    # the whole chain works (dispatcher env → wrapper kwargs → echo argv).
    assert "WRAPPER_CHAIN_OK" in proc.stdout


def test_entry_point_shell_command_wrapper_is_idempotent(tmp_path: Path) -> None:
    """Re-running with the same entry_point produces byte-equivalent wrapper
    (no mtime churn, no timestamp embedding in the wrapper body)."""
    intent = _minimal_intent(
        2,
        entry_point={
            "kind": "shell_command",
            "run_name": "r",
            "argv": ["python3", "main.py", "--seed", "{seed}"],
            "signature": {"seed": "int"},
        },
        task_generator={
            "kind": "enumerated",
            "params": {"items": [{"seed": 0}, {"seed": 1}]},
        },
    )
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    wrapper = tmp_path / ".hpc" / "wrappers" / "r.py"
    first_bytes = wrapper.read_bytes()
    first_mtime = wrapper.stat().st_mtime
    # Re-run; the skip-write-when-identical branch should not bump mtime.
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    assert wrapper.read_bytes() == first_bytes
    assert wrapper.stat().st_mtime == first_mtime, (
        "wrapper mtime should not change on a no-op re-run"
    )


def test_generator_then_validate_mode_picks_up_hand_edits(tmp_path: Path) -> None:
    """After dropping task_generator from intent, the next interview
    accepts whatever tasks.py now contains — operator escape hatch."""
    gen_intent = _minimal_intent(
        3,
        task_generator={
            "kind": "enumerated",
            "params": {"items": [{"a": 1}, {"a": 2}, {"a": 3}]},
        },
    )
    record_interview(InterviewSpec.model_validate(gen_intent), campaign_dir=tmp_path)
    # Operator hand-edits the canonical .hpc/tasks.py to add a fourth task
    (tmp_path / ".hpc" / "tasks.py").write_text(
        "_TASKS = [{'a': 1}, {'a': 2}, {'a': 3}, {'a': 4}]\n"
        "def total(): return len(_TASKS)\n"
        "def resolve(i): return _TASKS[i]\n"
    )
    # Re-interview with task_generator dropped and updated count
    edit_intent = _minimal_intent(4)
    data = record_interview(InterviewSpec.model_validate(edit_intent), campaign_dir=tmp_path)
    assert data["total_tasks"] == 4
    # cmd_sha should differ from the generator-mode run
    interview_doc = json.loads((tmp_path / "interview.json").read_text())
    # task_generator key should not be in the persisted interview anymore
    assert "task_generator" not in interview_doc


# ─── idempotency ──────────────────────────────────────────────────────────


def test_re_running_with_same_intent_overwrites_byte_equivalently(tmp_path: Path) -> None:
    """Modulo the _materialized.at timestamp, re-running is a no-op."""
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    intent = _minimal_intent(3, task_kind="ml-hparam-sweep")

    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    first = json.loads((tmp_path / "interview.json").read_text())
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)
    second = json.loads((tmp_path / "interview.json").read_text())

    # Drop timestamps before comparison
    for doc in (first, second):
        doc["_materialized"].pop("at", None)
    assert first == second


# ─── CLI surface ──────────────────────────────────────────────────────────


def _run_cli(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "hpc_agent", *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_help_lists_interview() -> None:
    rc, out, _ = _run_cli("--help")
    assert rc == 0
    assert "interview" in out


def test_cli_emits_envelope(tmp_path: Path) -> None:
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    spec_path = tmp_path / "intent.json"
    spec_path.write_text(json.dumps(_minimal_intent(3, task_kind="smoke")))

    rc, out, err = _run_cli("interview", "--spec", str(spec_path), "--campaign-dir", str(tmp_path))
    assert rc == 0, err
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["data"]["total_tasks"] == 3
    assert set(payload["data"]["preview"]) == {"first", "mid", "last"}


def test_cli_schema_violation_maps_to_user_error(tmp_path: Path) -> None:
    """A spec missing required `produced_by` should fail with EXIT_USER_ERROR."""
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    spec_path = tmp_path / "intent.json"
    spec_path.write_text(json.dumps({"goal": "x", "task_count": 3}))  # no produced_by

    rc, out, err = _run_cli("interview", "--spec", str(spec_path), "--campaign-dir", str(tmp_path))
    assert rc == 1, f"expected EXIT_USER_ERROR; got {rc}; stderr={err}"
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error_code"] == "spec_invalid"


# ─── #178: python_module entry resolves with campaign_dir on sys.path ───────


def test_python_module_entry_resolves_via_campaign_dir(tmp_path: Path) -> None:
    """A ``python_module`` entry importable on the cluster must not false-fail
    local intake — ``campaign_dir`` is put on sys.path for the import (#178)."""
    from hpc_agent.ops.memory.interview import _validate_python_module_entry

    pkg = tmp_path / "executors"  # PEP 420 namespace package (no __init__.py)
    pkg.mkdir(parents=True)
    (pkg / "job.py").write_text("def main():\n    return None\n")

    path_snapshot = list(sys.path)
    module_snapshot = set(sys.modules)
    try:
        assert str(tmp_path.resolve()) not in sys.path  # the bug's precondition
        # Previously raised spec_invalid ("module 'executors.job' does not import").
        _validate_python_module_entry({"module": "executors.job", "function": "main"}, tmp_path)
    finally:
        sys.path[:] = path_snapshot
        for mod in set(sys.modules) - module_snapshot:
            sys.modules.pop(mod, None)


def test_python_module_entry_still_rejects_genuinely_absent(tmp_path: Path) -> None:
    """A truly-absent module still raises — the fix only adds the path (#178)."""
    from hpc_agent.ops.memory.interview import _validate_python_module_entry

    with pytest.raises(errors.SpecInvalid, match="does not import"):
        _validate_python_module_entry({"module": "executors.nope", "function": "main"}, tmp_path)


# ─── #190: project-scoped Claude permissions for the bare worker ────────────


def _read_settings(campaign_dir: Path) -> dict:
    return json.loads((campaign_dir / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_interview_writes_claude_allow_rule(tmp_path: Path) -> None:
    """Onboarding grants the experiment dir the Bash(hpc-agent:*) allow rule so
    a spawned bare worker can drive the CLI headlessly (#190)."""
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    data = record_interview(InterviewSpec.model_validate(_minimal_intent(3)), campaign_dir=tmp_path)

    settings = _read_settings(tmp_path)
    assert settings["permissions"]["allow"] == ["Bash(hpc-agent:*)"]
    # The new artifact is reported the first time it's written.
    assert ".claude/settings.json" in data["artifacts"]


def test_interview_merges_into_existing_settings_without_clobber(tmp_path: Path) -> None:
    """An existing .claude/settings.json is preserved — other keys and other
    allow entries survive; our rule is appended (deduped), never overwriting."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        json.dumps(
            {
                "model": "opus",
                "permissions": {"allow": ["Bash(ls:*)"], "deny": ["Bash(rm:*)"]},
            }
        ),
        encoding="utf-8",
    )
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    record_interview(InterviewSpec.model_validate(_minimal_intent(3)), campaign_dir=tmp_path)

    settings = _read_settings(tmp_path)
    # Pre-existing keys/entries survive.
    assert settings["model"] == "opus"
    assert settings["permissions"]["deny"] == ["Bash(rm:*)"]
    assert "Bash(ls:*)" in settings["permissions"]["allow"]
    # Our rule is appended.
    assert "Bash(hpc-agent:*)" in settings["permissions"]["allow"]


def test_interview_permissions_write_is_idempotent(tmp_path: Path) -> None:
    """Re-running onboarding does not duplicate the rule, and a no-op re-run
    does not re-report the settings file as a fresh artifact."""
    _write_tasks(tmp_path, _HPARAM_TASKS_PY)
    record_interview(InterviewSpec.model_validate(_minimal_intent(3)), campaign_dir=tmp_path)
    data2 = record_interview(
        InterviewSpec.model_validate(_minimal_intent(3)), campaign_dir=tmp_path
    )

    settings = _read_settings(tmp_path)
    # Exactly one occurrence — no dupes on the second pass.
    assert settings["permissions"]["allow"].count("Bash(hpc-agent:*)") == 1
    # Second run was a no-op for the allow rule → not re-listed as an artifact.
    assert ".claude/settings.json" not in data2["artifacts"]
