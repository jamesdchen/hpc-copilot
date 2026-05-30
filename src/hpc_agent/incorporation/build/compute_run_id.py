"""``compute-run-id`` primitive — derive a deterministic run_id from tasks.py.

Replaces the inline ``python -c "import uuid, hashlib; ..."`` snippet
that agents previously used to derive a per-experiment ``run_id`` from
the ``cmd_sha`` of the materialized task list. Encoded as a real
primitive so the same shape is reachable from the CLI
(``hpc-agent compute-run-id``) and from Python.

The derivation is intentionally pure: load ``<experiment_dir>/.hpc/tasks.py``,
hash the materialized task list via
:func:`hpc_agent.state.run_sha.compute_cmd_sha`, then format the run_id
as ``<run_name>-<sha[:8]>``. The full 64-char ``cmd_sha`` is returned
alongside so callers don't have to recompute it.
"""

from __future__ import annotations

import re
from pathlib import Path

import hpc_agent
from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.state.run_sha import compute_cmd_sha

# Mirror the ``RunIdStrict`` constraint from
# ``hpc_agent/_wire/_shared.py``: alphanumerics, dot, underscore,
# hyphen. Filesystem-safe and matches what callers persist into
# sidecar paths.
_RUN_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


@primitive(
    name="compute-run-id",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        verb="compute-run-id",
        experiment_dir_arg=True,
        args=(
            CliArg(
                flag="--run-name",
                required=True,
                help="Human-chosen run name; combined with the cmd_sha prefix to form run_id.",
            ),
        ),
        help=("Compute the run_id from .hpc/tasks.py cmd_sha (run_id = <run_name>-<sha[:8]>)."),
    ),
    agent_facing=True,
)
def compute_run_id(experiment_dir: Path, *, run_name: str) -> dict[str, str]:
    """Return ``{"run_id": "<run_name>-<sha[:8]>", "cmd_sha": "<full 64-char sha>"}``.

    Parameters
    ----------
    experiment_dir
        Repo root containing ``.hpc/tasks.py``.
    run_name
        Human-chosen prefix. Must match ``^[A-Za-z0-9._\\-]+$``
        (the same constraint :class:`RunIdStrict` enforces on inputs).

    Raises
    ------
    errors.SpecInvalid
        When ``run_name`` violates the character class, or when
        ``.hpc/tasks.py`` is missing / malformed.
    """
    if not _RUN_NAME_RE.match(run_name):
        raise errors.SpecInvalid(
            f"invalid --run-name {run_name!r}: must match ^[A-Za-z0-9._\\-]+$ "
            "(alphanumerics, dot, underscore, hyphen)."
        )
    tasks_py = Path(experiment_dir) / ".hpc" / "tasks.py"
    if not tasks_py.is_file():
        raise errors.SpecInvalid(
            f".hpc/tasks.py not found under {experiment_dir} — "
            "run /wrap-entry-point first to scaffold the framework layout."
        )
    try:
        tasks = hpc_agent.load_tasks_module(tasks_py)
    except (AttributeError, TypeError, ImportError, FileNotFoundError) as exc:
        raise errors.SpecInvalid(
            f".hpc/tasks.py at {tasks_py} is malformed: {exc} — "
            "run /wrap-entry-point first to rebuild it."
        ) from exc
    try:
        cmd_sha = compute_cmd_sha(tasks)
    except (AttributeError, TypeError) as exc:
        raise errors.SpecInvalid(
            f".hpc/tasks.py at {tasks_py} is malformed: {exc} — "
            "run /wrap-entry-point first to rebuild it."
        ) from exc
    return {"run_id": f"{run_name}-{cmd_sha[:8]}", "cmd_sha": cmd_sha}
