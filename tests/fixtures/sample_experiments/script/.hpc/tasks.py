"""Hand-written tasks.py for the script-shape sample experiment fixture.

Two seeds × two learning rates, enumerated. The shape mirrors what
`build-tasks-py` would emit for an `enumerated` task generator — kept
hand-written here so the fixture is self-contained.
"""

from __future__ import annotations

from hpc_agent.executor_cli import flag

FLAGS: dict[str, list] = {
    "train": [
        flag("seed", int, default=0),
        flag("lr", float, default=1e-3),
    ],
}

_TASKS: list[dict] = [
    {"seed": 0, "lr": 1e-3},
    {"seed": 0, "lr": 1e-2},
    {"seed": 1, "lr": 1e-3},
    {"seed": 1, "lr": 1e-2},
]


def total() -> int:
    return len(_TASKS)


def resolve(task_id: int) -> dict:
    return dict(_TASKS[task_id])
