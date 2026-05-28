"""``hpc-agent run`` — the workflow-spawn orchestrator.

Houses the ``run`` subcommand: the code-orchestrated entrypoint that
validates fields, renders the canonical worker prompt, invokes a
fresh-context ``claude -p`` worker, and emits its parsed report. Its
``--inline`` mode (also ``HPC_AGENT_INLINE``) skips the spawn and returns
the rendered procedure for the calling agent to run in its own context.

This is a Tier 3 verb: a CLI-only orchestrator with no ``@primitive``
backing — it lives outside the registry-driven dispatcher
(:mod:`hpc_agent.cli._dispatch`). The ``register(sub)`` function below
is invoked from
:func:`hpc_agent.cli.parser._register_tier3_modules`.
"""

from __future__ import annotations

import argparse
import json
import os

from hpc_agent.cli._helpers import (
    EXIT_OK,
    _add_experiment_dir,
    _err,
    _ok,
)

# Env knob that flips the workflow from spawning a fresh `claude -p` worker to
# running its procedure inline in the current agent's context. A CLI `--inline`
# flag overrides it. The trade-off — context rot in exchange for no per-command
# spawn (no extra API cost / latency) — is the caller's to make.
_INLINE_ENV = "HPC_AGENT_INLINE"


def _inline_requested(args: argparse.Namespace) -> bool:
    if getattr(args, "inline", False):
        return True
    return os.environ.get(_INLINE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def cmd_run(args: argparse.Namespace) -> int:
    """Run a workflow end to end in a fresh-context worker.

    The code-orchestrated entrypoint: validates the fields, renders the
    canonical worker prompt, invokes a worker, and returns its parsed
    report. The spawn is emitted by code here — no PreToolUse hook
    mediates this path. See hpc_agent._kernel.lifecycle.run.

    In *inline* mode (``--inline`` / ``HPC_AGENT_INLINE``) it does NOT spawn:
    it renders the same canonical worker prompt and returns it in the envelope
    under ``data.prompt`` with ``data.mode == "inline"``, so the calling agent
    runs the procedure itself in its own context instead of forking a fresh
    ``claude -p`` worker per command.
    """
    from hpc_agent._kernel.extension.spawn_prompt import (
        SpawnContractError,
        validate_and_render_parts,
    )
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
    if _inline_requested(args):
        try:
            prompt = validate_and_render_parts(
                {
                    "workflow": args.workflow,
                    "experiment_dir": str(args.experiment_dir),
                    "fields": fields,
                }
            ).joined
        except SpawnContractError as exc:
            return _err(
                error_code="spec_invalid",
                message=str(exc),
                category="user",
                retry_safe=False,
            )
        # No worker is spawned: hand the rendered procedure back for the calling
        # agent to run in-context and produce the worker report itself.
        _ok(
            {
                "mode": "inline",
                "workflow": args.workflow,
                "experiment_dir": str(args.experiment_dir),
                "prompt": prompt,
                "instructions": (
                    "Inline mode: no worker was spawned. Execute the procedure in "
                    "`prompt` yourself, in this session (you have full tools and "
                    "credentials — do not spawn a worker or another agent), then "
                    "produce the worker report it asks for: a single JSON object "
                    '{"result": ..., "decisions": [...], "anomalies": "..."}.'
                ),
            },
            idempotent=False,
        )
        return EXIT_OK
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
        {"mode": "spawn", "report": report.model_dump(), "worker_exit_code": exit_code},
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
    p_run.add_argument(
        "--inline",
        action="store_true",
        help=(
            "Do not spawn a fresh `claude -p` worker; render the workflow "
            "procedure and return it under `data.prompt` (mode=inline) so the "
            "calling agent runs it in its own context. Trades context isolation "
            "for no per-command spawn. Also enabled by HPC_AGENT_INLINE=1."
        ),
    )
    p_run.set_defaults(func=cmd_run)


__all__ = ["cmd_run", "register"]
