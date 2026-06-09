"""Headless tick-loop — advance one workflow step per invocation.

This is the neutral substrate the campaign driver configures. It is
deliberately **not** a ``@primitive``. Primitives are pure JSON-in /
JSON-out tools that an agent invokes; this loop does the opposite — it
*drives*, and for judgement steps it may spawn an LLM (``claude -p``).
Keeping that out of the primitive layer preserves the primitives'
side-effect contract, testability, and cost transparency.

Each tick reads the ``delegate`` block emitted by ``hpc-agent
load-context`` and executes the next step:

- ``kind == "cli"`` — a deterministic step. The loop runs the matching
  ``hpc-agent`` verb directly (resolved through the injected
  :data:`StepTable`); no LLM, no cost.
- ``kind == "agent"`` — a judgement step (a fresh submission, a
  ``decide``). The loop runs the injected :data:`JudgementResolver` — the
  default spawns a fresh-context worker (Claude unless ``HPC_AGENT_INVOKER``
  selects another harness) — but **only** when ``--allow-agent-steps`` is
  passed, because spawning a worker is an explicit, opt-in, billable side
  effect.

One step per invocation: idempotent and cron-friendly. Wrap it in cron
or ``/loop`` to walk a sequence — each tick advances exactly one step
and the on-disk state (run sidecars, journal, cursors) is the only
thing carried between ticks.

The mechanism is neutral; the domain knowledge stays with the caller,
injected as a :data:`StepTable` (which deterministic verb each
``delegate.step`` maps to) and a :data:`JudgementResolver` (how an
``agent`` step is executed). This is the same seam
``_kernel/decision/kernel.py`` establishes one level down: the loop owns
the protocol, the caller owns the policy. ``hpc_agent.meta.campaign.driver``
is the caller that supplies the campaign step map and the entry point.

This module MUST NOT import anything from ``meta.campaign`` — the
dependency points campaign -> drive, never the reverse.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hpc_agent._kernel.extension.spawn_prompt import WorkerReport

__all__ = [
    "StepTable",
    "JudgementResolver",
    "load_context",
    "plan_action",
    "default_judgement_resolver",
    "drive_once",
    "drive",
]

# A delegate-step name -> the hpc-agent verb that performs that deterministic
# (``kind == "cli"``) step. Injected by the caller; the loop has no built-in
# vocabulary of its own.
StepTable = Mapping[str, str]

# How an ``agent`` (judgement) step is executed. Takes the delegate block's
# ``spawn_request`` and the experiment dir; returns the parsed worker report
# (to print as the per-tick record) and the worker's exit code. Injected so
# the loop never hardcodes a transport; the default wraps ``run_workflow``.
#
# Contract an injected resolver must honor (the default inherits both from
# ``run_workflow``):
#   * Pre-spawn credential fail-fast — surface an actionable error *before*
#     spawning when no usable credential is present (the default routes through
#     ``WorkerInvoker.missing_credential_remediation`` in ``run.py``), rather
#     than letting the worker die opaquely.
#   * Cache-stats (#244) do NOT ride this 2-tuple — a resolver that wants to
#     surface prompt-cache accounting must report it out of band.
JudgementResolver = Callable[[dict[str, Any], Path], "tuple[WorkerReport, int]"]


def load_context(experiment_dir: Path) -> dict[str, Any]:
    """Run ``hpc-agent load-context`` and return the envelope's ``data``.

    Raises :class:`RuntimeError` when the CLI fails or the envelope is
    not ``ok`` — the loop cannot plan a step without context.
    """
    proc = subprocess.run(
        ["hpc-agent", "load-context", "--experiment-dir", str(experiment_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"load-context failed (exit {proc.returncode}): {proc.stderr.strip()}")
    envelope = json.loads(proc.stdout)
    if not envelope.get("ok"):
        raise RuntimeError(f"load-context returned a non-ok envelope: {envelope}")
    data: dict[str, Any] = envelope["data"]
    return data


def plan_action(
    delegate: dict[str, Any] | None,
    *,
    step_table: StepTable,
    allow_agent_steps: bool,
) -> dict[str, Any]:
    """Map a ``delegate`` block to a concrete action intent.

    Pure function (no I/O) so the routing logic is unit-testable. The
    *step_table* (delegate-step -> hpc-agent verb) is injected by the caller —
    the mechanism stays neutral; the campaign map lives in
    ``meta.campaign.driver.CampaignLoopConfig``. Returns one of:

    - ``{"action": "cli", "verb": ..., "run_id": ..., "step": ...}``
    - ``{"action": "agent", "spawn_request": ..., "step": ...}``
    - ``{"action": "skip", "reason": ...}``
    """
    if not delegate:
        return {"action": "skip", "reason": "load-context returned no delegate block"}

    kind = delegate.get("kind")
    step = delegate.get("step")

    if kind == "cli":
        verb = step_table.get(step) if isinstance(step, str) else None
        if verb is None:
            return {"action": "skip", "reason": f"no cli verb mapped for step {step!r}"}
        run_id = delegate.get("run_id")
        if not run_id:
            return {"action": "skip", "reason": f"cli step {step!r} has no run_id"}
        return {"action": "cli", "verb": verb, "run_id": run_id, "step": step}

    if kind == "agent":
        if not allow_agent_steps:
            return {
                "action": "skip",
                "reason": (
                    f"step {step!r} needs an agent; pass --allow-agent-steps to "
                    "permit the driver to spawn a worker (a billable side effect)"
                ),
            }
        spawn_request = delegate.get("spawn_request")
        if not spawn_request:
            return {
                "action": "skip",
                "reason": f"agent step {step!r} has no spawn_request",
            }
        return {"action": "agent", "spawn_request": spawn_request, "step": step}

    return {"action": "skip", "reason": f"unknown delegate kind {kind!r}"}


def _run_cli_step(verb: str, run_id: str, experiment_dir: Path) -> int:
    """Run a deterministic ``hpc-agent`` workflow verb for *run_id*.

    Both ``monitor-flow`` and ``aggregate-flow`` only *require* ``run_id``
    in their input spec, so a minimal ``{"run_id": ...}`` spec is valid.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix=f"{verb}-spec-", delete=False
    ) as handle:
        json.dump({"run_id": run_id}, handle)
        spec_path = handle.name
    try:
        proc = subprocess.run(
            ["hpc-agent", verb, "--spec", spec_path, "--experiment-dir", str(experiment_dir)],
            check=False,
        )
        return proc.returncode
    finally:
        with contextlib.suppress(OSError):
            os.unlink(spec_path)


def default_judgement_resolver(
    spawn_request: dict[str, Any], experiment_dir: Path
) -> tuple[WorkerReport, int]:
    """Resolve a judgement step via ``claude -p`` — the default resolver.

    *spawn_request* is the delegate block's ``spawn_request`` — a
    ``{workflow, experiment_dir, fields}`` dict. It is handed to
    :func:`hpc_agent._kernel.lifecycle.run.run_workflow`, the same
    code-orchestrated entrypoint ``hpc-agent run`` uses: it validates
    and renders the request into the canonical worker prompt, invokes a
    fresh-context worker, and parses the worker's report.

    Returns the parsed report (which the loop prints as the per-tick record)
    and the worker's exit code. This is the exact current Claude path; the
    seam exists so an alternate transport can be injected (#305) without the
    loop knowing.
    """
    from hpc_agent._kernel.lifecycle.run import run_workflow

    report, exit_code, _cache_stats = run_workflow(
        workflow=spawn_request["workflow"],
        experiment_dir=str(experiment_dir),
        fields=spawn_request.get("fields", {}),
    )
    return report, exit_code


def _run_agent_step(
    spawn_request: dict[str, Any],
    experiment_dir: Path,
    resolver: JudgementResolver,
) -> int:
    """Run a judgement step via the injected *resolver* and record it.

    The resolver returns the parsed worker report and the exit code; the
    report is printed so a cron/`/loop` tick leaves a record of the step.
    """
    report, exit_code = resolver(spawn_request, experiment_dir)
    print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    return exit_code


def drive_once(
    experiment_dir: Path,
    *,
    step_table: StepTable,
    resolver: JudgementResolver,
    allow_agent_steps: bool = False,
    dry_run: bool = False,
) -> int:
    """Advance one workflow step under the caller's policy. Returns an exit code.

    The neutral loop body, free of any CLI/argparse coupling: ``load-context``,
    plan the action against the injected *step_table*, print the
    ``{delegate, plan}`` record, and dispatch — ``cli`` steps run an
    ``hpc-agent`` verb, ``agent`` steps run the injected *resolver*.

    This is the **programmatic** entry an external autonomous agent
    (Optuna / Ax / LangGraph / a custom loop) calls directly, supplying its own
    *step_table* and *resolver* — no argv to synthesize. The argparse
    :func:`drive` wrapper exists only for the console-script surface and is a
    thin shell over this.
    """
    data = load_context(experiment_dir)
    delegate = data.get("delegate")
    plan = plan_action(
        delegate,
        step_table=step_table,
        allow_agent_steps=allow_agent_steps,
    )

    print(json.dumps({"delegate": delegate, "plan": plan}, indent=2, sort_keys=True))

    if dry_run or plan["action"] == "skip":
        return 0
    if plan["action"] == "cli":
        return _run_cli_step(plan["verb"], plan["run_id"], experiment_dir)
    if plan["action"] == "agent":
        return _run_agent_step(plan["spawn_request"], experiment_dir, resolver)
    return 0


def drive(
    argv: list[str] | None,
    *,
    step_table: StepTable,
    resolver: JudgementResolver,
    prog: str,
    description: str,
) -> int:
    """Console-script wrapper: parse args, then delegate to :func:`drive_once`.

    *prog* / *description* let the caller brand the CLI surface (the campaign
    entry point names itself ``hpc-campaign-driver``). All loop behavior lives
    in :func:`drive_once`; this only translates argv into its keyword args.
    """
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=Path.cwd(),
        help="Experiment repo root (default: cwd).",
    )
    parser.add_argument(
        "--allow-agent-steps",
        action="store_true",
        help="Permit the driver to spawn a worker for judgement steps (billable).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned action and exit without executing it.",
    )
    args = parser.parse_args(argv)

    return drive_once(
        args.experiment_dir,
        step_table=step_table,
        resolver=resolver,
        allow_agent_steps=args.allow_agent_steps,
        dry_run=args.dry_run,
    )
