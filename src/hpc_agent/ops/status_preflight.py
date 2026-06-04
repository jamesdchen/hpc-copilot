"""``status-preflight``: composite primitive (WS5 #3, scaffold).

Collapses the top of ``hpc-status`` — Step 0 (``install-commands``) and
Step 1 (``load-context``) — into one CLI call. The simplest of the
``<skill>-preflight`` family (no ``reconcile`` branch like submit /
aggregate have) and a clean prototype for the pattern.

Sequential by design: ``install-commands`` must succeed before
``load-context`` — install lays down the bundled SKILL.md / agent
files and ``load-context`` may resolve paths that depend on them.
Plain ``subprocess.run`` is sufficient; no asyncio fan-out.

**Scaffold only.** Not registered as a CLI verb yet — the dispatcher
registration is held until WS2 (sub-skill return file primitive) lands
to avoid a dispatcher race. After WS2 lands, follow the same checklist
as for ``submit_preflight``: register in :mod:`hpc_agent.cli.dispatch`;
regenerate ``operations.json``; update ``hpc-status/SKILL.md`` to
invoke ``hpc-agent status-preflight`` instead of the two separate calls.

I/O contracts:

* Input: see ``hpc_agent/schemas/status_preflight.input.json``.
* Output: a ``dict`` matching ``schemas/status_preflight.output.json``.
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
    "status_preflight",
]


@dataclass(frozen=True)
class SubCall:
    """One sub-call within status-preflight (name + full argv)."""

    name: str
    argv: list[str]


def _build_subcalls(*, experiment_dir: Path, skip: list[str]) -> list[SubCall]:
    """Construct one :class:`SubCall` per non-skipped sub-step.

    Order is install-commands first, then load-context — install must
    succeed before load-context can resolve framework paths reliably.
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
    name="status-preflight",
    verb="validate",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Composite preflight before status: install-commands + "
            "load-context, sequenced, returned as one envelope."
        ),
        verb="status-preflight",
        args=(
            CliArg(
                "--experiment-dir",
                type=str,
                required=True,
                help="Absolute path to the experiment directory.",
            ),
        ),
        # install-commands + load-context are both local; no SSH involved.
    ),
    agent_facing=True,
)
def status_preflight(
    *,
    experiment_dir: str | Path,
    skip: list[str] | None = None,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """Run install-commands then load-context; return the composite ``data`` block.

    Returns a dict matching ``schemas/status_preflight.output.json``;
    the CLI dispatcher wraps it in a SuccessEnvelope. *experiment_dir*
    accepts both ``str`` (the CLI path) and ``Path`` (the in-process
    path) and is coerced internally.

    The composite never raises on a sub-call failure — failures surface
    inside ``SubResult.envelope`` so the install-commands run is preserved
    even when load-context blows up.
    """
    experiment_dir_path = (
        experiment_dir if isinstance(experiment_dir, Path) else Path(experiment_dir)
    )
    skip_list = list(skip or [])
    calls = _build_subcalls(experiment_dir=experiment_dir_path, skip=skip_list)

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
    }
