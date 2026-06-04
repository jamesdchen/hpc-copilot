"""``inspect-parallel-axes``: composite primitive — one-call axes inspection.

WS5 #7. Collapses the multi-``Read`` the ``hpc-build-executor`` /
axes-init companion performs once per executor build — reading
``.hpc/tasks.py`` (to identify each parallel dimension the experimenter
expressed) and ``.hpc/axes.yaml`` (to see what's already recorded and
whether axes-init would refuse-without-force) — into one pure-query CLI
verb the skill can branch on.

Pure query, ``side_effects=[]``: reads files, executes nothing. The
``tasks.py`` body is returned as text (not imported) — the same
no-arbitrary-execution discipline the skill follows with its ``Read``
tool, and faithful to what the companion does today (it *Reads*
``tasks.py`` to eyeball the grid / ``resolve`` shape; it does not run it).

I/O contracts:

* Input: see ``hpc_agent/schemas/inspect_parallel_axes.input.json``.
* Output: a ``dict`` matching ``schemas/inspect_parallel_axes.output.json``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliShape

__all__ = ["inspect_parallel_axes"]

# Trailing chars of tasks.py returned inline. tasks.py is small by
# convention (a FLAGS dict + total()/resolve()); the cap is a guard
# against a pathological hand-grown file bloating the envelope.
_TASKS_TEXT_CHARS: int = 8000


@primitive(
    name="inspect-parallel-axes",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Inspect an experiment's parallel axes in one call: parse "
            ".hpc/axes.yaml (axes / homogeneous_axes / classified executors) "
            "and surface .hpc/tasks.py's body so the build-executor / axes-init "
            "companion can classify dimensions without a multi-Read."
        ),
        verb="inspect-parallel-axes",
        experiment_dir_arg=True,
        requires_ssh=False,
    ),
    agent_facing=True,
)
def inspect_parallel_axes(*, experiment_dir: str | Path) -> dict[str, Any]:
    """Read ``.hpc/axes.yaml`` + ``.hpc/tasks.py`` and summarize parallel axes.

    Returns a dict matching ``schemas/inspect_parallel_axes.output.json``;
    the CLI dispatcher wraps it in a SuccessEnvelope. *experiment_dir*
    accepts both ``str`` (the CLI path) and ``Path``.

    The two signals the companion branches on:

    * ``axes_yaml_present`` + the parsed ``axes`` / ``homogeneous_axes`` /
      ``executors`` — tells the skill whether axes-init has already run
      (and would refuse-without-force), and what it recorded.
    * ``tasks_py_present`` + ``tasks_py_body`` — the raw ``tasks.py`` text
      for the agent to identify each parallel dimension from the grid /
      ``resolve`` shape, exactly as the companion's Step 1 does today.

    A corrupt / schema-violating ``axes.yaml`` is surfaced as
    ``axes_yaml_error`` rather than raised, so the companion gets the
    ``tasks.py`` half even when the YAML half is broken.
    """
    import jsonschema

    from hpc_agent import errors
    from hpc_agent.state.axes import axes_path, read_axes

    exp = Path(experiment_dir)
    axes_file = axes_path(exp)
    tasks_file = exp / ".hpc" / "tasks.py"

    axes_present = axes_file.exists()
    axes_cfg: dict[str, Any] | None = None
    axes_error: str | None = None
    if axes_present:
        # read_axes raises JournalCorrupt (non-mapping top level) or
        # jsonschema.ValidationError (schema violation) on a broken file —
        # surface either as a string so the tasks.py half still returns.
        try:
            axes_cfg = read_axes(exp)
        except (errors.HpcError, jsonschema.ValidationError, OSError) as exc:
            axes_error = str(exc)

    cfg = axes_cfg or {}
    tasks_present = tasks_file.exists()
    tasks_body = ""
    if tasks_present:
        try:
            tasks_body = tasks_file.read_text(encoding="utf-8")[-_TASKS_TEXT_CHARS:]
        except OSError as exc:
            tasks_body = f"[inspect-parallel-axes] could not read tasks.py: {exc}"

    return {
        "experiment_dir": str(exp),
        "axes_yaml_path": str(axes_file),
        "axes_yaml_present": axes_present,
        "axes_yaml_error": axes_error,
        "axes": list(cfg.get("axes") or []),
        "homogeneous_axes": list(cfg.get("homogeneous_axes") or []),
        "executors": dict(cfg.get("executors") or {}),
        "tasks_py_path": str(tasks_file),
        "tasks_py_present": tasks_present,
        "tasks_py_body": tasks_body,
    }
