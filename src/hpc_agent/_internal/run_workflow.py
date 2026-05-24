"""Code-orchestrated workflow execution — the deterministic entrypoint.

``hpc-agent run <workflow>`` and the campaign driver's agent-step
both call :func:`run_workflow`. It is the *code-orchestrated*
counterpart to the model-orchestrated slash-command path: the spawn is
emitted here, by code, not by an LLM composing a ``Task`` call — so the
worker's prompt is deterministic by construction and no ``PreToolUse``
hook mediates this path.

The function is a thin composition of the spawn contract's public
surface: validate + render the request, invoke a fresh-context worker,
parse its report. Campaign is excluded — it is a loop, driven
tick-by-tick by ``hpc-campaign-driver``, not a single run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.lifecycle.invoke import get_invoker
from hpc_agent.atoms.spawn_prompt import (
    SpawnContractError,
    WorkerReport,
    parse_worker_report,
    validate_and_render_parts,
)


def run_workflow(
    *, workflow: str, experiment_dir: str, fields: dict[str, Any]
) -> tuple[WorkerReport, int]:
    """Run *workflow* end to end in a fresh-context worker.

    Returns the parsed :class:`WorkerReport` and the worker's exit code.
    Raises :class:`SpawnContractError` when the *request* is invalid (a
    user/spec error) and :class:`hpc_agent.errors.HpcError` when the
    *worker* fails to return a valid report (an internal failure — not
    the caller's fault).
    """
    prompt = validate_and_render_parts(
        {"workflow": workflow, "experiment_dir": experiment_dir, "fields": fields}
    )
    invocation = get_invoker().invoke(prompt, cwd=Path(experiment_dir))
    try:
        report = parse_worker_report(invocation.output, workflow=workflow)
    except SpawnContractError as exc:
        raise errors.HpcError(
            f"the {workflow!r} worker did not return a valid report "
            f"(exit {invocation.exit_code}): {exc}"
        ) from exc
    return report, invocation.exit_code
