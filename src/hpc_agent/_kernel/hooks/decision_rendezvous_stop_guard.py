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

* pending_decision marker set **and** the latest decision record is a ``y`` that
  TARGETS this boundary (``resolved["next_block"]`` names the parked
  ``next_verb`` and its ``ts`` is at/after the marker's ``awaiting_since``) →
  approval committed but driver unconsumed → **force continue**.
* pending_decision marker set but the latest decision is a *nudge* (or there is
  no decision yet), OR the latest ``y`` is a prior boundary's already-consumed
  greenlight / a same-boundary re-park's stale one that does NOT target this
  boundary → still waiting for the human → **silent**.
* no pending_decision marker → not parked → **silent**.

The condition is **self-healing**: the next ``block-drive`` tick consumes the
approved spec and clears the marker, after which ``is_awaiting_decision`` is
False and the guard has nothing to block on. Loop-safe: ``stop_hook_active``
passes straight through.

The rejector → completer (RULED 2026-07-12)
-------------------------------------------
``docs/design/stop-hook-completer.md`` rules this guard a COMPLETER: the parked
obligation — "advance the driver" — is *mechanical* (it is ``block-drive``), so
when the harness declares the ``stop-hook-append`` capability AND the next verb
is mechanical (a real chain block, not a recovery arm) AND transport is healthy
(the run's SSH breaker is CLOSED, no degraded signal), :func:`_completer_output`
runs the tick IN CODE — advancing self-heals the marker (no bounce), a re-park
at a new boundary bounces once carrying the fresh brief (render-a-proposal is
model judgment). The fork-exhaustion night (finding 20/21) is the counter-example
the transport gate exists for: a tripped breaker → the completer refuses and
degrades to :func:`_rejector_output` byte-for-byte (today's bounce). Absent/unknown
capability, a judgment verb, or degraded transport → the same byte-identical
bounce.

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

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from hpc_agent.cli.skill_returns import known_return_dirs

__all__ = ["build_hook_output", "find_committed_unadvanced", "main"]


def find_committed_unadvanced(experiment_dir: Path) -> dict[str, Any] | None:
    """First in-flight run under *experiment_dir* whose ``y`` is committed but
    the driver has not advanced, or ``None``.

    "Committed but unadvanced" = the run still carries a ``pending_decision``
    marker (:func:`hpc_agent.state.journal.is_awaiting_decision`) **and** the
    latest decision record is a greenlight that TARGETS this parked boundary —
    ``response == "y"`` whose ``resolved["next_block"]`` names the marker's
    ``resume_cursor["next_verb"]`` and whose ``ts`` is at/after the marker's
    ``awaiting_since`` (the shared :func:`block_drive.greenlight_targets_boundary`
    predicate). A marker with a trailing nudge, no decision yet, a PREVIOUS
    boundary's already-consumed ``y`` (bug-sweep #23), or a same-boundary re-park's
    stale ``y`` (run-12 finding 21) is the valid "waiting for the human" stop and
    yields ``None`` — the guard never forces a tick into a void (§5).

    The decision journal is read under the SCOPE the marker's workflow selects
    — mirroring how ``block_drive.run_tick`` locates the committed greenlight:
    a ``campaign`` chain journals its decisions under scope ``"campaign"``
    (keyed by the same id the marker is parked under), everything else under
    scope ``"run"``. Reading only the run scope would make the guard blind to
    every campaign-chain commit.

    Returns ``{"run_id", "block", "workflow"}`` for the first such run (block /
    workflow read from the marker; either may be ``None`` if the marker is
    partial). Filesystem / journal errors on any single run are swallowed — a
    run we cannot read is treated as not-pending.
    """
    from hpc_agent import errors
    from hpc_agent._kernel.lifecycle.block_drive import committed_greenlight_for_boundary
    from hpc_agent.state.decision_journal import read_decisions
    from hpc_agent.state.index import find_in_flight_runs
    from hpc_agent.state.journal import read_pending_decision

    try:
        records = find_in_flight_runs(experiment_dir)
    except OSError:
        return None

    for record in records:
        run_id = record.run_id
        try:
            marker = read_pending_decision(run_id, experiment_dir=experiment_dir)
            if not marker:
                continue
            scope_kind = "campaign" if marker.get("workflow") == "campaign" else "run"
            decisions = read_decisions(experiment_dir, scope_kind, run_id)
        except (OSError, ValueError, errors.SpecInvalid):
            continue
        if not decisions:
            continue
        # BOUNDARY-SCOPED (bug-sweep #23 / run-12 finding 21 / F13): fire only when the
        # greenlight that actually TARGETS this parked boundary is the newest decision
        # CONCERNING it — via the ONE shared scan ``block_drive.committed_greenlight_for_boundary``
        # the driver also keys on (previously the guard tested only ``decisions[-1]`` while
        # the driver scanned newest-first, so the two drifted). The scan scans newest-first
        # and stops at the first SAME-BOUNDARY record: a targeting ``y`` fires; a same-boundary
        # nudge journaled at/after the park is the human redrafting → silent; a prior
        # boundary's already-consumed ``y`` / a stale re-park ``y`` / an UNRELATED later record
        # (an overnight-consent, a sign-off, another block's touchpoint) is skipped, so it
        # neither fires the guard nor — the F13 fix — silences a genuine ``y`` behind it.
        cursor = marker.get("resume_cursor") or {}
        next_verb = cursor.get("next_verb") if isinstance(cursor, dict) else None
        marker_block = marker.get("block")
        if (
            committed_greenlight_for_boundary(
                decisions,
                block=marker_block if isinstance(marker_block, str) else None,
                next_verb=next_verb,
                awaiting_since=marker.get("awaiting_since"),
            )
            is None
        ):
            continue
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

    Otherwise routes the committed-unadvanced hit through :func:`_decide` — the
    REJECTOR bounce (the default, capability-dark)::

        {"decision": "block", "reason": "<invoke block-drive to advance>"}

    or, when the ``stop-hook-append`` capability is declared and the gates pass,
    the COMPLETER (runs the tick in code; a ``systemMessage`` audit note on a
    clean advance, a fresh-brief bounce on a new boundary — :func:`_completer_output`).
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
        return _decide(cand, hit)
    return None


def _rejector_output(hit: dict[str, Any]) -> dict[str, Any]:
    """Today's REJECTOR shape — the capability-absent (dark) default.

    Byte-identical to the pre-completer bounce: the model invokes ``block-drive``
    to advance the driver. This is what the completer degrades to wherever the
    ``stop-hook-append`` capability is absent, the next verb is a JUDGMENT verb,
    or transport is unhealthy (breaker open / degraded) — the fork-exhaustion
    night (finding 20/21) is exactly the last case.
    """
    run_id = hit["run_id"]
    block = hit.get("block") or "?"
    workflow = hit.get("workflow") or "?"
    reason = (
        f"approved spec committed for {run_id} block {block} — invoke "
        f"`hpc-agent block-drive --run-id {run_id} --workflow {workflow}` "
        "to advance the driver (do not end the turn)."
    )
    return {"decision": "block", "reason": reason}


def _next_verb_is_mechanical(next_verb: Any) -> bool:
    """True when *next_verb* is a deterministic block span code can run itself.

    The MECHANICAL / JUDGMENT boundary (docs/design/stop-hook-completer.md §3 +
    "The decision-rendezvous guard"): a real block verb in the ONE chaining SoT
    (:data:`hpc_agent.infra.block_chain.WORKFLOW_OF`) is a deterministic advance —
    running the tick is the omission "advance the driver". A recovery arm
    (``retarget-run`` — not a chain block), an anomaly resume, or an
    absent/unknown next verb is JUDGMENT (the model must author it) and stays a
    bounce. Fail-closed: any error → not mechanical (bias to the bounce).
    """
    if not isinstance(next_verb, str) or not next_verb:
        return False
    try:
        from hpc_agent.infra import block_chain

        return next_verb in block_chain.WORKFLOW_OF
    except Exception:
        return False


def _marker_next_verb(experiment_dir: Path, run_id: str) -> tuple[str | None, Any]:
    """The parked marker's ``(next_verb, awaiting_since)``, or ``(None, None)``.

    Read read-only from the ``pending_decision`` marker (the completer re-reads it
    rather than widening :func:`find_committed_unadvanced`'s pinned return dict).
    Fail-open: any error yields ``(None, None)`` — the completer then treats the
    verb as non-mechanical and degrades to the bounce.
    """
    try:
        from hpc_agent.state.journal import read_pending_decision

        marker = read_pending_decision(run_id, experiment_dir=experiment_dir)
    except Exception:
        return (None, None)
    if not isinstance(marker, dict):
        return (None, None)
    cursor = marker.get("resume_cursor") or {}
    next_verb = cursor.get("next_verb") if isinstance(cursor, dict) else None
    return (next_verb if isinstance(next_verb, str) else None, marker.get("awaiting_since"))


def _transport_healthy(experiment_dir: Path, run_id: str) -> bool:
    """True when the run's SSH circuit breaker is CLOSED (the ruling's gate).

    Running a tick opens an SSH volley; the fork-exhaustion night (finding 20/21)
    is the counter-example a blind completer would have AMPLIFIED. So the
    completer fires only when the run's host breaker reads ``"closed"`` via the
    ONE read-side predicate (:func:`hpc_agent.infra.ssh_circuit.effective_state` —
    a genuinely-cooling ``"open"`` or a ``"half_open_eligible"`` both refuse). A
    run that never tripped the breaker has no state file → ``effective_state``
    reads ``"closed"`` (fail-open healthy). Fail-CLOSED on any error reading the
    run record / host / breaker (bias to the bounce — the safe degrade).
    """
    try:
        from hpc_agent.state.journal import load_run

        record = load_run(experiment_dir, run_id)
        ssh_target = getattr(record, "ssh_target", None) if record is not None else None
    except Exception:
        return False
    if not isinstance(ssh_target, str) or not ssh_target:
        return False
    try:
        from hpc_agent.infra import ssh_circuit

        # Host normalization mirrors ssh_circuit._host (user@host / bare alias).
        host = ssh_target.rsplit("@", 1)[-1].strip()
        if not host:
            return False
        path = ssh_circuit.circuit_state_path(host)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
        if not isinstance(doc, dict):
            doc = None
        return ssh_circuit.effective_state(doc, now=time.time()) == "closed"
    except Exception:
        return False


def _run_drive_tick(experiment_dir: Path, run_id: str, workflow: str | None) -> tuple[Any, int]:
    """Run one ``block-drive`` tick in code — the completer's mechanical advance.

    A thin, monkeypatchable seam over
    :func:`hpc_agent._kernel.lifecycle.block_drive.run_tick` (tests substitute it
    to simulate advance / re-park without a live cluster). Returns the tick's
    ``(BlockDriveResult, exit_code)``.
    """
    from hpc_agent._kernel.lifecycle.block_drive import run_tick

    return run_tick(experiment_dir, run_id=run_id, workflow=workflow)


def _completer_output(experiment_dir: Path, hit: dict[str, Any]) -> dict[str, Any] | None:
    """The COMPLETER (RULED 2026-07-12): run the mechanical tick in code, no bounce.

    Gated three ways — the ``stop-hook-append`` capability, a MECHANICAL next verb,
    and a HEALTHY transport (breaker closed). Any gate failing degrades to
    :func:`_rejector_output` byte-for-byte (the fork-exhaustion night = the
    breaker-open case). All gates pass → run ``block-drive`` in code:

    * the tick ADVANCES (marker clears — self-heals, nothing to block): PROCEED
      with a code-appended ``systemMessage`` audit note (no bounce);
    * the tick re-parks at a NEW decision boundary (a fresh brief the human must
      answer — render-a-proposal is model judgment, §3): BOUNCE carrying the fresh
      brief so the model renders it (block-once, loop-safe);
    * the tick re-parks at the SAME boundary (the block did not advance —
      not_ready / a first-span failure re-parked verbatim): PROCEED (the model
      bounce buys nothing, finding 21; the scheduled ``doctor`` tick is the
      out-of-session backstop — the doc's "silence + doctor backstop" landing).

    Any exception running the tick → the bounce (invariant 4: degrade to the
    rejector, never wedge). The completer NEVER runs on a ``stop_hook_active``
    forced continuation (``build_hook_output`` returns early), so the bounce leg
    is block-once by construction.
    """
    run_id = hit["run_id"]
    workflow = hit.get("workflow")

    before_next_verb, before_awaiting = _marker_next_verb(experiment_dir, run_id)
    if not _next_verb_is_mechanical(before_next_verb):
        return _rejector_output(hit)  # judgment verb → the model must author it
    if not _transport_healthy(experiment_dir, run_id):
        return _rejector_output(hit)  # breaker open / degraded → never fire blindly

    try:
        result, _code = _run_drive_tick(experiment_dir, run_id, workflow)
    except Exception:
        return _rejector_output(hit)  # a broken tick degrades to the bounce

    after_next_verb, after_awaiting = _marker_next_verb(experiment_dir, run_id)
    action = getattr(result, "action", None)

    if after_next_verb is None:
        # Marker cleared — the driver advanced past the boundary (chained /
        # detached / terminal). Nothing left to block on.
        return {
            "systemMessage": (
                "hpc-agent decision-rendezvous completer — advanced the driver in "
                f"code (run {run_id}, action {action}; model-untouched). The parked "
                "greenlight was consumed; nothing to relay."
            )
        }

    if (after_next_verb, after_awaiting) != (before_next_verb, before_awaiting):
        # Re-parked at a NEW boundary: a genuinely new human decision. Rendering
        # its brief as a proposal is model judgment (§3) → bounce carrying it.
        brief = getattr(result, "brief", None)
        brief_txt = ""
        if isinstance(brief, dict) and brief:
            with contextlib.suppress(TypeError, ValueError):
                brief_txt = " Fresh brief: " + json.dumps(brief, sort_keys=True, default=str)
        return {
            "decision": "block",
            "reason": (
                f"advanced {run_id} in code to block {after_next_verb}, which parks "
                "for a fresh human greenlight — render its proposal for the human "
                f"(do not end the turn).{brief_txt}"
            ),
        }

    # Same boundary re-parked: the block did not advance (not_ready / first-span
    # failure). A model bounce buys nothing (finding 21); PROCEED — the breaker
    # (which will open under a real storm) and the scheduled doctor tick backstop.
    return {
        "systemMessage": (
            "hpc-agent decision-rendezvous completer — ran a tick in code for run "
            f"{run_id} (action {action}); the block did not advance (still parked). "
            "Backstop: the scheduled doctor tick."
        )
    }


def _decide(experiment_dir: Path, hit: dict[str, Any]) -> dict[str, Any] | None:
    """Route a committed-unadvanced *hit* to the completer or the rejector (D1).

    Capability-gated (docs/design/stop-hook-completer.md): with the
    ``stop-hook-append`` capability declared the completer runs the mechanical
    ``block-drive`` tick in code (gated further on a mechanical verb + healthy
    transport); absent/unknown — the default, since no harness declares it — it
    degrades to :func:`_rejector_output` byte-for-byte. Fail-open: any error
    reading the capability degrades to the rejector.
    """
    try:
        from hpc_agent.ops.harness_capabilities import detect_stop_hook_append

        completer_active = detect_stop_hook_append() is True
    except Exception:
        completer_active = False

    if completer_active:
        return _completer_output(experiment_dir, hit)
    return _rejector_output(hit)


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
