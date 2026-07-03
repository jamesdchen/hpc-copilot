"""``Stop`` hook — block ending the turn over a committed-but-unadvanced decision.

This is *harness-mediated*, not a CLI ``@primitive``: Claude Code runs it as a
``command`` hook wired into ``~/.claude/settings.json``'s ``hooks.Stop`` array
(see :func:`hpc_agent.agent_assets.install_agent_assets`). It is invoked when
the agent is about to end its turn, receives the Stop payload as JSON on
**stdin**, and may emit ``{"decision": "block", "reason": ...}`` on **stdout**
to make the agent continue instead.

Why it exists
-------------
This generalizes the ``skill-return`` Stop guard to the ``block-drive`` decision
rendezvous (``docs/design/block-drive.md`` §5). At a block's y/nudge boundary
the driver parks: it writes a ``pending_decision`` marker + brief and exits, the
LLM renders the brief as a proposal, and the human answers. On approval the LLM
commits the approved input spec to the decision journal (``response == "y"``).
The §5 Phase-4 failure mode is that the LLM commits the ``y`` and then *ends its
turn* ("recorded, done"), leaving the driver un-advanced — the same 2026-06-10
stall the skill-return guard was built for, hit at the decision-commit boundary.

The subtlety (§5 "the whole subtlety")
--------------------------------------
The guard must **not** force continuation while the driver is merely *waiting
for the human* (Phase 2/3a). Waiting is a valid stop; blocking it would loop the
harness into a void with nothing to advance. The commit-is-the-approval design
is what lets a single filesystem read distinguish the two states with no
heuristic about turn content:

* pending_decision marker set **and** the latest decision record is a ``y`` →
  approval committed but driver unconsumed → **force continue**.
* pending_decision marker set but the latest decision is a *nudge* (or there is
  no decision yet) → still waiting for the human → **silent**.
* no pending_decision marker → not parked → **silent**.

The condition is **self-healing**: the next ``block-drive`` tick consumes the
approved spec and clears the marker, after which ``is_awaiting_decision`` is
False and the guard has nothing to block on. Loop-safe: ``stop_hook_active``
passes straight through.

Loop safety & defensiveness
---------------------------
* ``stop_hook_active`` (Claude Code's marker that this stop is already a
  hook-forced continuation) → clean no-op; the guard blocks a given stop at
  most once, never loops.
* A Stop payload carries no command, so — like the skill-return guard — the
  experiment dir is recovered from ``cwd`` plus every directory the skill-return
  emitter left in its breadcrumb (:func:`known_return_dirs`). That is a
  best-effort superset of likely experiment dirs; a run parked under a dir the
  hook cannot see simply is not forced (the scheduled ``doctor`` tick, §5
  Phase-5, is the out-of-session backstop).
* For a malformed payload, an unreadable journal, or no committed-unadvanced
  decision it is a **clean no-op** — prints nothing, exits ``0``. It never
  raises and never exits non-zero.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from hpc_agent.cli.skill_returns import known_return_dirs

__all__ = ["build_hook_output", "find_committed_unadvanced", "main"]


def find_committed_unadvanced(experiment_dir: Path) -> dict[str, Any] | None:
    """First in-flight run under *experiment_dir* whose ``y`` is committed but
    the driver has not advanced, or ``None``.

    "Committed but unadvanced" = the run still carries a ``pending_decision``
    marker (:func:`hpc_agent.state.journal.is_awaiting_decision`) **and** the
    latest record in its decision journal has ``response == "y"``. A marker with
    a trailing nudge (or no decision yet) is the valid "waiting for the human"
    stop and yields ``None``.

    Returns ``{"run_id", "block", "workflow"}`` for the first such run (block /
    workflow read from the marker; either may be ``None`` if the marker is
    partial). Filesystem / journal errors on any single run are swallowed — a
    run we cannot read is treated as not-pending.
    """
    from hpc_agent.state.decision_journal import read_decisions
    from hpc_agent.state.index import find_in_flight_runs
    from hpc_agent.state.journal import is_awaiting_decision, read_pending_decision

    try:
        records = find_in_flight_runs(experiment_dir)
    except OSError:
        return None

    for record in records:
        run_id = record.run_id
        try:
            if not is_awaiting_decision(run_id, experiment_dir=experiment_dir):
                continue
            decisions = read_decisions(experiment_dir, "run", run_id)
        except (OSError, ValueError):
            continue
        if not decisions:
            continue
        if decisions[-1].get("response") != "y":
            # Latest touchpoint is a nudge / not an approval — still waiting.
            continue
        marker = read_pending_decision(run_id, experiment_dir=experiment_dir)
        return {
            "run_id": run_id,
            "block": marker.get("block"),
            "workflow": marker.get("workflow"),
        }
    return None


def build_hook_output(payload: Any) -> dict[str, Any] | None:
    """Pure core: map a Stop *payload* to a block decision, or ``None``.

    Returns ``None`` (→ caller prints nothing, the stop proceeds) when:

    * *payload* is not a mapping.
    * ``stop_hook_active`` is truthy — this stop is already a hook-forced
      continuation; blocking again would loop.
    * no in-flight run under any resolved experiment dir has a committed
      ``y`` awaiting an un-advanced driver (§5 Phase 2/3a — waiting for the
      human — is a *valid* stop; the guard stays silent, not forcing
      continuation into a void).

    Otherwise returns the Claude Code Stop hook-output shape::

        {"decision": "block", "reason": "<invoke block-drive to advance>"}
    """
    if not isinstance(payload, dict):
        return None

    if payload.get("stop_hook_active"):
        return None

    cwd = payload.get("cwd")
    cwd_dir = Path(cwd) if isinstance(cwd, str) and cwd else Path(os.getcwd())

    # A Stop payload has no command, so the experiment dir cannot be parsed the
    # way the autofetch sibling does. Scan cwd first, then every dir the
    # skill-return emitter recorded (the best-effort breadcrumb superset).
    candidate_dirs: list[Path] = [cwd_dir]
    seen = {cwd_dir.expanduser().resolve()}
    for d in known_return_dirs():
        resolved = d.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            candidate_dirs.append(d)

    for cand in candidate_dirs:
        hit = find_committed_unadvanced(cand)
        if hit is None:
            continue
        run_id = hit["run_id"]
        block = hit.get("block") or "?"
        workflow = hit.get("workflow") or "?"
        reason = (
            f"approved spec committed for {run_id} block {block} — invoke "
            f"`hpc-agent block-drive --run-id {run_id} --workflow {workflow}` "
            "to advance the driver (do not end the turn)."
        )
        return {"decision": "block", "reason": reason}
    return None


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint the harness invokes — read stdin, maybe print, never crash.

    Reads the Stop payload from stdin, runs :func:`build_hook_output`, and
    prints the resulting JSON to stdout when non-``None``. Any unexpected
    error is swallowed and reported as a clean no-op (exit ``0``): a broken
    guard must degrade to today's behaviour (the stop proceeds; the scheduled
    ``doctor`` tick remains the out-of-session backstop), never wedge the
    harness. ``argv`` is accepted for symmetry with other entrypoints but is
    unused.
    """
    del argv
    try:
        raw = sys.stdin.read()
    except OSError:
        return 0

    try:
        payload = json.loads(raw) if raw.strip() else None
    except (json.JSONDecodeError, ValueError):
        return 0

    try:
        output = build_hook_output(payload)
    except Exception:
        return 0

    if output is not None:
        print(json.dumps(output), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the harness
    raise SystemExit(main())
