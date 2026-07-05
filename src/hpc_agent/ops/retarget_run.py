"""``retarget-run`` — the cluster-retarget recovery arm, sequenced in code.

Proving-run #5 wave 5.2 (``docs/design/proving-run-5-hardening.md`` §3 wave 5.2,
§4.1). The block-drive anomaly terminators name recovery ACTIONS, but
cluster-retarget was the one action with no verb — so the agent freelanced ~5
steps (close out → re-resolve → re-mint → supersede → re-canary) and fumbled
three (proving run #4/#5, findings 9/10/13). This verb composes the pieces that
already exist into ONE journaled decision:

1. a fresh ``resolve`` under a NEW run_name + the NEW cluster — reusing
   ``revise-resolved``'s sidecar-reconstruction (:func:`_reconstruct_resolve_spec`,
   re-pointed not duplicated) with the run_name overridden, so ``job_env`` /
   activation / ``ssh_target`` / ``backend`` / the sidecar are all RE-DERIVED for
   the target cluster (the finding-13 class closed by construction);
2. ``supersede_run(old, new)`` — wave-2 supersession closes the failed attempt
   (and its ``-canary`` pairing), so the fresh run is not a scope-hop escape hatch
   (proving run #4, finding g/h);
3. a re-canary — ``submit-and-verify`` with ``stop_after_canary=True`` (the #160
   gate: submit the 1-task canary on the NEW cluster and verify it BEFORE the main
   array is ever offered).

**Why a NEW run_name (the design point).** A run_id keys on parameters +
run_name only (#207): a retarget keeps the SAME params (only the cluster moves),
so KEEPING the run_name would mint the IDENTICAL run_id on the new cluster and
layer-1 dedup would RE-ATTACH to the failed attempt instead of superseding it. So
this verb cannot simply call ``revise-resolved`` (which derives run_name from the
run_id and keeps it); it re-points ``revise-resolved``'s reconstruction helper
with a FRESH run_name (``<old_run_name>-<cluster>``, code-derived — the LLM never
authors it), giving a distinct run_id that wave-2 supersession can close cleanly.

**Ordering (resolve → supersede → re-canary).** Resolve runs FIRST: it keys its
own resume-vs-fresh detection on the NEW run_id (``_live_canary_attempt`` /
``find-prior-run``), so it never trips on the old attempt's still-live canary —
the retarget-under-a-live-canary case the design origin walked. Only then does
:func:`supersede_run` close the old attempt, so the re-canary's own supersession
gate finds no live same-identity sibling and passes without a ``supersedes``
field.

**It does NOT bypass the gates.** The re-canary is the #160 canary gate (cheap,
sandboxed); the returned brief carries ``needs_decision=True`` so the human
re-``y``s it through the EXISTING ``append-decision`` path (the authorship +
brief-provenance gates still run on the re-commit), and the MAIN array stays
behind the S3 greenlight gate.

**The load-bearing guard** (:func:`_assert_retarget_changes_cluster`): the patch
MUST name a cluster different from the failed attempt's. A same-cluster (or
clusterless) delta would mint a run_id that collides with the old attempt (a
self-supersession — closing the very run being re-launched), so it is refused
with a directive to use ``revise-resolved`` instead. The derived-field guard is
``revise-resolved``'s own (:func:`_assert_patch_targets_input_fields`, re-pointed)
— a ``patch`` key naming ``job_env`` / ``executor`` / ``ssh_target`` / … is
refused, exactly as for the nudge-as-delta verb.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.retarget_run import RetargetRunInput, RetargetRunResult
from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

# Re-point (never duplicate) the nudge-as-delta verb's helpers: the derived-field
# guard, the sidecar-reconstruction, and the base-resolved read are IDENTICAL to
# ``revise-resolved``'s — a retarget differs only in that it re-resolves under a
# FRESH run_name and then supersedes + re-canaries (proving-run-5 §4.1).
from hpc_agent.ops.revise_resolved import (
    _assert_patch_targets_input_fields,
    _latest_committed_resolved,
    _reconstruct_resolve_spec,
)
from hpc_agent.ops.submit_and_verify import submit_and_verify
from hpc_agent.ops.supersession import supersede_run

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["retarget_run"]


def _assert_retarget_changes_cluster(patch: dict[str, Any], *, prior_cluster: str) -> str:
    """The load-bearing guard: a retarget MUST move to a DIFFERENT cluster.

    Returns the new cluster name. Refuses (``errors.SpecInvalid``) a ``patch``
    that names no ``cluster``, or names the SAME cluster the failed attempt ran
    on. Both would derive a run_name that keeps the old run identity (or an
    unchanged one), minting a run_id that COLLIDES with the attempt being
    superseded — a self-supersession that closes the very run it re-launches
    (#207: a run_id keys on parameters + run_name only). A same-cluster nudge is
    a plain revision, so the message routes the caller to ``revise-resolved``.

    This is a guard that CAN fire (engineering-principles: "verify a guard can
    actually fire"): a same-cluster / clusterless retarget IS expressible in the
    input model, and this is the only place it is caught.
    """
    new_cluster = str(patch.get("cluster") or "").strip()
    prior = str(prior_cluster or "").strip()
    if not new_cluster:
        raise errors.SpecInvalid(
            "retarget-run: the patch names no `cluster` — a retarget re-derives "
            "job_env/ssh_target/backend/activation for a NEW cluster and supersedes "
            "the old attempt, so the delta MUST name the target cluster, e.g. "
            '{"cluster": "hoffman2"}. A non-cluster delta is a plain revision — use '
            "revise-resolved for that."
        )
    if new_cluster == prior:
        raise errors.SpecInvalid(
            f"retarget-run: the patch keeps the SAME cluster ({prior!r}) — a "
            "retarget must move to a DIFFERENT cluster (else the retargeted run_id "
            "collides with the attempt being superseded, a self-supersession; a "
            "run_id keys on parameters + run_name only, #207). To change resources "
            "on the SAME cluster, use revise-resolved instead."
        )
    return new_cluster


def _old_scheduler(experiment_dir: Path, *, old_run_id: str, cluster: str) -> str:
    """The scheduler family to close the OLD attempt's jobs with (best-effort).

    ``supersede_run`` needs a backend only when the old record has live job_ids to
    cancel (the live-canary retarget case). Prefer the old run's journaled
    ``RunRecord.backend``; fall back to the old cluster's ``clusters.yaml``
    scheduler; else an empty string (``supersede_run`` then records a
    pending-closure marker rather than cancelling — never blocks the retarget).
    """
    from hpc_agent.state.journal import load_run

    record = load_run(experiment_dir, old_run_id)
    if record is not None and record.backend:
        return str(record.backend)
    from hpc_agent.infra.clusters import load_clusters_config

    return str((load_clusters_config().get(cluster) or {}).get("scheduler") or "")


def _est_core_hours(submit: SubmitFlowSpec) -> float:
    """The pre-dispatch core-hours footprint for the retargeted spec (S2 parity).

    Mirrors ``submit_blocks._estimate_for_submit``: total_tasks × walltime × cores,
    via the single ``estimate_core_hours`` kernel. Defensive — a missing walltime
    reads as a zero footprint, never raises.
    """
    from hpc_agent.infra.cost import estimate_core_hours

    resources = submit.resources
    walltime_s = resources.walltime_sec if (resources and resources.walltime_sec) else 0
    cores = resources.cpus if (resources and resources.cpus) else None
    return estimate_core_hours(
        total_tasks=submit.total_tasks,
        walltime_s=walltime_s or 0,
        cores_per_task=cores,
    ).est_core_hours


@primitive(
    name="retarget-run",
    verb="workflow",
    composes=["resolve-submit-inputs", "submit-and-verify"],
    side_effects=[
        SideEffect(
            "writes-sidecar",
            "<experiment>/.hpc/runs/<new_run_id>.json (the retargeted sidecar)",
        ),
        SideEffect("scheduler-submit", "<new-cluster> (re-canary only)"),
        SideEffect("ssh", "<new-cluster> (re-canary poll) + <old-cluster> (supersession kill)"),
    ],
    # SiblingRunLive (from the re-canary's supersession gate) shares the
    # ``spec_invalid`` error_code, so SpecInvalid already covers it in the envelope.
    error_codes=[errors.SpecInvalid, errors.ClusterUnknown],
    idempotent=True,
    idempotency_key="old_run_id",
    cli=CliShape(
        help=(
            "Cluster-retarget recovery arm (proving-run-5 wave 5.2): re-resolve a "
            "failed attempt under a NEW run_name + the NEW cluster (re-deriving "
            "job_env/ssh_target/backend/activation), SUPERSEDE the old attempt, and "
            "RE-CANARY on the new cluster — one verb, one journaled decision. The "
            "patch must name a NEW cluster (a same-cluster delta → revise-resolved); "
            "a derived field is REFUSED. Returns the S2-shaped brief for the human's "
            "re-y — it does NOT bypass the append-decision / greenlight gates."
        ),
        spec_arg=True,
        spec_model=RetargetRunInput,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="retarget_run"),
    ),
    agent_facing=True,
)
def retarget_run(experiment_dir: Path, *, spec: RetargetRunInput) -> RetargetRunResult:
    """Supersede the failed attempt, re-resolve on a new cluster, and re-canary.

    1. **Guards** (load-bearing): the patch may target ONLY resolver-owned input
       fields (:func:`_assert_patch_targets_input_fields`) AND must name a NEW
       cluster (:func:`_assert_retarget_changes_cluster`).
    2. Read the failed attempt's sidecar for the run-owned resolve inputs; refuse
       a scope with no sidecar (there is no resolved prior to retarget).
    3. Fresh **resolve** under a NEW run_name (``<old_run_name>-<cluster>``, unless
       overridden) — re-derives ``job_env`` / activation / ``ssh_target`` /
       ``backend`` / the sidecar for the target cluster. Resolve keys its own
       resume detection on the NEW run_id, so it never trips on the old attempt's
       still-live canary. A non-``resolved`` outcome surfaces as ``resolve_blocked``.
    4. **Supersede** the old attempt (:func:`supersede_run`) — close it + its
       ``-canary`` pairing, stamp the old→new link — so the re-canary's own
       supersession gate finds no live same-identity sibling.
    5. **Re-canary** on the new cluster (``submit-and-verify`` with
       ``stop_after_canary=True``): the #160 gate verifies the 1-task canary before
       any main array. Return the S2-shaped brief with ``needs_decision=True`` — the
       human re-``y``s it through append-decision (this verb does NOT bypass the
       gates); the main array stays behind the S3 greenlight.

    Raises :class:`errors.SpecInvalid` on a derived-field / same-cluster patch, a
    scope with no resolved-run sidecar, or an unresolvable target cluster.
    """
    _assert_patch_targets_input_fields(spec.patch)

    from hpc_agent.state.runs import read_run_sidecar

    old_run_id = spec.old_run_id
    try:
        sidecar = read_run_sidecar(experiment_dir, old_run_id)
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(
            f"retarget-run: no resolved-run sidecar for old_run_id={old_run_id!r} — "
            "this verb retargets a RESOLVED prior (its per-run sidecar carries the "
            "run-owned resolve inputs to re-derive from). Resolve it first "
            "(submit-s1), then retarget. (A cluster retarget under a live canary IS "
            "a resolved prior — its sidecar exists.)"
        ) from exc

    prior_cluster = str(sidecar.get("cluster") or "").strip()
    new_cluster = _assert_retarget_changes_cluster(spec.patch, prior_cluster=prior_cluster)

    # The FRESH run_name — code-derived so the LLM never authors it. The cluster in
    # the name guarantees a run_id distinct from the (same-params) failed attempt.
    old_run_name = old_run_id.rsplit("-", 1)[0] or old_run_id
    new_run_name = (spec.new_run_name or f"{old_run_name}-{new_cluster}").strip()

    # 1. Fresh resolve under the NEW run_name — re-point revise-resolved's
    #    reconstruction (sidecar + patch → re-derived cluster-owned fields) and
    #    override ONLY the run_name. resolve-submit-inputs recomputes run_id /
    #    cmd_sha / job_env / EXECUTOR / the sidecar from the delta.
    resolve_spec = _reconstruct_resolve_spec(
        experiment_dir, run_id=old_run_id, sidecar=sidecar, patch=spec.patch
    ).model_copy(update={"run_name": new_run_name})
    rr = resolve_submit_inputs(experiment_dir, spec=resolve_spec)

    base_resolved = _latest_committed_resolved(experiment_dir, "run", old_run_id)
    resolved: dict[str, Any] = {k: v for k, v in base_resolved.items() if k != "next_block"}
    resolved.update(spec.patch)

    if rr.stage_reached != "resolved" or rr.submit_spec is None:
        # The fresh resolve surfaced its OWN decision (a live sibling of the NEW
        # run_id from a prior retarget, or a needed scaffold) — the old attempt has
        # NOT been superseded (we never got a clean run to supersede toward). Hand
        # the resolve brief back for the human to decide; do not re-canary.
        return RetargetRunResult(
            stage_reached="resolve_blocked",
            needs_decision=True,
            reason=(
                f"retarget re-resolve did not reach 'resolved' ({rr.stage_reached}): {rr.reason}"
            ),
            superseded_run_id=old_run_id,
            run_id=rr.run_id,
            verified=False,
            brief={
                "resolved": resolved,
                "resolve": {
                    "stage_reached": rr.stage_reached,
                    "reason": rr.reason,
                    "run_id": rr.run_id,
                    "prior_run_id": rr.prior_run_id,
                    "prior_status": rr.prior_status,
                    "prior_cluster": rr.prior_cluster,
                },
                "patch": dict(spec.patch),
            },
            applied_patch=dict(spec.patch),
        )

    new_run_id = rr.run_id or new_run_name
    if new_run_id == old_run_id:
        # Backstop for an explicit new_run_name that collapsed to the old identity —
        # superseding the old attempt would close the very run we are re-launching.
        raise errors.SpecInvalid(
            f"retarget-run: the retargeted run_id ({new_run_id!r}) equals the attempt "
            f"being superseded ({old_run_id!r}) — a retarget must change the run "
            "identity (new_run_name collided). Omit new_run_name (it is derived from "
            "the cluster) or pass a distinct one."
        )

    # 2. Supersede the old attempt — close it + its -canary pairing and stamp the
    #    old→new link, so the re-canary's supersession gate finds no live sibling.
    supersession = supersede_run(
        experiment_dir,
        old_run_id=old_run_id,
        new_run_id=new_run_id,
        scheduler=_old_scheduler(experiment_dir, old_run_id=old_run_id, cluster=prior_cluster),
    )

    # 3. Re-canary on the NEW cluster (the #160 gate: canary verified before main).
    submit = SubmitFlowSpec.model_validate(rr.submit_spec).model_copy(update={"canary": True})
    sv = submit_and_verify(
        experiment_dir, spec=SubmitAndVerifySpec(submit=submit), stop_after_canary=True
    )

    est_core_hours = _est_core_hours(submit)
    brief: dict[str, Any] = {
        # run_id + cluster ride the brief so a relay renders from the brief's OWN
        # data (design §5.3): the canonical line is "canary <state> on <cluster>".
        "run_id": sv.run_id,
        "cluster": new_cluster,
        "retargeted_from": {"run_id": old_run_id, "cluster": prior_cluster},
        "resolved": resolved,
        "canary_run_id": sv.canary_run_id,
        "canary_job_ids": sv.canary_job_ids,
        "verified": sv.verified,
        "failure_kind": sv.failure_kind,
        "est_core_hours": est_core_hours,
        "supersession": supersession,
        "resolve": {
            "run_id": rr.run_id,
            "cmd_sha": rr.cmd_sha,
            "submit_spec": rr.submit_spec,
            "sidecar_path": rr.sidecar_path,
        },
        "patch": dict(spec.patch),
    }
    if sv.verify_result is not None:
        brief["verify_result"] = sv.verify_result.model_dump(mode="json")

    if sv.verified:
        reason = (
            f"retargeted to {new_cluster!r}: canary green (est. {est_core_hours:g} "
            f"core-hours); superseded {old_run_id!r}. Greenlight to submit & watch on "
            f"{new_cluster}."
        )
        stage = "retargeted_canary_verified"
    else:
        reason = (
            f"retargeted to {new_cluster!r}: canary FAILED again "
            f"({sv.failure_kind or 'no canary'}); superseded {old_run_id!r}. Propose "
            "the next recovery."
        )
        stage = "retargeted_canary_failed"

    return RetargetRunResult(
        stage_reached=stage,  # type: ignore[arg-type]  # the two verified branches
        needs_decision=True,
        reason=reason,
        superseded_run_id=old_run_id,
        run_id=sv.run_id,
        verified=sv.verified,
        failure_kind=sv.failure_kind,
        brief=brief,
        applied_patch=dict(spec.patch),
    )
