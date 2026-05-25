"""``hpc-agent run`` — the workflow-spawn orchestrator.

Houses the ``run`` subcommand: the code-orchestrated entrypoint that
validates fields, renders the canonical worker prompt, invokes a
fresh-context ``claude -p`` worker, and emits its parsed report.

This is a Tier 3 verb: a CLI-only orchestrator with no ``@primitive``
backing — it lives outside the registry-driven dispatcher
(:mod:`hpc_agent.cli._dispatch`). The ``register(sub)`` function below
is invoked from
:func:`hpc_agent.cli.parser._register_tier3_modules`.
"""

from __future__ import annotations

import argparse
import json

from hpc_agent.cli._helpers import (
    EXIT_OK,
    _add_experiment_dir,
    _err,
    _ok,
)


def cmd_run(args: argparse.Namespace) -> int:
    """Run a workflow end to end in a fresh-context worker.

    The code-orchestrated entrypoint: validates the fields, renders the
    canonical worker prompt, invokes a worker, and returns its parsed
    report. The spawn is emitted by code here — no PreToolUse hook
    mediates this path. See hpc_agent._kernel.lifecycle.run.
    """
    from hpc_agent._kernel.extension.spawn_prompt import SpawnContractError
    from hpc_agent._kernel.lifecycle.run import run_workflow

    try:
        fields = json.loads(args.fields_json)
    except json.JSONDecodeError as exc:
        return _err(
            error_code="spec_invalid",
            message=f"--fields-json is not valid JSON: {exc}",
            category="user",
            retry_safe=False,
        )
    if not isinstance(fields, dict):
        return _err(
            error_code="spec_invalid",
            message="--fields-json must be a JSON object",
            category="user",
            retry_safe=False,
        )
    try:
        report, exit_code = run_workflow(
            workflow=args.workflow,
            experiment_dir=str(args.experiment_dir),
            fields=fields,
        )
    except SpawnContractError as exc:
        return _err(
            error_code="spec_invalid",
            message=str(exc),
            category="user",
            retry_safe=False,
        )
    # The ``run`` verb spawns a fresh-context worker — side-effectful by
    # design. Mark the envelope non-idempotent so caller retry logic
    # treats it as such (``_ok`` defaults to idempotent=True).
    _ok(
        {"report": report.model_dump(), "worker_exit_code": exit_code},
        idempotent=False,
    )
    return EXIT_OK


def register(sub: argparse._SubParsersAction) -> None:
    """Register the ``run`` subcommand on *sub*.

    Called from :func:`hpc_agent.cli.parser._register_tier3_modules`.
    ``run`` is a Tier 3 verb (no ``@primitive`` backing), so it cannot
    be picked up by the registry walk in
    :func:`hpc_agent.cli.parser._register_from_registry`.
    """
    p_run = sub.add_parser(
        "run",
        help=(
            "Run a workflow (submit / status / aggregate) end to end in a "
            "fresh-context worker — the code-orchestrated entrypoint. "
            "Renders the canonical prompt, invokes a worker, returns its "
            "parsed report. Campaign is a loop; use hpc-campaign-driver."
        ),
    )
    _add_experiment_dir(p_run)
    p_run.add_argument(
        "--workflow",
        required=True,
        # campaign is excluded: it is a loop driven tick-by-tick by
        # hpc-campaign-driver, not a single run.
        choices=["submit", "status", "aggregate"],
        help="Which workflow the fresh-context worker will run.",
    )
    p_run.add_argument(
        "--fields-json",
        type=str,
        default="{}",
        help=(
            "Inline JSON object of the invocation's resolved fields "
            "(interview answers). Default: '{}'."
        ),
    )
    p_run.set_defaults(func=cmd_run)


__all__ = ["cmd_run", "register"]
