"""Toy caller-side deployment gate — the refusal core does NOT own.

The registration kernel ships the mechanical answer (``verify-registration``
reports ``current | stale | revoked | superseded | absent``); the DEPLOY refusal
lives caller-side (``docs/design/registration-kernel.md`` R8: "core does not own
the deploy boundary"). This toy consumer wires that refusal on TWO legs:

* the EDIT-drift leg — ``verify-registration``'s ``status``, which recomputes the
  dossier / template / prerequisite legs at read time. That status is DELIBERATELY
  TIME-INDEPENDENT (R6 view_sha byte-identity: no ``now`` enters the signed
  projection), so it alone cannot see a lapsed review horizon.
* the HORIZON leg — the TIME-aware queue. A ``review_horizon`` that has lapsed
  since registration is a time-based staleness that only surfaces when a real
  ``now`` is threaded through ``reduce_registration``; the attention queue already
  does exactly that (live-conformance C-horizon; bug-sweep #48 arm (a) —
  RULING 2 2026-07-12: the time-aware queue owns the deployment gate's horizon
  leg). We consult the queue's ONE reduction rather than mint a second horizon
  evaluation.

The gate deploys ONLY when verify reads ``current`` AND the queue does not name
the registration horizon-lapsed; it refuses otherwise.

Toy-domain only — a widget batch. Never a real (harxhar/quant) strategy.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent._wire.actions.verify_registration import VerifyRegistrationSpec
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.attention_queue import horizon_lapsed_registration_ids
from hpc_agent.ops.registration.verify_op import verify_registration


def deploy_or_refuse(
    experiment_dir: Path, registration_id: str, *, now: str | None = None
) -> object:
    """Deploy the widget batch iff its registration is live, else refuse.

    Returns the verify-registration result on a clearance; raises ``SystemExit``
    (the caller's non-zero refusal) when verify reports a non-``current`` status
    (edit-drift / revoke / supersession) OR when the time-aware attention queue
    names this registration horizon-lapsed at *now* (a lapsed ``review_horizon``,
    invisible to verify's time-independent status). *now* defaults to the wall
    clock (``utcnow_iso``); tests thread a fixed timestamp (the ``doctor``
    deterministic-testing precedent). This is the whole deploy-boundary policy —
    core never grows a deploy/promote/go-live verb.
    """
    experiment_dir = Path(experiment_dir)
    result = verify_registration(
        experiment_dir=experiment_dir,
        spec=VerifyRegistrationSpec(registration_id=registration_id),
    )
    if result.status != "current":
        raise SystemExit(
            f"REFUSED: registration {registration_id!r} reads {result.status!r}, not 'current' "
            "— NOT deploying (re-register is the remedy for staleness)."
        )
    # The horizon leg: verify's status is time-independent by design, so a lapsed
    # review horizon can only be seen through the queue's now-threaded reduction.
    now = now if now is not None else utcnow_iso()
    if registration_id in horizon_lapsed_registration_ids(experiment_dir, now=now):
        raise SystemExit(
            f"REFUSED: registration {registration_id!r} reads 'current' but its review horizon "
            f"has lapsed as of {now} — NOT deploying (a dated re-affirmation is the remedy)."
        )
    return result
