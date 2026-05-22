"""Code-orchestrated workflow execution — the deterministic entrypoint.

``hpc-agent run <workflow>`` (and, in future, the campaign driver's
agent-step) call :func:`run_workflow`. It is the *code-orchestrated*
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

from hpc_agent._internal.invoke import get_invoker
from hpc_agent.atoms.spawn_prompt import (
    SpawnContractError,
    WorkerReport,
    parse_worker_report,
    validate_and_render,
)


def run_workflow(
    *, workflow: str, experiment_dir: str, fields: dict[str, Any]
) -> tuple[WorkerReport, int]:
    """Run *workflow* end to end in a fresh-context worker.

    Returns the parsed :class:`WorkerReport` and the worker's exit code.
    Raises :class:`SpawnContractError` when the request is invalid or
    the worker produces no parseable report.
    """
    prompt = validate_and_render(
        {"workflow": workflow, "experiment_dir": experiment_dir, "fields": fields}
    )
    invocation = get_invoker().invoke(prompt, cwd=Path(experiment_dir))
    try:
        report = parse_worker_report(invocation.output, workflow=workflow)
    except SpawnContractError as exc:
        if invocation.exit_code != 0:
            raise SpawnContractError(
                f"worker exited {invocation.exit_code} and produced no valid report: {exc}"
            ) from exc
        raise
    return report, invocation.exit_code
