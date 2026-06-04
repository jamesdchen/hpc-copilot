"""``submit-preflight``: composite primitive — top-of-submit boilerplate.

WS5 #1 (repurposed). Mirror of :mod:`status_preflight` with a cluster
SSH-connectivity check on top: collapses the ``install-commands`` +
``load-context`` + (optional) ``check-preflight`` calls at the top of
every ``hpc-submit`` invocation into ONE CLI call so the agent's role
shrinks to one tool call.

**Note on the prior incarnation.** This module previously fanned
``export-package`` + ``plan-throughput`` + ``validate-campaign`` out in
parallel — the framing the 2026-06-04 demo agent improvised. Inspection
showed the trio is at THREE separate Steps in the canonical
``worker_prompts/submit.md`` (Step 0 / 4b / 6c) with hard data
dependencies (plan-throughput needs ``total_tasks`` from grid expansion;
validate-campaign needs the assembled spec), so it can't actually be
parallelised without flow restructuring. The repurposed verb is the
genuinely-composable boilerplate the audit's ``<skill>-preflight`` row
described.

Internal composition: sequential ``subprocess.run`` over the existing
``hpc-agent`` verbs. ``install-commands`` must succeed before
``load-context`` can resolve framework paths reliably; ``check-preflight``
is then run when ``--cluster`` is supplied (the cluster_ssh_echo probe in
that verb exercises the production SSH path so the submit doesn't blow
up post-spec-build with `getsockname` / agent-blind ssh / qsub-on-PATH).

I/O contracts:

* Input: see ``hpc_agent/schemas/submit_preflight.input.json``.
* Output: a ``dict`` matching ``schemas/submit_preflight.output.json``.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

__all__ = [
    "SubCall",
    "submit_preflight",
]


@dataclass(frozen=True)
class SubCall:
    """One sub-call within submit-preflight (name + full argv)."""

    name: str
    argv: list[str]


def _build_subcalls(*, experiment_dir: Path, cluster: str | None, skip: list[str]) -> list[SubCall]:
    """Construct one :class:`SubCall` per non-skipped sub-step.

    Order is install-commands → load-context → check-preflight. install
    must succeed first; check-preflight is appended last because it's
    the most expensive (5s ssh round-trip on the slow path) and we
    want the cheap local checks to fail-fast.
    """
    exp_str = str(experiment_dir)
    calls: list[SubCall] = []

    if "install-commands" not in skip:
        calls.append(SubCall(name="install-commands", argv=["hpc-agent", "install-commands"]))

    if "load-context" not in skip:
        calls.append(
            SubCall(
                name="load-context",
                argv=["hpc-agent", "load-context", "--experiment-dir", exp_str],
            )
        )

    if "check-preflight" not in skip:
        argv = ["hpc-agent", "preflight"]
        if cluster is not None:
            argv += ["--cluster", cluster]
        calls.append(SubCall(name="check-preflight", argv=argv))

    return calls


def _synth_error_subresult(
    *, error_code: str, message: str, category: str, elapsed_sec: float
) -> dict[str, Any]:
    """Build a SubResult whose envelope is a synthesised ErrorEnvelope.

    Used when the sub-call could not emit its own JSON (spawn failure,
    timeout, non-JSON stdout). Matches ErrorEnvelope in envelope.json.
    """
    return {
        "envelope": {
            "ok": False,
            "error_code": error_code,
            "message": message,
            "category": category,
            "retry_safe": False,
        },
        "elapsed_sec": elapsed_sec,
        "ok": False,
    }


def _run_subprocess(call: SubCall, *, timeout_sec: float) -> dict[str, Any]:
    """Run *call.argv* synchronously; return its SubResult dict.

    Captures stdout + stderr; parses stdout as a JSON envelope. Spawn
    failure, timeout, and non-JSON stdout all synthesise a uniform
    ErrorEnvelope so the outer composite can branch consistently.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(
            call.argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return _synth_error_subresult(
            error_code="cluster_timeout",
            message=f"{call.name} exceeded {timeout_sec}s timeout",
            category="cluster",
            elapsed_sec=time.monotonic() - started,
        )
    except OSError as exc:
        return _synth_error_subresult(
            error_code="internal",
            message=f"failed to spawn {call.name}: {exc}",
            category="internal",
            elapsed_sec=time.monotonic() - started,
        )

    elapsed_sec = time.monotonic() - started

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        stderr_tail = (proc.stderr or "")[-400:]
        return {
            "envelope": {
                "ok": False,
                "error_code": "internal",
                "message": (
                    f"{call.name} did not emit a JSON envelope on stdout; "
                    f"stderr tail: {stderr_tail}"
                ),
                "category": "internal",
                "retry_safe": False,
            },
            "elapsed_sec": elapsed_sec,
            "ok": False,
        }

    return {
        "envelope": envelope,
        "elapsed_sec": elapsed_sec,
        "ok": bool(envelope.get("ok", False)),
    }


@primitive(
    name="submit-preflight",
    verb="validate",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Composite preflight at the top of submit: install-commands + "
            "load-context + (when --cluster is supplied) check-preflight."
        ),
        verb="submit-preflight",
        args=(
            CliArg(
                "--experiment-dir",
                type=str,
                required=True,
                help="Absolute path to the experiment directory.",
            ),
            CliArg(
                "--cluster",
                type=str,
                default=None,
                help=(
                    "Optional cluster name. When supplied, check-preflight runs "
                    "the cluster_ssh_echo functional probe through the production "
                    "ssh path; without it, only the local-env checks fire."
                ),
            ),
        ),
        # check-preflight is the SSH-touching sub-call when --cluster is set;
        # declare requires_ssh so WS4's contract test is satisfied.
        requires_ssh=True,
    ),
    agent_facing=True,
)
def submit_preflight(
    *,
    experiment_dir: str | Path,
    cluster: str | None = None,
    skip: list[str] | None = None,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """Run install-commands → load-context → check-preflight.

    Returns a dict matching ``schemas/submit_preflight.output.json``;
    the CLI dispatcher wraps it in a SuccessEnvelope. *experiment_dir*
    accepts both ``str`` (the CLI path) and ``Path`` (the in-process
    path) and is coerced internally.

    The composite never raises on a sub-call failure — failures surface
    inside ``SubResult.envelope`` so the cheaper sub-calls' work is
    preserved even when check-preflight blows up.
    """
    experiment_dir_path = (
        experiment_dir if isinstance(experiment_dir, Path) else Path(experiment_dir)
    )
    skip_list = list(skip or [])
    calls = _build_subcalls(experiment_dir=experiment_dir_path, cluster=cluster, skip=skip_list)

    started = time.monotonic()
    sub_results: list[dict[str, Any]] = []
    for c in calls:
        sub_results.append(_run_subprocess(c, timeout_sec=timeout_sec))
    elapsed_total_sec = time.monotonic() - started

    by_name = {c.name: r for c, r in zip(calls, sub_results, strict=False)}
    overall = "fail" if any(not r["ok"] for r in sub_results) else "pass"

    return {
        "overall": overall,
        "elapsed_total_sec": elapsed_total_sec,
        "install_commands": by_name.get("install-commands"),
        "load_context": by_name.get("load-context"),
        "check_preflight": by_name.get("check-preflight"),
    }
