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
from typing import Any

import hpc_agent
from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.state.run_sha import compute_cmd_sha

# The reserved per-task bookkeeping key a closed-loop strategy round-trips
# through ``resolve()``. Must match the member of
# ``hpc_agent.state.run_sha.RESERVED_TASK_KEYS`` (the key compute_cmd_sha
# strips, so it never affects parameter identity).
_TRIAL_TOKEN_KEY = "trial_token"

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
def compute_run_id(experiment_dir: Path, *, run_name: str) -> dict[str, Any]:
    """Return ``{"run_id": ..., "cmd_sha": ..., "trial_tokens": ...}``.

    ``run_id`` is ``"<run_name>-<sha[:8]>"``; ``cmd_sha`` is the full 64-char
    hash. ``trial_tokens`` is the task-ordered list of the opaque
    ``trial_token`` each ``resolve(i)`` returned (``None`` when no task carries
    one) — surfaced here, the one place the task list is materialized, so a
    CLI caller can thread it into ``write-run-sidecar`` and have it round-trip
    to ``prior_records()``.

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
    except (AttributeError, TypeError, ImportError, FileNotFoundError, ValueError, KeyError) as exc:
        raise errors.SpecInvalid(
            f".hpc/tasks.py at {tasks_py} is malformed: {exc} — "
            "run /wrap-entry-point first to rebuild it."
        ) from exc
    try:
        cmd_sha = compute_cmd_sha(tasks)
        # Materialize the per-task trial_token in the SAME load (the only place
        # the task list is realized at submit time). resolve() is idempotent by
        # the eager-materialization convention, so re-reading it here is cheap.
        tokens = [tasks.resolve(i).get(_TRIAL_TOKEN_KEY) for i in range(int(tasks.total()))]
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        raise errors.SpecInvalid(
            f".hpc/tasks.py at {tasks_py} is malformed: {exc} — "
            "run /wrap-entry-point first to rebuild it."
        ) from exc
    # Omit (None) when no task carries a token, so the field stays absent for
    # ordinary non-campaign submits rather than a list of nulls.
    trial_tokens = tokens if any(t is not None for t in tokens) else None
    return {
        "run_id": f"{run_name}-{cmd_sha[:8]}",
        "cmd_sha": cmd_sha,
        "trial_tokens": trial_tokens,
    }
