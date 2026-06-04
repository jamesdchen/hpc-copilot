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

# The named subagent inline mode routes to. Its definition
# (src/slash_commands/agents/hpc-worker.md, installed to ~/.claude/agents/ by
# `hpc-agent install-commands`) carries ``model:`` in its own frontmatter, so
# the harness enforces the pin regardless of the caller's model — the pin rides
# with the definition, not the call site. The model hint surfaced in the
# envelope (for a harness dispatching an ad-hoc subagent with a per-call model)
# is read from invoke._WORKER_MODEL at call time — NOT a second copy of the
# string — so the spawn and inline paths can never disagree on the worker model.
_WORKER_SUBAGENT = "hpc-worker"


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
    from hpc_agent._kernel.lifecycle.invoke import (
        _WORKER_MODEL,
        worker_credentials_available,
    )
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
                # Structured routing hint so a harness can dispatch without
                # parsing the prose: the named subagent carries the model pin
                # (model: haiku) in its own definition, so the harness enforces
                # it regardless of the caller's model. `hpc-agent
                # install-commands` ships it to ~/.claude/agents/hpc-worker.md.
                "subagent": {
                    "preferred_name": _WORKER_SUBAGENT,
                    "model": _WORKER_MODEL,
                    "task": prompt,
                },
                "instructions": (
                    "Inline mode: no `claude -p` worker was spawned. Produce the "
                    "worker report the procedure in `prompt` asks for — a single "
                    'JSON object {"result": ..., "decisions": [...], '
                    '"anomalies": "..."}. How you run it depends on your '
                    "capability, in this order:\n"
                    f"- If a named subagent `{_WORKER_SUBAGENT}` is available "
                    "(Claude Code installs it from this package via `hpc-agent "
                    "install-commands`), dispatch exactly ONE subagent of that "
                    "type with `prompt` as its entire task and return the JSON "
                    f"object it produces. `{_WORKER_SUBAGENT}` is pinned to a "
                    "small, cheap model in its own definition — the harness "
                    "enforces that pin, so do not override its model. This keeps "
                    "the procedure's verbose execution (rsync, qsub, scheduler "
                    "dumps) out of your context, recovering the isolation the "
                    "default worker spawn would have given.\n"
                    "- Else if you have a generic subagent-spawning tool (Claude "
                    "Code's `Agent` tool — formerly `Task` — or your harness's "
                    "equivalent), dispatch ONE subagent with `prompt` as its "
                    f"task; pin it to `{_WORKER_MODEL}` if your tool lets you "
                    "choose a model (the procedure is a deterministic sequence, "
                    "not open-ended reasoning).\n"
                    "- Else run the procedure yourself in this session (you have "
                    "full tools and credentials).\n"
                    "The subagent (when used) is the leaf: it runs every step "
                    "itself and spawns nothing further. Either path stays in this "
                    "session — do NOT start a fresh `claude -p`/CLI worker or "
                    "re-invoke `hpc-agent run`; inline deliberately skips that "
                    "spawn.\n"
                    "Isolation ceiling: a subagent recovers *context* isolation "
                    "(the procedure's transcript stays out of your context) but "
                    "NOT *environment* isolation — it shares this session's "
                    "sandbox posture and auto-loads project CLAUDE.md, unlike the "
                    "default `--bare` `claude -p` spawn, which forces the sandbox "
                    "off and strips CLAUDE.md for a reproducible-minimum context. "
                    "If you need that stronger isolation (e.g. a sandboxed session "
                    "would block the cluster SSH, or project memory must not color "
                    "the run), the default spawn — drop `HPC_AGENT_INVOKER=inline` "
                    "— is the right tool, not inline."
                ),
            },
            idempotent=False,
        )
        return EXIT_OK
    # Prompt-cache accounting is collected on EVERY spawn by default (#244) so
    # a silent cache miss surfaces continuously, not only under an opt-in flag.
    # The worker then runs with ``--output-format json``; if that ever
    # misbehaves the unwrap falls back to the raw stdout, and an operator can
    # disable it outright with HPC_AGENT_REPORT_CACHE_STATS=0. The legacy
    # ``--report-cache-stats`` flag is retained (now redundant) for back-compat.
    report_cache_stats = os.environ.get("HPC_AGENT_REPORT_CACHE_STATS", "1") != "0"
    try:
        report, exit_code, cache_stats = run_workflow(
            workflow=args.workflow,
            experiment_dir=str(args.experiment_dir),
            fields=fields,
            report_cache_stats=report_cache_stats,
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
    data = {"mode": "spawn", "report": report.model_dump(), "worker_exit_code": exit_code}
    if report_cache_stats:
        # Surface the worker's prompt-cache accounting so an operator can
        # confirm the cacheable prefix actually hit cache. ``None`` when the
        # transport didn't expose usage (e.g. the worker crashed before
        # emitting an envelope) — reported as-is so the gap is visible.
        data["cache_stats"] = cache_stats
    _ok(data, idempotent=False)
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
        "--report-cache-stats",
        action="store_true",
        help=(
            "Deprecated/no-op: the spawned worker's prompt-cache token "
            "accounting (cache_read_input_tokens / cache_creation_input_tokens "
            "/ input_tokens / output_tokens) is now reported under "
            "`data.cache_stats` on EVERY spawn run by default (#244), so this "
            "flag is no longer needed. The worker runs with `--output-format "
            "json` to capture billing usage; disable the whole behaviour with "
            "HPC_AGENT_REPORT_CACHE_STATS=0. Ignored by --inline (no worker)."
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
