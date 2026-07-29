"""Reading the shipped workflow plans as SOURCE THAT RUNS, not as text.

The plans under ``src/hpc_agent/slash_commands/workflows`` are JavaScript the
Claude Workflow engine executes. Contracts about them have two arms:

* a **static** arm, which greps the source — necessary (it runs everywhere) and
  provably insufficient (a plan that mentions every right word can still do the
  wrong thing; see the envelope bug in ``test_workflow_plan_commands``);
* an **executed** arm, which runs the plan's OWN functions under node against
  data the Python side really produced. That arm needs the plan's definitions
  without its driver — the plan ends in top-level ``await``/``return`` against
  engine globals (``agent``, ``phase``, ``parallel``, ``args``) that no test
  supplies.

:func:`portable_prefix` is that split. Every plan marks it the same way: the
line ``validatePlan()`` at column 0 is the plan's entry point — everything
above it is definitions (``meta``, ``STEPS``, ``RELAYS``, ``FLAG_RENDER``,
``COMMANDS``, ``parseEnvelope``, and the plan's own predicates), everything
below is the run. Import the prefix, append a harness tail, and the functions
under test are THE shipped ones rather than a Python paraphrase of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests._paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["PLAN_ENTRY_POINT", "WORKFLOWS_DIR", "plan_paths", "portable_prefix"]

WORKFLOWS_DIR = REPO_ROOT / "src/hpc_agent/slash_commands/workflows"

#: The plan's entry point, at column 0. The split marker, not a convention this
#: module invents: :func:`portable_prefix` raises when a plan lacks it rather
#: than silently handing back the whole file (which would not load).
PLAN_ENTRY_POINT = "\nvalidatePlan()\n"


def plan_paths() -> list[Path]:
    """Every shipped workflow plan, sorted."""
    return sorted(WORKFLOWS_DIR.glob("*.js"))


def portable_prefix(text: str, *, name: str) -> str:
    """The plan's definitions — everything above its ``validatePlan()`` call.

    Loadable as an ES module on its own: the driver below the marker is what
    references the engine globals. *name* only names the plan in the error.
    """
    index = text.find(PLAN_ENTRY_POINT)
    if index < 0:
        raise AssertionError(
            f"{name}: no `validatePlan()` entry-point line at column 0 — the executed "
            "contracts split the plan there, so a plan that moved or renamed its entry "
            "point must say so here rather than silently losing its executed arm"
        )
    return text[: index + 1]
