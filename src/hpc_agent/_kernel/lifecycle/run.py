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
from hpc_agent._kernel.extension.spawn_prompt import (
    SpawnContractError,
    WorkerReport,
    parse_worker_report,
    validate_and_render_parts,
)
from hpc_agent._kernel.lifecycle.invoke import get_invoker


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
    invoker = get_invoker()
    # Fail fast with an actionable message when the worker would spawn without a
    # usable credential (e.g. a parent session authenticated via OAuth, which
    # the ``--bare`` child cannot use) instead of an opaque worker-side "Not
    # logged in" surfacing later as a malformed-report crash.
    remediation = invoker.missing_credential_remediation()
    if remediation is not None:
        raise SpawnContractError(remediation)
    invocation = invoker.invoke(prompt, cwd=Path(experiment_dir))
    try:
        report = parse_worker_report(invocation.output, workflow=workflow)
    except SpawnContractError as exc:
        # Include the worker's stderr AND stdout tails (when present) so
        # the error surfaces what the worker actually said before crashing
        # — otherwise debugging a malformed-report failure means "rerun
        # the worker manually with HPC_AGENT_WORKER_DEBUG=1 and hope it
        # repros". `claude -p --bare` often prints informational text to
        # stdout before dying without an envelope; the stderr tail alone
        # is empty in that case (observed on Windows demos).
        def _tail(text: str | None, *, cap: int = 2000) -> str:
            t = (text or "").strip()
            return ("…" + t[-cap:]) if len(t) > cap else t

        stderr_tail = _tail(invocation.stderr)
        stdout_tail = _tail(invocation.output)
        suffix_parts = []
        if stderr_tail:
            suffix_parts.append(f"worker stderr: {stderr_tail}")
        if stdout_tail:
            suffix_parts.append(f"worker stdout: {stdout_tail}")
        # Fallback hint: the spawned worker has its own credential
        # (typically ANTHROPIC_API_KEY for a `--bare` child), separate
        # from the caller's interactive Claude Code OAuth session. When
        # that key hits a quota/billing limit, rotates, or 401/403s, the
        # worker dies before emitting a report — exactly the
        # malformed-report path that lands here. Inline mode skips the
        # spawn entirely and runs the procedure in the caller's session
        # (which has its own creds), so it's the natural recovery for
        # the quota class. Always surfaced — the operator can ignore it
        # when the failure is for a different reason; the cost of an
        # unhelpful hint is a sentence of prose.
        suffix_parts.append(
            "Fallback: if this looks like a workspace API-quota / auth issue "
            "(the spawned worker has its own credential, separate from your "
            "interactive Claude Code session), set HPC_AGENT_INVOKER=inline "
            "and re-run — inline mode runs the procedure in your active "
            "session instead of spawning a new --bare subprocess. Skip this "
            "fallback if the worker is failing for a different reason."
        )
        suffix = ("\n" + "\n".join(suffix_parts)) if suffix_parts else ""
        raise errors.HpcError(
            f"the {workflow!r} worker did not return a valid report "
            f"(exit {invocation.exit_code}): {exc}{suffix}"
        ) from exc
    return report, invocation.exit_code
