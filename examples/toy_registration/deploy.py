"""Toy caller-side deployment gate — the ~10-line refusal core does NOT own.

The registration kernel ships the mechanical answer (``verify-registration``
reports ``current | stale | revoked | superseded | absent``); the DEPLOY refusal
lives caller-side (``docs/design/registration-kernel.md`` R8: "core does not own
the deploy boundary"). This toy consumer wires that refusal: it deploys ONLY when
the registration reads ``current``, and refuses on anything else.

Toy-domain only — a widget batch. Never a real (harxhar/quant) strategy.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent._wire.actions.verify_registration import VerifyRegistrationSpec
from hpc_agent.ops.registration.verify_op import verify_registration


def deploy_or_refuse(experiment_dir: Path, registration_id: str) -> object:
    """Deploy the widget batch iff its registration reads ``current``, else refuse.

    Returns the verify-registration result on a clearance; raises ``SystemExit``
    (the caller's non-zero refusal) on any non-``current`` status. This is the
    whole deploy-boundary policy — core never grows a deploy/promote/go-live verb.
    """
    result = verify_registration(
        experiment_dir=Path(experiment_dir),
        spec=VerifyRegistrationSpec(registration_id=registration_id),
    )
    if result.status != "current":
        raise SystemExit(
            f"REFUSED: registration {registration_id!r} reads {result.status!r}, not 'current' "
            "— NOT deploying (re-register is the remedy for staleness)."
        )
    return result
