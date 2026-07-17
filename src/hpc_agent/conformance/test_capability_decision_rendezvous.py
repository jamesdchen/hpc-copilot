"""Conformance kit — capability 7 (decision-rendezvous commit-then-continue).

Asserts a harness's turn-final seam (``run_decision_rendezvous``) FORCES exactly
one continuation when a human greenlight (``response == "y"``) is committed to the
decision journal for a parked block boundary but the ``block-drive`` driver has NOT
advanced past it — and stays SILENT while the driver is merely awaiting the human
(no committed ``y`` targeting the boundary), never forcing a continuation into a
void. It is loop-safe: a ``stop_hook_active`` re-entry (``previously_blocked``)
never forces a second time. This is contract capability 7
(``docs/internals/harness-contract.md``, "Capability 7 — the decision-rendezvous
commit-then-continue").

The seam is outcome-shaped (``EnforcementOutcome``: ``blocked`` = "forced a
continuation" + a ``reason``), never mechanism-shaped — a Stop hook and any other
turn-final interceptor certify through the same seam (the D-K3 outcome-not-
mechanism rule).

Standalone / reference (the K2 pattern): with no ``--harness-adapter`` — OR an
adapter that does not declare capability 7 (a FOREIGN proof is owed, Wave C) — the
built-in REFERENCE guard, hpc-agent's own
``decision_rendezvous_stop_guard.build_hook_output`` core driven IN-PROCESS over a
seeded journal, is the candidate (the behaved-for-the-reference-adapter leg). When
an adapter DECLARES capability 7, its ``run_decision_rendezvous`` is the candidate.
It never SKIPs: capability 7 is not part of the three-capability ``conforming:
harness contract v1`` verdict, so the module always certifies the reference core as
the baseline.

The journal state is seeded through the ordinary state API (``mark_pending_decision``
+ ``append_decision``) exactly as a live ``block-drive`` park does — the kit sets up
committed-unadvanced vs merely-awaiting and drives the candidate over each.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from hpc_agent.conformance.adapter import (
    CAP_DECISION_RENDEZVOUS,
    EnforcementOutcome,
    declared_capabilities,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from hpc_agent.state.run_record import RunRecord

_RUN_ID = "kit-rendezvous-run"
_BLOCK = "s2"
_WORKFLOW = "submit"
# A NON-mechanical next verb (not a real ``block_chain`` block) so the reference
# guard takes the REJECTOR (block-once bounce) path deterministically, regardless
# of whether the ``stop-hook-append`` completer capability is active in the
# ambient env — the force-continuation outcome is what capability 7 asserts.
_NEXT_VERB = "s3"
_BRIEF = {"proposal": "canary looks good", "cost": 42}


# --- journal seeding (a live block-drive park, reproduced) -------------------


def _record(run_id: str) -> RunRecord:
    from hpc_agent.state.run_record import RunRecord

    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["100"],
        total_tasks=4,
        submitted_at="2026-07-03T00:00:00+00:00",
        experiment_dir="/exp",
        status="in_flight",
    )


def seed_park(experiment_dir: Path, run_id: str = _RUN_ID) -> None:
    """Upsert an in-flight run and stamp its ``pending_decision`` marker + brief."""
    from hpc_agent.state.journal import mark_pending_decision, upsert_run

    upsert_run(experiment_dir, _record(run_id))
    mark_pending_decision(
        run_id,
        block=_BLOCK,
        workflow=_WORKFLOW,
        brief=_BRIEF,
        resume_cursor={
            "workflow": _WORKFLOW,
            "run_id": run_id,
            "next_verb": _NEXT_VERB,
            "current_verb": _BLOCK,
        },
        awaiting_since="2026-07-03T00:30:00+00:00",
        experiment_dir=experiment_dir,
    )


def commit_greenlight(experiment_dir: Path, run_id: str = _RUN_ID) -> None:
    """Commit a ``y`` that TARGETS the parked boundary (``next_block`` names the
    marker's ``next_verb``; the auto-stamped ``ts`` is after ``awaiting_since``)."""
    from hpc_agent.state.decision_journal import append_decision

    append_decision(
        experiment_dir,
        scope_kind="run",
        scope_id=run_id,
        block=_BLOCK,
        response="y",
        resolved={"approved": True, "next_block": _NEXT_VERB},
    )


def commit_nudge(experiment_dir: Path, run_id: str = _RUN_ID) -> None:
    """Commit a NUDGE (not a greenlight) — still awaiting the human."""
    from hpc_agent.state.decision_journal import append_decision

    append_decision(
        experiment_dir,
        scope_kind="run",
        scope_id=run_id,
        block=_BLOCK,
        response="cap the cost at 10",
    )


# --- the rendezvous candidate seam -------------------------------------------


@dataclass(frozen=True)
class RendezvousCandidate:
    """A turn-final rendezvous seam under test — the reference core or an adapter."""

    name: str
    run: Callable[..., EnforcementOutcome]


def _builtin_reference() -> RendezvousCandidate:
    """hpc-agent's own rendezvous core driven in-process (the reference provider).

    Builds the ``Stop`` payload (``stop_hook_active`` models the re-entry) and maps
    ``decision_rendezvous_stop_guard.build_hook_output`` onto an
    ``EnforcementOutcome``: a ``{"decision": "block", ...}`` is a FORCED continuation
    (``blocked=True``, ``reason``); ``None`` or a non-blocking (proceed /
    ``systemMessage``) output is a PASS.
    """
    from hpc_agent._kernel.hooks.decision_rendezvous_stop_guard import build_hook_output

    def run(experiment_dir: Path, *, previously_blocked: bool = False) -> EnforcementOutcome:
        payload = {
            "hook_event_name": "Stop",
            "cwd": str(experiment_dir),
            "stop_hook_active": previously_blocked,
        }
        out = build_hook_output(payload)
        forced = isinstance(out, dict) and out.get("decision") == "block"
        reason = out.get("reason") if isinstance(out, dict) else None
        return EnforcementOutcome(blocked=bool(forced), reason=reason if forced else None)

    return RendezvousCandidate(name="hpc-agent (decision_rendezvous_stop_guard)", run=run)


@pytest.fixture
def decision_rendezvous_candidate(request: pytest.FixtureRequest) -> RendezvousCandidate:
    """The rendezvous seam to certify — the adapter's when declared, else reference.

    With ``--harness-adapter`` AND a declared capability 7, the adapter's
    ``run_decision_rendezvous`` is the candidate. Otherwise the built-in reference
    core runs (no SKIP — capability 7 is not a ``conforming: harness contract v1``
    verdict capability; a FOREIGN proof is the Wave-C follow-on).
    """
    spec = request.config.getoption("--harness-adapter", default=None)
    if spec:
        adapter = request.getfixturevalue("harness_adapter")
        if CAP_DECISION_RENDEZVOUS in declared_capabilities(adapter):
            return RendezvousCandidate(
                name=getattr(adapter, "name", "<adapter>"), run=adapter.run_decision_rendezvous
            )
    return _builtin_reference()


# --- assertions (mirror-drivable: first arg is the candidate, second the repo) --
#
# Each check seeds a FRESH state into its own repo, so callers must pass a distinct
# claimed repo per check (the pytest ``fixture_repo`` is per-test; the mirror
# claims one per call).


def check_forces_on_committed_unadvanced(candidate: RendezvousCandidate, repo: Path) -> None:
    """A committed ``y`` the driver has not consumed FORCES exactly one continuation."""
    seed_park(repo)
    commit_greenlight(repo)
    outcome = candidate.run(repo)
    assert outcome.blocked is True, (
        f"[{candidate.name}] a committed greenlight the driver has not advanced past "
        "MUST force a continuation (advance the driver), not end the turn"
    )
    assert outcome.reason, f"[{candidate.name}]: a forced continuation must carry a reason"


def check_silent_while_merely_awaiting(candidate: RendezvousCandidate, repo: Path) -> None:
    """Parked with NO committed ``y`` (and with a trailing nudge) → SILENT.

    The §5 subtlety: forcing a continuation while merely awaiting the human loops
    the harness into a void. A rendezvous that FIRES here is FAILED (guard-can-fire).
    """
    seed_park(repo)  # parked, nothing committed yet
    assert candidate.run(repo).blocked is False, (
        f"[{candidate.name}] parked-but-awaiting (no committed y) MUST stay silent — "
        "forcing a continuation into a void is the failure this closes"
    )
    commit_nudge(repo)  # the human nudged — still awaiting a fresh y
    assert candidate.run(repo).blocked is False, (
        f"[{candidate.name}] a trailing nudge (latest decision is not a y) MUST stay silent"
    )


def check_loop_safe_reentry(candidate: RendezvousCandidate, repo: Path) -> None:
    """``previously_blocked=True`` never forces again — block at most once.

    Even with a committed-unadvanced greenlight present (which forces on the first
    pass), a re-entry that is already a forced continuation must pass through.
    """
    seed_park(repo)
    commit_greenlight(repo)
    outcome = candidate.run(repo, previously_blocked=True)
    assert outcome.blocked is False, (
        f"[{candidate.name}] a stop that is already a hook-forced continuation MUST "
        "NOT be forced again (loop-safe re-entry)"
    )


def test_rendezvous_forces_on_committed_unadvanced(
    decision_rendezvous_candidate: RendezvousCandidate, fixture_repo: Path
) -> None:
    """Capability 7 behaved leg: a committed-unadvanced greenlight forces one continue."""
    check_forces_on_committed_unadvanced(decision_rendezvous_candidate, fixture_repo)


def test_rendezvous_silent_while_merely_awaiting(
    decision_rendezvous_candidate: RendezvousCandidate, fixture_repo: Path
) -> None:
    """Capability 7 behaved leg: merely-awaiting (and a trailing nudge) stays silent."""
    check_silent_while_merely_awaiting(decision_rendezvous_candidate, fixture_repo)


def test_rendezvous_loop_safe_reentry(
    decision_rendezvous_candidate: RendezvousCandidate, fixture_repo: Path
) -> None:
    """Capability 7 behaved leg: a ``stop_hook_active`` re-entry never forces twice."""
    check_loop_safe_reentry(decision_rendezvous_candidate, fixture_repo)
