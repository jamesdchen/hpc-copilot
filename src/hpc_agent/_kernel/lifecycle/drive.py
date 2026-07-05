"""Headless tick-loop — advance one deterministic workflow step per invocation.

A neutral substrate for external autonomous loops. It is deliberately
**not** a ``@primitive``: primitives are pure JSON-in / JSON-out tools an
agent invokes; this loop *drives*.

Each tick reads the ``delegate`` block emitted by ``hpc-agent
load-context`` and executes the next step:

- ``kind == "cli"`` — a deterministic step. The loop runs the matching
  ``hpc-agent`` verb directly (resolved through the injected
  :data:`StepTable`); no LLM, no cost.
- ``kind == "agent"`` — a judgement step (a fresh submission, a
  ``decide``). Always planned as ``skip``: a judgement step is a human
  decision boundary, driven via ``block-drive`` — the ``claude -p``
  bare-worker spawn transport this loop used to dispatch was deleted in
  the §6 worker removal (``docs/design/proving-run-2-hardening.md``).

One step per invocation: idempotent and cron-friendly. Wrap it in cron
or ``/loop`` to walk a sequence — each tick advances exactly one step
and the on-disk state (run sidecars, journal, cursors) is the only
thing carried between ticks.

The mechanism is neutral; the domain knowledge stays with the caller,
injected as a :data:`StepTable` (which deterministic verb each
``delegate.step`` maps to). This is the same seam
``_kernel/decision/kernel.py`` establishes one level down: the loop owns
the protocol, the caller owns the policy.

This module also owns the §5 driver-tick watchdog stamps
(:func:`_stamp_driver_tick`, :data:`_DEFAULT_DRIVER_TICK_CADENCE_SECONDS`)
consumed by ``ops/submit/runner.py`` and ``block_drive``.

This module MUST NOT import anything from ``meta.campaign`` — the
dependency points campaign -> drive, never the reverse.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "StepTable",
    "load_context",
    "plan_action",
    "drive_once",
]

# A delegate-step name -> the hpc-agent verb that performs that deterministic
# (``kind == "cli"``) step. Injected by the caller; the loop has no built-in
# vocabulary of its own.
StepTable = Mapping[str, str]


def load_context(experiment_dir: Path) -> dict[str, Any]:
    """Run ``hpc-agent load-context`` and return the envelope's ``data``.

    Raises :class:`RuntimeError` when the CLI fails or the envelope is
    not ``ok`` — the loop cannot plan a step without context.
    """
    try:
        proc = subprocess.run(
            ["hpc-agent", "load-context", "--experiment-dir", str(experiment_dir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            # load-context is a fast, purely local read (journal + config);
            # 120s covers interpreter cold-start + registry walk with margin.
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"load-context timed out after 120s: {exc}") from exc
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
) -> dict[str, Any]:
    """Map a ``delegate`` block to a concrete action intent.

    Pure function (no I/O) so the routing logic is unit-testable. The
    *step_table* (delegate-step -> hpc-agent verb) is injected by the caller —
    the mechanism stays neutral. Returns one of:

    - ``{"action": "cli", "verb": ..., "run_id": ..., "step": ...}``
    - ``{"action": "skip", "reason": ...}``

    An ``agent`` (judgement) delegate always plans ``skip``: a judgement step
    is a human decision boundary, driven via ``block-drive`` — the
    ``claude -p`` worker spawn transport this loop used to dispatch was
    deleted in the §6 worker removal.
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
        return {
            "action": "skip",
            "reason": (
                f"step {step!r} is a human decision boundary — drive it via "
                "block-drive (the worker spawn transport was removed)"
            ),
        }

    return {"action": "skip", "reason": f"unknown delegate kind {kind!r}"}


# Fallback watchdog deadline (seconds) when the tick left no cadence hint — a
# generous default so a cron/`/loop` schedule that ticks less often than a
# monitor poll does not false-alarm the ``doctor`` verb (§5).
_DEFAULT_DRIVER_TICK_CADENCE_SECONDS = 900.0


def _driver_tick_cadence_seconds(experiment_dir: Path, run_id: str) -> float:
    """Best-effort read of the cadence the last monitor tick chose for *run_id*.

    Reads ``next_tick_seconds`` from the tail of the run's
    ``<run_id>.monitor.jsonl`` tick log (the only durable cadence signal in the
    system, written by the monitor step this driver tick just ran). Falls back
    to :data:`_DEFAULT_DRIVER_TICK_CADENCE_SECONDS` when absent, non-positive, or
    unreadable — deriving the watchdog deadline from the pace the tick itself set
    (design §5) rather than a fixed constant. Never raises.
    """
    try:
        from hpc_agent.state.run_record import runs_dir

        path = runs_dir(experiment_dir) / f"{run_id}.monitor.jsonl"
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            nxt = json.loads(stripped).get("next_tick_seconds")
            if isinstance(nxt, (int, float)) and not isinstance(nxt, bool) and nxt > 0:
                return float(nxt)
            break  # last record carried no cadence hint → default
    except Exception:  # noqa: BLE001 — cadence read is best-effort
        return _DEFAULT_DRIVER_TICK_CADENCE_SECONDS
    return _DEFAULT_DRIVER_TICK_CADENCE_SECONDS


def _stamp_driver_tick(experiment_dir: Path, run_id: str) -> None:
    """Stamp the driver dead-man's-switch fields for *run_id* (§5 watchdog).

    Every tick records ``last_tick_at`` + ``next_tick_due`` in the journal so an
    independent failure domain (the in-session timer, or the OS-scheduled
    ``doctor`` verb) can detect a stalled driver. ``next_tick_due`` is derived
    from the cadence the tick itself chose (see
    :func:`_driver_tick_cadence_seconds`). Best-effort and fully guarded: the
    journal record is the primary state and a stamping failure (no record yet,
    lock contention, clock issue) must never crash the tick.
    """
    try:
        from datetime import timedelta

        from hpc_agent.infra.time import utcnow
        from hpc_agent.state.journal import stamp_tick

        now_dt = utcnow()
        cadence = _driver_tick_cadence_seconds(experiment_dir, run_id)
        due = (now_dt + timedelta(seconds=cadence)).isoformat(timespec="seconds")
        stamp_tick(
            run_id,
            last_tick_at=now_dt.isoformat(timespec="seconds"),
            next_tick_due=due,
            experiment_dir=experiment_dir,
        )
    except Exception:  # noqa: BLE001 — stamping must never crash the tick
        # ... but a failing stamp blinds the watchdog (the doctor reads these
        # fields), and "either side dying is loud" (§5). Warn, don't vanish.
        logging.getLogger(__name__).warning(
            "watchdog stamp failed for run %s — doctor cannot see this driver until a stamp lands",
            run_id,
            exc_info=True,
        )


def _cli_step_argv(verb: str, spec_path: str, experiment_dir: Path) -> list[str]:
    """The argv one CLI step runs (a seam so tests can substitute a child)."""
    return ["hpc-agent", verb, "--spec", spec_path, "--experiment-dir", str(experiment_dir)]


# Exit code for a step whose child exceeded its deadline and was killed — the
# coreutils ``timeout(1)`` convention (mirrors ``block_drive._TIMEOUT_EXIT_CODE``).
_TIMEOUT_EXIT_CODE = 124


def _run_cli_step(verb: str, run_id: str, experiment_dir: Path) -> int:
    """Run a deterministic ``hpc-agent`` workflow verb for *run_id*.

    Both ``monitor-flow`` and ``aggregate-flow`` only *require* ``run_id``
    in their input spec, so a minimal ``{"run_id": ...}`` spec is valid.

    The wait is BOUNDED by the per-verb deadline from the block registry
    (:func:`block_chain.verb_deadline_seconds`; the flow verbs fall in its
    watch class, so the deadline is the 24 h default budget + slack). stdio is
    inherited (no pipes), so on expiry ``subprocess.run`` kills the child and
    returns immediately — no post-kill drain wedge to guard; the step reports
    :data:`_TIMEOUT_EXIT_CODE`.
    """
    from hpc_agent.infra import block_chain

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix=f"{verb}-spec-", delete=False
    ) as handle:
        json.dump({"run_id": run_id}, handle)
        spec_path = handle.name
    deadline = block_chain.verb_deadline_seconds(verb, {"run_id": run_id})
    try:
        proc = subprocess.run(
            _cli_step_argv(verb, spec_path, experiment_dir),
            check=False,
            timeout=deadline,
        )
        return proc.returncode
    except subprocess.TimeoutExpired:
        logging.getLogger(__name__).warning(
            "cli step %s exceeded its %.0fs driver deadline — child killed", verb, deadline
        )
        return _TIMEOUT_EXIT_CODE
    finally:
        with contextlib.suppress(OSError):
            os.unlink(spec_path)


def drive_once(
    experiment_dir: Path,
    *,
    step_table: StepTable,
    dry_run: bool = False,
) -> int:
    """Advance one deterministic workflow step. Returns an exit code.

    The neutral loop body, free of any CLI/argparse coupling: ``load-context``,
    plan the action against the injected *step_table*, print the
    ``{delegate, plan}`` record, and dispatch — ``cli`` steps run an
    ``hpc-agent`` verb; ``agent`` (judgement) steps always skip, because a
    judgement step is a human decision boundary driven via ``block-drive``
    (the ``claude -p`` worker spawn transport was deleted in the §6 worker
    removal — see ``docs/design/proving-run-2-hardening.md`` Move 3).

    This is the **programmatic** entry an external autonomous loop
    (Optuna / Ax / a custom controller) calls directly, supplying its own
    *step_table* — no argv to synthesize.
    """
    data = load_context(experiment_dir)
    delegate = data.get("delegate")
    plan = plan_action(delegate, step_table=step_table)

    print(json.dumps({"delegate": delegate, "plan": plan}, indent=2, sort_keys=True))

    if dry_run:
        return 0
    # ``skip`` plans don't carry a run_id, but the delegate block they came
    # from does — recover it so that branch can re-stamp too.
    run_id = delegate.get("run_id") if isinstance(delegate, dict) else None
    if plan["action"] == "skip":
        # A skip still refreshes the dead-man's-switch deadline when there IS a
        # run to stamp, so a live driver repeatedly idling on skip steps is not
        # mistaken for a stalled one (find_stalled_runs reads next_tick_due). A
        # skip with no run_id has nothing to stamp — guard for it.
        if run_id:
            _stamp_driver_tick(experiment_dir, run_id)
        return 0
    if plan["action"] == "cli":
        exit_code = _run_cli_step(plan["verb"], plan["run_id"], experiment_dir)
        # Stamp the dead-man's switch AFTER the step, so the cadence it chose is
        # already on disk to derive next_tick_due from (§5). Guarded — never
        # perturbs the step's own exit code.
        _stamp_driver_tick(experiment_dir, plan["run_id"])
        return exit_code
    return 0
