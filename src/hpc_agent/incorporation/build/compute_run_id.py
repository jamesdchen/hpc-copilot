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
from hpc_agent.state.run_sha import RESERVED_TASK_KEYS, compute_cmd_sha

# The reserved per-task bookkeeping key a closed-loop strategy round-trips
# through ``resolve()``. A member of
# ``hpc_agent.state.run_sha.RESERVED_TASK_KEYS`` (the keys compute_cmd_sha
# strips, so they never affect parameter identity); we read it directly to
# surface ``trial_tokens`` and strip the whole set when surfacing
# ``trial_params``.
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
    """Return ``{"run_id", "cmd_sha", "total", "trial_tokens", "trial_params"}``.

    ``run_id`` is ``"<run_name>-<sha[:8]>"``; ``cmd_sha`` is the full 64-char
    hash. ``total`` is the authoritative task count (``tasks.total()``,
    == ``len(trial_params)``) — the ground truth a caller cross-checks an
    agent-authored ``total_tasks`` / ``task_count`` against (finding 21).
    ``trial_tokens`` is the task-ordered list of the opaque
    ``trial_token`` each ``resolve(i)`` returned (``None`` when no task carries
    one). ``trial_params`` is the task-ordered list of the resolved per-task
    params each ``resolve(i)`` returned, minus
    :data:`~hpc_agent.state.run_sha.RESERVED_TASK_KEYS` (i.e. the exact
    pre-image of ``cmd_sha``) — so a run's params are recoverable from its
    sidecar for provenance / reproducibility, since ``cmd_sha`` is a one-way
    hash. Both are surfaced here, the one place the task list is materialized,
    so a CLI caller can thread them into ``write-run-sidecar`` and have them
    round-trip to ``prior_records()``. The framework never interprets
    ``trial_params`` — they are opaque bytes (see ``docs/design/campaign-seam.md``).

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
        # Materialize the per-task kwargs ONCE in the SAME load (the only place
        # the task list is realized at submit time). resolve() is idempotent by
        # the eager-materialization convention, so re-reading it here is cheap.
        # The reserved bookkeeping key (trial_token) is the reconciliation
        # token; everything else is a swept/resolved param.
        total = int(tasks.total())
        materialized = [tasks.resolve(i) for i in range(total)]
        tokens = [m.get(_TRIAL_TOKEN_KEY) for m in materialized]
        # trial_params is the cmd_sha pre-image: each task's resolved kwargs
        # with the reserved keys stripped, exactly what compute_cmd_sha hashed.
        params: list[dict[str, Any]] = [
            {k: v for k, v in m.items() if k not in RESERVED_TASK_KEYS} for m in materialized
        ]
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
        # The authoritative task count — ``tasks.total()`` materialized ONCE here,
        # the one place the task list is realized at submit time (== len of
        # ``trial_params`` by construction). Surfaced as a named field so a caller
        # cross-checking an agent-authored ``total_tasks`` / ``task_count`` against
        # the ground truth (resolve-submit-inputs, finding 21) names the count
        # directly instead of re-deriving ``len(trial_params)``.
        "total": total,
        "trial_tokens": trial_tokens,
        # Always surfaced (one dict per task): the run's params ARE its
        # provenance, recoverable independent of whether it's a campaign.
        "trial_params": params,
    }
