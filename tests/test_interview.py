"""Tests for the ``interview`` primitive and its CLI adapter.

Spec scope:

- The primitive validates an agent-written tasks.py against a structured
  intent and persists interview.json + (conditionally) meta.json.
- It is *experiment-agnostic at the schema level*: the schema does not
  enumerate search-space shapes (logspace / grid / seeds_x / …) and any
  dict-shaped tasks.py — hyperparameter sweeps, eval matrices, RL
  rollouts, benchmark sweeps — round-trips equally. The dict requirement
  is inherited from claude-hpc's pre-existing tasks.py contract
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

from claude_hpc.atoms.interview import record_interview

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
    (tmp_path / "tasks.py").write_text(tasks_src)
    intent = _minimal_intent(expected_count, task_kind="example-family")

    data = record_interview(intent, campaign_dir=tmp_path)

    assert data["total_tasks"] == expected_count
    # claude-hpc's tasks.py contract requires resolve(i) to return a dict
    # (kwargs get **-unpacked into the user's task function and must be
    # JSON-serializable for cmd_sha). The interview inherits that constraint;
    # it does NOT add further structure (no `lr` field, no `n` field, etc.).
    assert isinstance(data["preview"]["first"], dict)
    assert (tmp_path / "interview.json").is_file()


def test_non_dict_tasks_py_fails_with_existing_contract_error(tmp_path: Path) -> None:
    """Sentinel: if tasks.py returns a non-dict (forbidden by claude-hpc's
    pre-existing contract), the failure happens at compute_cmd_sha — surfaced
    as a TypeError. Locking this so that loosening the dict requirement later
    is a deliberate, multi-place change rather than an accident."""
    (tmp_path / "tasks.py").write_text(
        "_TASKS = [('a', 1), ('b', 2)]\n"
        "def total(): return len(_TASKS)\n"
        "def resolve(i): return _TASKS[i]\n"
    )
    with pytest.raises(TypeError, match="must return a dict"):
        record_interview(_minimal_intent(2), campaign_dir=tmp_path)


# ─── persistence shape ────────────────────────────────────────────────────


def test_interview_json_round_trips_intent_verbatim(tmp_path: Path) -> None:
    """Intent fields are persisted as-is (modulo the _materialized block)."""
    (tmp_path / "tasks.py").write_text(_HPARAM_TASKS_PY)
    intent = _minimal_intent(
        3,
        task_kind="ml-hparam-sweep",
        budget={"gpu_hours": 200, "wall_clock_max_h": 12},
        abort_if={"metric": "val_loss", "above": 5.0, "after_tasks": 2},
        notes="LR range chosen from prior 'narrow sweep' findings",
    )

    record_interview(intent, campaign_dir=tmp_path)

    persisted = json.loads((tmp_path / "interview.json").read_text())
    for key in ("goal", "task_count", "task_kind", "budget", "abort_if", "notes", "produced_by"):
        assert persisted[key] == intent[key], f"intent.{key} did not round-trip"
    assert "_materialized" in persisted
    assert persisted["_materialized"]["total_tasks"] == 3
    assert "cmd_sha" in persisted["_materialized"]


def test_meta_json_only_written_when_intent_supplies_relevant_fields(tmp_path: Path) -> None:
    """No cluster_target and no budget → no meta.json update."""
    (tmp_path / "tasks.py").write_text(_HPARAM_TASKS_PY)
    data = record_interview(_minimal_intent(3), campaign_dir=tmp_path)
    assert data["artifacts"] == ["interview.json"]
    assert not (tmp_path / "meta.json").exists()


def test_meta_json_merge_preserves_existing_keys(tmp_path: Path) -> None:
    """Pre-existing meta.json keys win on conflict; total_tasks is overridden."""
    (tmp_path / "tasks.py").write_text(_HPARAM_TASKS_PY)
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

    record_interview(intent, campaign_dir=tmp_path)

    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["experiment_id"] == "exp-001"  # preserved
    assert meta["cluster"] == "old-cluster"  # operator wins on conflict
    assert meta["profile"] == "gpu-a100"  # net-new from intent
    assert meta["total_tasks"] == 3  # tasks.total() always authoritative


# ─── cross-checks ─────────────────────────────────────────────────────────


def test_task_count_mismatch_raises(tmp_path: Path) -> None:
    """Cross-check: intent.task_count must equal tasks.total()."""
    (tmp_path / "tasks.py").write_text(_HPARAM_TASKS_PY)  # 3 tasks
    intent = _minimal_intent(99)  # operator says 99
    with pytest.raises(ValueError, match="task_count = 99 but tasks.total"):
        record_interview(intent, campaign_dir=tmp_path)
    # On mismatch, interview.json must NOT be written (atomicity).
    assert not (tmp_path / "interview.json").exists()


def test_missing_tasks_py_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing tasks.py"):
        record_interview(_minimal_intent(1), campaign_dir=tmp_path)


def test_empty_tasks_py_raises(tmp_path: Path) -> None:
    """tasks.total() == 0 is rejected explicitly with a clear error rather
    than slipping through to a divide-by-zero downstream."""
    (tmp_path / "tasks.py").write_text("def total(): return 0\ndef resolve(i): raise IndexError\n")
    with pytest.raises(ValueError, match="no tasks to dispatch"):
        record_interview(_minimal_intent(1), campaign_dir=tmp_path)


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
    data = record_interview(intent, campaign_dir=tmp_path)
    assert data["total_tasks"] == 3
    assert "tasks.py" in data["artifacts"]
    assert (tmp_path / "tasks.py").is_file()
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
    data = record_interview(intent, campaign_dir=tmp_path)
    assert data["total_tasks"] == 6
    assert set(data["preview"]["first"]) == {"lr", "batch_size"}


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
    data = record_interview(intent, campaign_dir=tmp_path)
    assert data["total_tasks"] == 4
    first = data["preview"]["first"]
    assert "env" in first and "seed" in first


def test_generator_numeric_logspace(tmp_path: Path) -> None:
    """Logspace covers low→high inclusive at endpoints."""
    intent = _minimal_intent(
        5,
        task_generator={
            "kind": "numeric_logspace",
            "params": {"param": "lr", "low": 1e-5, "high": 1e-1, "n": 5},
        },
    )
    data = record_interview(intent, campaign_dir=tmp_path)
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
    data = record_interview(intent, campaign_dir=tmp_path)
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
    with pytest.raises(ValueError, match="recipe and stated count disagree"):
        record_interview(intent, campaign_dir=tmp_path)
    assert not (tmp_path / "tasks.py").exists()
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
    record_interview(intent, campaign_dir=tmp_path)
    first = (tmp_path / "tasks.py").read_bytes()
    record_interview(intent, campaign_dir=tmp_path)
    second = (tmp_path / "tasks.py").read_bytes()
    assert first == second


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
    record_interview(gen_intent, campaign_dir=tmp_path)
    # Operator hand-edits tasks.py to add a fourth task
    (tmp_path / "tasks.py").write_text(
        "_TASKS = [{'a': 1}, {'a': 2}, {'a': 3}, {'a': 4}]\n"
        "def total(): return len(_TASKS)\n"
        "def resolve(i): return _TASKS[i]\n"
    )
    # Re-interview with task_generator dropped and updated count
    edit_intent = _minimal_intent(4)
    data = record_interview(edit_intent, campaign_dir=tmp_path)
    assert data["total_tasks"] == 4
    # cmd_sha should differ from the generator-mode run
    interview_doc = json.loads((tmp_path / "interview.json").read_text())
    # task_generator key should not be in the persisted interview anymore
    assert "task_generator" not in interview_doc


# ─── idempotency ──────────────────────────────────────────────────────────


def test_re_running_with_same_intent_overwrites_byte_equivalently(tmp_path: Path) -> None:
    """Modulo the _materialized.at timestamp, re-running is a no-op."""
    (tmp_path / "tasks.py").write_text(_HPARAM_TASKS_PY)
    intent = _minimal_intent(3, task_kind="ml-hparam-sweep")

    record_interview(intent, campaign_dir=tmp_path)
    first = json.loads((tmp_path / "interview.json").read_text())
    record_interview(intent, campaign_dir=tmp_path)
    second = json.loads((tmp_path / "interview.json").read_text())

    # Drop timestamps before comparison
    for doc in (first, second):
        doc["_materialized"].pop("at", None)
    assert first == second


# ─── CLI surface ──────────────────────────────────────────────────────────


def _run_cli(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "claude_hpc", *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_help_lists_interview() -> None:
    rc, out, _ = _run_cli("--help")
    assert rc == 0
    assert "interview" in out


def test_cli_emits_envelope(tmp_path: Path) -> None:
    (tmp_path / "tasks.py").write_text(_HPARAM_TASKS_PY)
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
    (tmp_path / "tasks.py").write_text(_HPARAM_TASKS_PY)
    spec_path = tmp_path / "intent.json"
    spec_path.write_text(json.dumps({"goal": "x", "task_count": 3}))  # no produced_by

    rc, out, err = _run_cli("interview", "--spec", str(spec_path), "--campaign-dir", str(tmp_path))
    assert rc == 1, f"expected EXIT_USER_ERROR; got {rc}; stderr={err}"
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error_code"] == "spec_invalid"
