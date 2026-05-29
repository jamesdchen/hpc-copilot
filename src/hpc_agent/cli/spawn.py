"""``hpc-agent run`` — the workflow-spawn orchestrator.

Houses the ``run`` subcommand: the code-orchestrated entrypoint that
validates fields, renders the canonical worker prompt, invokes a
fresh-context ``claude -p`` worker, and emits its parsed report. Its
``--inline`` mode (also ``HPC_AGENT_INVOKER=inline``) skips the spawn and
returns the rendered procedure for the calling agent to run in-session —
delegating to a single subagent when its harness exposes one (which
recovers the worker's context isolation), otherwise in its own context.
The agent-reachable ``--inline`` flag is refused when a spawning worker can
authenticate — inline is then a user opt-in via ``HPC_AGENT_INVOKER=inline``,
not an agent default (#155).

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

# "inline" is a pseudo-invoker selected through the same HPC_AGENT_INVOKER knob
# as the real spawning transports in invoke.py (claude-cli / claude-cli-oauth),
# but it is handled here rather than by get_invoker(): it is the *absence* of a
# worker transport — the calling agent runs the procedure and produces the
# report — not another WorkerInvoker. The --inline flag forces it regardless of
# the env. The trade-off (context rot for no per-command spawn) is the caller's.
_INVOKER_ENV = "HPC_AGENT_INVOKER"
_INLINE_INVOKER = "inline"


def _inline_via_flag(args: argparse.Namespace) -> bool:
    """The agent-reachable ``--inline`` flag was passed."""
    return bool(getattr(args, "inline", False))


def _inline_via_env() -> bool:
    """The user set ``HPC_AGENT_INVOKER=inline`` in the environment.

    This is the deliberate, unconditional opt-in: a shell-level signal the
    in-session agent does not set for itself. Unlike the ``--inline`` flag it is
    never refused by the worker-available guard in :func:`cmd_run` (#155).
    """
    return os.environ.get(_INVOKER_ENV, "").strip().lower() == _INLINE_INVOKER


def cmd_run(args: argparse.Namespace) -> int:
    """Run a workflow end to end in a fresh-context worker.

    The code-orchestrated entrypoint: validates the fields, renders the
    canonical worker prompt, invokes a worker, and returns its parsed
    report. The spawn is emitted by code here — no PreToolUse hook
    mediates this path. See hpc_agent._kernel.lifecycle.run.

    In *inline* mode (``--inline`` / ``HPC_AGENT_INVOKER=inline``) it does NOT spawn:
    it renders the same canonical worker prompt and returns it in the envelope
    under ``data.prompt`` with ``data.mode == "inline"``, so the calling agent
    runs the procedure in-session — handing it to a single subagent when its
    harness exposes one (recovering the worker's context isolation), else in its
    own context — instead of forking a fresh ``claude -p`` worker per command.

    The agent-reachable ``--inline`` flag is REFUSED when a spawning worker can
    authenticate (:func:`worker_credentials_available`) and the user has not set
    ``HPC_AGENT_INVOKER=inline``: inline trades away the worker's context
    isolation and is a user opt-in, not something an agent should synthesize
    around an unfounded worker-auth worry (#155). The env var stays the
    unconditional opt-in.
    """
    from hpc_agent._kernel.extension.spawn_prompt import (
        SpawnContractError,
        validate_and_render_parts,
    )
    from hpc_agent._kernel.lifecycle.invoke import worker_credentials_available
    from hpc_agent._kernel.lifecycle.run import run_workflow

    # --fields-file wins over inline --fields-json: reading the JSON from a file
    # sidesteps the shell-quoting layers that mangle inline backslash paths on
    # Windows (a collapsed `\\`->`\` yields invalid JSON escapes like `\U`).
    fields_file = getattr(args, "fields_file", None)
    source_label = "--fields-json"
    raw_fields = args.fields_json
    if fields_file:
        source_label = f"--fields-file {fields_file}"
        try:
            with open(fields_file, encoding="utf-8") as fh:
                raw_fields = fh.read()
        except OSError as exc:
            return _err(
                error_code="spec_invalid",
                message=f"--fields-file could not be read: {exc}",
                category="user",
                retry_safe=False,
            )
    try:
        fields = json.loads(raw_fields)
    except json.JSONDecodeError as exc:
        return _err(
            error_code="spec_invalid",
            message=f"{source_label} is not valid JSON: {exc}",
            category="user",
            retry_safe=False,
        )
    if not isinstance(fields, dict):
        return _err(
            error_code="spec_invalid",
            message=f"{source_label} must be a JSON object",
            category="user",
            retry_safe=False,
        )
    flag_inline = _inline_via_flag(args)
    env_inline = _inline_via_env()
    if flag_inline and not env_inline and worker_credentials_available():
        # Hard guard (#155): the agent-reachable ``--inline`` FLAG must not let a
        # caller synthesize an inline run when a spawning worker can authenticate.
        # The default isolated worker is correct; inline trades away its context
        # isolation and is a USER opt-in. ``HPC_AGENT_INVOKER=inline`` (a shell
        # signal the agent doesn't set) stays the unconditional opt-in; the flag
        # alone is refused so an agent can't route around an available worker on
        # an unfounded worker-auth worry (the recurrence that reopened #155).
        return _err(
            error_code="spec_invalid",
            message=(
                "--inline not honored: a spawning worker can authenticate "
                "(ANTHROPIC_API_KEY / cloud creds or a Claude Code OAuth login is "
                "present), so the default isolated worker works. Inline trades away "
                "that isolation and is a user opt-in, not an agent default. Drop "
                "--inline to use the default spawn, or set HPC_AGENT_INVOKER=inline "
                "to deliberately force inline."
            ),
            category="user",
            retry_safe=False,
        )
    if flag_inline or env_inline:
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
        # No `claude -p` worker is spawned: hand the rendered procedure back for
        # the calling agent to run in-session — delegating to one subagent when it
        # has that capability, else in its own context — and produce the report.
        _ok(
            {
                "mode": "inline",
                "workflow": args.workflow,
                "experiment_dir": str(args.experiment_dir),
                "prompt": prompt,
                "instructions": (
                    "Inline mode: no `claude -p` worker was spawned. Produce the "
                    "worker report the procedure in `prompt` asks for — a single "
                    'JSON object {"result": ..., "decisions": [...], '
                    '"anomalies": "..."}. How you run it depends on your '
                    "capability:\n"
                    "- If you have a subagent-spawning tool (Claude Code's `Agent` "
                    "tool — formerly `Task` — or your harness's equivalent), prefer "
                    "it: dispatch exactly ONE subagent with `prompt` as its entire "
                    "task and return the JSON object it produces. That keeps the "
                    "procedure's verbose execution (rsync, qsub, scheduler dumps) "
                    "out of your context — recovering the isolation the default "
                    "worker spawn would have given. The subagent is the leaf: it "
                    "runs every step itself and spawns nothing further.\n"
                    "- If you have no subagent capability, run the procedure "
                    "yourself in this session (you have full tools and "
                    "credentials).\n"
                    "Either path stays in this session — do NOT start a fresh "
                    "`claude -p`/CLI worker or re-invoke `hpc-agent run`; inline "
                    "deliberately skips that spawn."
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
        "--fields-file",
        type=str,
        default=None,
        help=(
            "Path to a file holding the fields JSON object. Takes precedence "
            "over --fields-json. Use this to avoid shell-escaping inline JSON "
            "(notably Windows backslash paths, which a quoting layer can mangle "
            "into invalid JSON escapes)."
        ),
    )
    p_run.add_argument(
        "--inline",
        action="store_true",
        help=(
            "Do not spawn a fresh `claude -p` worker; render the workflow "
            "procedure and return it under `data.prompt` (mode=inline) so the "
            "calling agent runs it in-session — delegating to one subagent when it "
            "has that capability (recovering context isolation), else in its own "
            "context. REFUSED when a spawning worker can authenticate "
            "(ANTHROPIC_API_KEY / cloud creds or a Claude Code OAuth login): "
            "inline is then a user opt-in via HPC_AGENT_INVOKER=inline, not an "
            "agent default (#155)."
        ),
    )
    p_run.set_defaults(func=cmd_run)


__all__ = ["cmd_run", "register"]
