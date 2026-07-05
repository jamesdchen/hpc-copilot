"""``submit-speculate`` — the speculative canary (human-amplification-blocks §3).

An opt-in, THIN surface that runs S2's canary *early*, while the human reviews
the S1 brief. It composes the SAME primitive ``submit-s2`` composes
(:func:`hpc_agent.ops.submit_and_verify.submit_and_verify` with
``stop_after_canary=True``) over the recommended-defaults submit spec, so a plain
``y`` on the brief finds the canary already done and a nudge that changes the
spec re-canaries.

Budget = 1 per pending brief, and nudge-invalidation, both come **for free** from
the ``(cmd_sha, version)`` canary TTL cache — no extra machinery is built:

* ``verify-canary`` records ``(cmd_sha, version)`` as validated on a green canary;
  ``submit-flow``'s Phase-1 skip reads the same key. So once a speculative canary
  is validated-fresh, both a repeat ``submit-speculate`` (refused here) AND S2's
  own submit-flow skip see it and do not re-fire — the cache is the dedup.
* ``cmd_sha`` is parameter identity (``HPC_CMD_SHA``). A nudge that changes the
  spec moves ``cmd_sha`` → the stale entry no longer matches → the next canary is
  fresh; an unchanged spec keeps the same ``cmd_sha`` → S2 finds it fresh and
  skips. Nudge-invalidation is therefore a property of the existing key, not new
  code.

Never cancels anything (§3): a superseded canary drains naturally; there is no
kill path here. Pre-greenlight only ever puts the single-task canary in the queue
— the main array is unreachable from this verb (``stop_after_canary``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.submit_speculate import (
    SubmitSpeculateResult,
    SubmitSpeculateSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.submit_and_verify import submit_and_verify

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["submit_speculate"]


def _canary_key(base_job_env: dict[str, str] | None, *, cluster: str) -> str | None:
    """Return the ``(cmd_sha, framework-version, cluster)`` cache key, or None.

    None when the cache is disabled or the spec carries no ``HPC_CMD_SHA`` — both
    mean "cannot dedup on the cache", so speculation proceeds (the canary is
    bounded).
    """
    from hpc_agent import __version__ as _pkg_version
    from hpc_agent.state import canary_cache

    if canary_cache.cache_disabled():
        return None
    cmd_sha = (base_job_env or {}).get("HPC_CMD_SHA") or ""
    if not cmd_sha:
        return None
    return canary_cache.canary_cache_key(
        cmd_sha=cmd_sha, version=_pkg_version or "", cluster=cluster
    )


@primitive(
    name="submit-speculate",
    verb="workflow",
    composes=["submit-and-verify"],
    side_effects=[
        SideEffect("scheduler-submit", "<cluster> (speculative canary only)"),
        SideEffect("ssh", "<cluster> (canary poll + log scan)"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="submit.submit.run_id",
    cli=CliShape(
        help=(
            "Speculative canary (design §3): run S2's canary EARLY, under the S1 "
            "recommended defaults, while the human reviews the brief. Composes "
            "submit-and-verify(stop_after_canary=True) — only the 1-task canary "
            "enters the queue; the main array never launches. Budget = 1 per brief "
            "and nudge-invalidation come free from the (cmd_sha, version) canary "
            "TTL cache: a validated-fresh canary is refused (S2 reuses it). Never "
            "cancels anything."
        ),
        spec_arg=True,
        spec_model=SubmitSpeculateSpec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="submit_speculate"),
    ),
    agent_facing=True,
)
def submit_speculate(experiment_dir: Path, *, spec: SubmitSpeculateSpec) -> SubmitSpeculateResult:
    """Run the speculative canary, or no-op when one is already validated-fresh.

    The budget (§3: at most one speculative canary per pending brief) is enforced
    purely by the ``(cmd_sha, version)`` TTL cache: a validated-fresh canary means
    a prior speculation (or an S2 run) already covered this exact spec, so this
    call refuses with ``speculated=False`` rather than firing a redundant canary.
    Otherwise it runs ``submit-and-verify(stop_after_canary=True)`` — the same
    canary path S2 runs — and reports the outcome. It never launches the main
    array and never cancels anything.
    """
    base = spec.submit.submit
    key = _canary_key(base.job_env, cluster=base.cluster)
    if key is not None:
        from hpc_agent.state import canary_cache

        if canary_cache.is_canary_validated_fresh(key):
            return SubmitSpeculateResult(
                run_id=base.run_id,
                speculated=False,
                verified=True,
                reason=(
                    "a canary for this (cmd_sha, version) is already validated-fresh; "
                    "S2 will reuse it — no speculative canary fired (budget = 1 per brief)."
                ),
            )

    # Detach-by-contract (design §3): the budget/dedup check above ran
    # synchronously (a validated-fresh canary is the no-op returned above); a
    # FRESH canary would now fire, so with detach ON (default) hand it to a
    # durable background worker and return immediately — speculation must never
    # hold the chat while the human reviews the S1 brief. ``verified`` is unknown
    # until the worker's poll lands in the journal.
    if spec.detach:
        from hpc_agent._kernel.lifecycle.detached import launch_submit_block_detached

        dumped = spec.model_copy(update={"detach": False}).model_dump(mode="json")
        launch = launch_submit_block_detached(
            verb="submit-speculate",
            experiment_dir=str(experiment_dir),
            spec=dumped,
        )
        return SubmitSpeculateResult(
            run_id=base.run_id,
            speculated=True,
            verified=False,
            started=True,
            watch="journal",
            detached_pid=launch.pid,
            reason=(
                "speculative canary detached — it polls in a durable background "
                "worker; read the outcome from the journal. A plain `y` on the S1 "
                "brief finds S2 reusing a green canary."
            ),
        )

    sv = submit_and_verify(experiment_dir, spec=spec.submit, stop_after_canary=True)
    return SubmitSpeculateResult(
        run_id=sv.run_id,
        speculated=True,
        verified=sv.verified,
        reason=(
            "speculative canary verified; a plain `y` on the S1 brief finds S2 already done."
            if sv.verified
            else f"speculative canary failed ({sv.failure_kind}); the S1 brief still governs."
        ),
        canary_run_id=sv.canary_run_id,
        canary_job_ids=sv.canary_job_ids,
        failure_kind=sv.failure_kind,
    )
