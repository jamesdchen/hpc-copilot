"""Tests for the dispatcher-reserved kwarg refusal in ``build-tasks-py``.

The cluster-side dispatcher builds its result_dir format context as
``ctx = {"task_id": ..., "run_id": ..., **kwargs}`` — task kwargs WIN over
the reserved keys (``execution/mapreduce/dispatch.py::_format_result_dir``).
A swept param literally named ``run_id`` / ``task_id`` therefore renders the
placeholder as the per-task KWARG value cluster-side, while aggregate-time
code (``ops/aggregate_flow.py::_render_known_placeholders``) can only know
the REAL run identity — the harvest scope diverges from where the
dispatcher wrote and the pull loudly refuses. ``build-tasks-py`` refuses
those names at scaffold time so the divergence can never be constructed.

Split out of ``test_tasks_py.py`` (the ``test_tasks_py_planner.py``
precedent): that module pre-dates the wire-model types and is not mypy-clean
under the per-file lint guard, so new reserved-name coverage lands here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.build_tasks_py import BuildTasksPyInput
from hpc_agent.incorporation.build.tasks_py import (
    DISPATCHER_FORMAT_RESERVED_KEYS,
    build_tasks_py,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_reserved_keys_are_exactly_run_id_and_task_id() -> None:
    """The refused set is exactly the dispatcher's two format-context keys —
    deliberately NOT ``run_sha.RESERVED_TASK_KEYS`` (that set is hashed
    identity; touching it would change cmd_sha for existing runs)."""
    assert sorted(DISPATCHER_FORMAT_RESERVED_KEYS) == ["run_id", "task_id"]


def test_dispatcher_reserved_axis_name_run_id_rejected(tmp_path: Path) -> None:
    """An axis named ``run_id`` collides with the dispatcher's result_dir
    format context, so it is refused at scaffold time with the param named —
    before this lint, only the env-shadow check ran and bare ``run_id``
    (``RUN_ID`` shadows nothing) sailed through."""
    with pytest.raises(errors.SpecInvalid, match=r"'run_id'.*dispatcher-reserved format"):
        build_tasks_py(
            tmp_path,
            spec=BuildTasksPyInput.model_validate(
                {
                    "axes": [{"name": "run_id", "values": ["a", "b"]}],
                    "flags_by_executor": {"src.ml": [{"name": "run_id", "type": "str"}]},
                }
            ),
        )
    # The scaffold must NOT have been written.
    assert not (tmp_path / ".hpc" / "tasks.py").exists()


def test_dispatcher_reserved_axis_name_task_id_rejected(tmp_path: Path) -> None:
    """``task_id`` was already refused (its uppercase shadows ``$TASK_ID``);
    the dispatcher-format refusal now fires FIRST with the precise reason —
    the env-shadow message must not mask the format-collision one."""
    with pytest.raises(errors.SpecInvalid, match=r"'task_id'.*dispatcher-reserved format"):
        build_tasks_py(
            tmp_path,
            spec=BuildTasksPyInput.model_validate(
                {
                    "axes": [{"name": "task_id", "values": [1, 2]}],
                    "flags_by_executor": {"src.ml": [{"name": "task_id", "type": "int"}]},
                }
            ),
        )
    assert not (tmp_path / ".hpc" / "tasks.py").exists()


def test_near_miss_axis_names_still_allowed(tmp_path: Path) -> None:
    """Control: a normal sweep passes unchanged. Names that merely CONTAIN a
    reserved key do not collide with the dispatcher's exact-key format
    context and must still scaffold — the refusal is surgical, not a
    substring ban."""
    out = build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput.model_validate(
            {
                "axes": [
                    {"name": "exp_run_id", "values": ["a", "b"]},
                    {"name": "task_id_num", "values": [1, 2]},
                ],
                "flags_by_executor": {
                    "src.ml": [
                        {"name": "exp_run_id", "type": "str"},
                        {"name": "task_id_num", "type": "int"},
                    ]
                },
            }
        ),
    )
    assert out["wrote"] is True
    assert out["n_tasks"] == 4
