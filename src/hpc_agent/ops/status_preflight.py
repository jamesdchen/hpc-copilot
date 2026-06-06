"""``status-preflight``: composite primitive (WS5 #3, scaffold).

Collapses the top of ``hpc-status`` — Step 0 (``install-commands``) and
Step 1 (``load-context``) — into one CLI call. The simplest of the
``<skill>-preflight`` family (no ``reconcile`` branch like
``aggregate-preflight`` has) and a clean prototype for the pattern.

Concurrency (#291): ``install-commands`` and ``load-context`` fan out on
a ``ThreadPoolExecutor``. They are write-disjoint AND read-disjoint —
``install-commands`` writes only ``~/.claude/{commands,skills,agents}/``
plus ``~/.claude/settings.json``; ``load-context`` reads only
``$EXPERIMENT/.hpc/runs/*.json``, ``.hpc/journal/*.json``, and
``.hpc/campaigns/<id>/cursor.json``. The earlier "install must succeed
first so load-context can resolve framework paths" claim was inert: the
audit (#289) flagged it as a strict data-dependent chain; a focused
source-walk verified no ``~/.claude`` reads anywhere in load-context's
transitive call tree. Fanning saves ~50-150 ms per status poll.

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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

__all__ = [
    "SubCall",
    "status_preflight",
]

# install-commands and load-context are write-disjoint AND read-disjoint
# (#291), so they fan out concurrently on a thread pool. The composite's
# wall-clock for the pair is bounded by the slower of the two, not their
# sum.
_PARALLEL_SUBCALLS = frozenset({"install-commands", "load-context"})


@dataclass(frozen=True)
class SubCall:
    """One sub-call within status-preflight (name + full argv)."""

    name: str
    argv: list[str]


def _build_subcalls(*, experiment_dir: Path, skip: list[str]) -> list[SubCall]:
    """Construct one :class:`SubCall` per non-skipped sub-step.

    install-commands and load-context are both members of
    :data:`_PARALLEL_SUBCALLS` and fan out concurrently at run time
    (#291). The listing order here is purely conventional — the runner
    dispatches both on a thread pool.
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


def _run_subcalls(calls: list[SubCall], *, timeout_sec: float) -> dict[str, dict[str, Any]]:
    """Run *calls*: members of :data:`_PARALLEL_SUBCALLS` fan out concurrently.

    install-commands and load-context are write-disjoint AND read-disjoint
    (#291), so they fan out on a thread pool. With a single call (e.g. one
    arm skipped) the pool is unnecessary and we run inline. Returns
    ``{name: SubResult}``; a sub-call failure surfaces inside its
    ``SubResult.envelope`` rather than raising, so the healthy arm's work
    is preserved.
    """
    results: dict[str, dict[str, Any]] = {}

    parallel = [c for c in calls if c.name in _PARALLEL_SUBCALLS]
    sequential = [c for c in calls if c.name not in _PARALLEL_SUBCALLS]

    if len(parallel) == 1:
        results[parallel[0].name] = _run_subprocess(parallel[0], timeout_sec=timeout_sec)
    elif parallel:
        with ThreadPoolExecutor(max_workers=len(parallel)) as pool:
            futures = {
                pool.submit(_run_subprocess, c, timeout_sec=timeout_sec): c.name for c in parallel
            }
            for fut, name in futures.items():
                results[name] = fut.result()

    for c in sequential:
        results[c.name] = _run_subprocess(c, timeout_sec=timeout_sec)

    return results


@primitive(
    name="status-preflight",
    verb="validate",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Composite preflight before status: install-commands ∥ "
            "load-context, fanned concurrently, returned as one envelope."
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
    """Run install-commands ∥ load-context; return the composite ``data`` block.

    Returns a dict matching ``schemas/status_preflight.output.json``;
    the CLI dispatcher wraps it in a SuccessEnvelope. *experiment_dir*
    accepts both ``str`` (the CLI path) and ``Path`` (the in-process
    path) and is coerced internally.

    install-commands and load-context fan out CONCURRENTLY on a thread
    pool (#291) — they are write-disjoint AND read-disjoint, so the
    composite's wall-clock for the pair is bounded by the slower of the
    two rather than their sum.

    The composite never raises on a sub-call failure — failures surface
    inside ``SubResult.envelope`` so the install-commands run is preserved
    even when load-context blows up. ``overall`` is ``fail`` iff any
    non-skipped sub-call returned ``ok: false`` — fanning never swallows
    a failure.
    """
    experiment_dir_path = (
        experiment_dir if isinstance(experiment_dir, Path) else Path(experiment_dir)
    )
    skip_list = list(skip or [])
    calls = _build_subcalls(experiment_dir=experiment_dir_path, skip=skip_list)

    started = time.monotonic()
    by_name = _run_subcalls(calls, timeout_sec=timeout_sec)
    elapsed_total_sec = time.monotonic() - started

    overall = "fail" if any(not r["ok"] for r in by_name.values()) else "pass"

    return {
        "overall": overall,
        "elapsed_total_sec": elapsed_total_sec,
        "install_commands": by_name.get("install-commands"),
        "load_context": by_name.get("load-context"),
    }
