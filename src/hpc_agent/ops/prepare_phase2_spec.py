"""``prepare-phase2-spec`` primitive — derive the Phase-2 main-array spec (#279).

Two-phase canary gate (submit.md): Phase 1 submits ONLY the canary
(``canary_only: true``) and hands off; Phase 2 launches the main array
once ``verify-canary`` is green. The Phase-2 spec is the Phase-1 spec
with three deterministic flips and nothing else changed:

* ``canary: false``        — the canary already ran in Phase 1.
* ``canary_only: false``   — Phase 2 IS the main-array launch.
* ``skip_rsync_deploy: true`` — Phase 1 already rsync+deployed the tree
  to the same ``(ssh_target, remote_path)``; re-shipping it is wasted
  work (and the local tree hasn't changed between the two phases).

Before this primitive the worker round-tripped to *rebuild* a spec that
was 99% known the moment the canary handoff fired — re-resolving fields
it already had on the Phase-1 spec. This verb collapses that to a single
deterministic transform validated locally (the issue calls this "schema
validation"): no SSH, no journal reads, no cluster round-trip.

Pure function over the spec dict — validation constructs the
:class:`SubmitFlowSpec` model in-process and re-raises a pydantic
``ValidationError`` as :class:`errors.SpecInvalid` (mirroring how
``ops/submit_flow.py`` adapts ``ValueError`` → ``SpecInvalid`` so a bad
spec surfaces a typed envelope error rather than a stack trace).

INVARIANT (#279): the Phase-2 spec MUST be derivable from the Phase-1
spec with NO runtime state from canary execution — the three flips are
static, and every other field is copied verbatim. This is what lets the
worker skip the rebuild round-trip. If a future change makes any Phase-2
field depend on what the canary *did* at runtime (e.g. dynamic resource
adjustment off observed canary memory/walltime), this primitive's whole
premise breaks: the spec would no longer be knowable at handoff time and
this verb must NOT be used to derive it.

I/O contracts:

* Input: reuses ``hpc_agent/schemas/submit_flow.input.json`` (the
  Phase-1 ``SubmitFlowSpec`` shape) via the CLI ``schema_ref``.
* Output: a ``dict`` matching ``schemas/prepare_phase2_spec.output.json``.
"""

from __future__ import annotations

from typing import Any

import pydantic

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef

__all__ = ["prepare_phase2_spec"]

# The exact three flips that turn a Phase-1 submit-flow spec into the
# Phase-2 main-array spec (#279). Everything else is copied verbatim.
_PHASE2_FLIPS: dict[str, bool] = {
    "canary": False,
    "canary_only": False,
    "skip_rsync_deploy": True,
}


@primitive(
    name="prepare-phase2-spec",
    verb="validate",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help=(
            "Derive the Phase-2 main-array submit-flow spec from a Phase-1 "
            "two-phase-canary spec by flipping canary=false, canary_only=false, "
            "skip_rsync_deploy=true, validated locally against SubmitFlowSpec. "
            "Eliminates the spec-rebuild round-trip between verify-canary and "
            "the Phase-2 submit."
        ),
        verb="prepare-phase2-spec",
        spec_arg=True,
        spec_required=True,
        schema_ref=SchemaRef(input="submit_flow"),
    ),
    agent_facing=True,
)
def prepare_phase2_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Turn a Phase-1 submit-flow *spec* into the Phase-2 main-array spec.

    *spec* is a Phase-1 ``SubmitFlowSpec`` dict (the dispatcher loads
    ``--spec`` and validates it against ``submit_flow.input.json`` before
    calling this primitive). Applies exactly the three :data:`_PHASE2_FLIPS`
    — ``canary=false``, ``canary_only=false``, ``skip_rsync_deploy=true`` —
    and copies every other field verbatim, then validates the result by
    constructing :class:`SubmitFlowSpec`.

    Returns ``{phase2_spec, flips_applied}``:

    * ``phase2_spec`` — the derived main-array spec (a dict): *spec* with
      the three flips applied, everything else identical.
    * ``flips_applied`` — the three boolean flips, echoed back so the
      caller can audit exactly what changed.

    Raises :class:`errors.SpecInvalid` when the derived spec fails
    ``SubmitFlowSpec`` validation (e.g. the Phase-1 spec was missing a
    required field or carried ``total_tasks=0``) — a pydantic
    ``ValidationError`` is adapted to the typed envelope error, the same
    way ``ops/submit_flow.py`` adapts ``ValueError``.
    """
    phase2 = {**spec, **_PHASE2_FLIPS}

    try:
        # Local schema validation (#279): construct the model in-process.
        # Cheap — no SSH, no journal, no cluster round-trip.
        SubmitFlowSpec.model_validate(phase2)
    except pydantic.ValidationError as exc:
        raise errors.SpecInvalid(str(exc)) from exc

    return {
        "phase2_spec": phase2,
        "flips_applied": dict(_PHASE2_FLIPS),
    }
