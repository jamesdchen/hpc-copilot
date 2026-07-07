"""``reproduce-run`` — mint a pinned-identity reproduction, hand off to submit-s2.

The MINT half of the reproduction receipt (``docs/design/reproduction-receipt.md``
is the decision record). Given a FINISHED run, re-resolve it under a new run_name
against the SAME code + params + env, record the one-directional ``reproduces``
provenance link, and hand off to ``submit-s2`` via ``next_block`` — returning in
SECONDS. A later ``verify-reproduction`` compares the two runs' reduced metrics
under a caller-owned tolerance and writes the durable receipt.

Shape borrowed from ``ops/retarget_run.py`` (the non-blocking mint +
``next_block=submit-s2`` hand-off), with three deliberate departures the decision
record pins:

1. **Supersedes NOTHING.** A reproduction closes nothing — the original stays
   valid, the thing being reproduced. ``reproduces`` is a sidecar provenance
   back-link, not supersession; the supersession helper is never imported here
   (decision record, finding 2).
2. **The drift guard, both dimensions** (:func:`_assert_no_drift`). ``cmd_sha``
   is PARAMETER identity only (#207) — an executor-body edit keeps it — so a
   ``cmd_sha`` match alone would "reproduce" drifted code and call the mismatch a
   nondeterminism (finding 3). The guard refuses on param drift (current vs
   recorded ``cmd_sha``, naming both shas + the first differing task index from
   the sidecar's ``trial_params`` pre-image) AND on code drift
   (``state.code_drift.detect_code_drift`` over the recorded ``executor`` /
   ``tasks_py_sha`` vs current). A moved/edited tree REFUSES with the evidence;
   v1 never reconstructs-and-pretends.
3. **A disjoint remote_path** (:func:`_repro_remote_path`, finding 4). The
   reproduction resolves under ``<orig_remote_path>-repro`` — never nested under
   or a sibling within the original's tree — because the per-task fallback reduce
   scans ``remote_path`` recursively, so a shared subtree would blend the repro's
   rows into the original's future mean (the run-#6 11-row-mean class).

Like ``retarget-run`` this verb NEVER runs the re-canary inline: S2's
detach-by-contract worker owns the canary poll, so the verb returns in seconds
and is safe as a curated MCP tool. The re-canary's ``(cmd_sha, version,
cluster)``-validated-fresh skip is LEGITIMATE here (the tree is identical to the
original by construction) — the always-canary override is deliberately NOT set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.reproduce_run import ReproduceRunInput, ReproduceRunResult
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef

# compute-run-id materializes the CURRENT tree (cmd_sha + trial_params) for the
# param-drift dimension; compute_tasks_py_sha + the interview's materialized
# executor feed the code-drift predicate. Imported into THIS module's namespace
# (not called through the resolve seam) so the drift guard fires BEFORE any
# resolve work — and so a test can mock them at the reproduce boundary.
from hpc_agent.incorporation.build.compute_run_id import compute_run_id
from hpc_agent.ops.resolve_submit_inputs import (
    _materialized_executor_cmd,
    resolve_submit_inputs,
)

# Re-point (never duplicate) revise-resolved's reconstruction: the sidecar +
# (empty) patch → a ResolveSubmitInputsSpec with the run-owned inputs re-derived.
# A reproduction differs only in that it overrides run_name / remote_path / scopes
# and threads reproduction_of — it does NOT patch a cluster (same cluster) and it
# supersedes nothing.
from hpc_agent.ops.revise_resolved import (
    _latest_committed_resolved,
    _reconstruct_resolve_spec,
)
from hpc_agent.state.code_drift import detect_code_drift
from hpc_agent.state.run_sha import compute_tasks_py_sha

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.infra.cost import CostEstimate

__all__ = ["reproduce_run"]

# The code-derived reproduction suffix — appended to BOTH the run_name and the
# remote_path so the reproduction gets a distinct run_id AND a disjoint results
# subtree. The LLM never authors it.
_REPRO_SUFFIX = "-repro"


def _cmd_sha_matches(recorded: str, current: str) -> bool:
    """True when the current cmd_sha matches the recorded one (prefix-tolerant).

    A resolved sidecar stores the full 64-char ``cmd_sha``; a legacy record may
    carry the 8-char prefix. Treat a prefix match as identity so an old sidecar
    does not false-trip the param-drift guard.
    """
    if not recorded or not current:
        return False
    return current == recorded or current.startswith(recorded) or recorded.startswith(current)


def _first_differing_task(
    recorded: list[dict[str, Any]] | None, current: list[dict[str, Any]] | None
) -> int | None:
    """The first task index whose params differ between recorded and current.

    ``trial_params`` is the ``cmd_sha`` pre-image (one dict per task, task-ordered),
    so a cmd_sha mismatch has a concrete first differing task the human can look
    at. Returns the first index where the two lists disagree (a differing dict, or
    a length boundary), or ``None`` when there is no pre-image to compare (a legacy
    sidecar with no ``trial_params``).
    """
    if not recorded and not current:
        return None
    rec = recorded or []
    cur = current or []
    for i in range(min(len(rec), len(cur))):
        if rec[i] != cur[i]:
            return i
    if len(rec) != len(cur):
        return min(len(rec), len(cur))
    return None


def _repro_remote_path(original_remote: str) -> str:
    """Derive the reproduction's DISJOINT remote_path (``<orig>-repro``).

    The per-task fallback reduce scans ``record.remote_path`` RECURSIVELY for
    every ``metrics.json`` (``ops/aggregate_flow._per_task_metrics_reduce``), so a
    reproduction sharing the original's subtree would blend its rows into the
    original's future mean (the run-#6 11-row-mean contamination class). The
    ``-repro`` sibling root keeps each run's recursive scan seeing only its own
    rows.

    This asserts the derived path is genuinely disjoint from the original — it
    must not EQUAL the original, nor be a path-ancestor of it, nor be nested under
    it (finding 4). A guard that CAN fire: were the suffix convention ever changed
    to a nested path, the reduce-contamination it prevents would recur, so it
    refuses the framework bug here rather than at a corrupted harvest.
    """
    orig = str(original_remote or "").rstrip("/")
    if not orig:
        raise errors.SpecInvalid(
            "reproduce-run: the original run's sidecar carries no remote_path to "
            "derive a disjoint reproduction path from."
        )
    derived = orig + _REPRO_SUFFIX
    # Path-ancestor test: B is under A iff B == A or B starts with A + "/". The
    # ``-repro`` suffix makes ``/scratch/exp-repro`` a SIBLING of ``/scratch/exp``
    # (not under ``/scratch/exp/``), so neither is an ancestor of the other.
    if derived == orig or derived.startswith(orig + "/") or orig.startswith(derived + "/"):
        raise errors.SpecInvalid(
            f"reproduce-run: derived reproduction remote_path {derived!r} is not "
            f"disjoint from the original's {orig!r} — a shared subtree would let the "
            "recursive per-task reduce cross-contaminate the original's future mean."
        )
    return derived


def _assert_no_drift(
    experiment_dir: Path, *, original_run_id: str, sidecar: dict[str, Any]
) -> None:
    """Refuse to "reproduce" code that has DRIFTED since the original — both dims.

    The load-bearing guard (decision record, finding 3). A guard that CAN fire:
    an edited executor body or a changed swept param IS expressible in the current
    tree, and this is the only place it is caught before the mint.

    * **Param drift.** Compute the CURRENT tree's ``cmd_sha`` for the original's
      run_name and compare against the recorded one. A mismatch names BOTH shas +
      the first differing task index (from the sidecar's ``trial_params``
      pre-image), so the human sees exactly which task's params moved.
    * **Code drift.** ``cmd_sha`` is param identity only — an executor-body edit
      keeps it — so route :func:`detect_code_drift` over the recorded ``executor``
      / ``tasks_py_sha`` vs the current tree's. A drifted dimension refuses with
      the recorded value that changed (the evidence).
    """
    run_name = original_run_id.rsplit("-", 1)[0] or original_run_id

    # --- param drift: current cmd_sha vs recorded ---------------------------
    cr = compute_run_id(experiment_dir, run_name=run_name)
    current_cmd_sha = str(cr["cmd_sha"])
    recorded_cmd_sha = str(sidecar.get("cmd_sha") or "")
    if not _cmd_sha_matches(recorded_cmd_sha, current_cmd_sha):
        idx = _first_differing_task(sidecar.get("trial_params"), cr.get("trial_params"))
        where = (
            f"first differing task index {idx}"
            if idx is not None
            else "the differing task is unknown (original sidecar has no trial_params pre-image)"
        )
        raise errors.SpecInvalid(
            f"reproduce-run: the params of run_name {run_name!r} have DRIFTED since "
            f"{original_run_id!r} ran — recorded cmd_sha {recorded_cmd_sha!r} vs current "
            f"{current_cmd_sha!r} ({where}). cmd_sha is the parameter identity of the "
            "experiment; a re-run of a DIFFERENT parameter set is not a reproduction. "
            "Restore the original's task list, or submit the changed params as a new run."
        )

    # --- code drift: executor / tasks_py_sha (cmd_sha misses these) ---------
    drift = detect_code_drift(
        recorded_executor=sidecar.get("executor"),
        recorded_tasks_py_sha=sidecar.get("tasks_py_sha"),
        current_executor=_materialized_executor_cmd(experiment_dir),
        current_tasks_py_sha=compute_tasks_py_sha(experiment_dir / ".hpc" / "tasks.py"),
    )
    if drift.drifted:
        evidence: list[str] = []
        if drift.executor_changed:
            evidence.append(f"executor (recorded {drift.drifted_executor!r})")
        if drift.code_changed:
            recorded_sha = drift.drifted_tasks_py_sha
            evidence.append(f"tasks.py bytes (recorded tasks_py_sha {recorded_sha!r})")
        raise errors.SpecInvalid(
            f"reproduce-run: the CODE of {original_run_id!r} has DRIFTED since it ran "
            f"({'; '.join(evidence)}). cmd_sha is parameter identity only (#207) — it "
            "does not fold in the executor body or tasks.py bytes — so identical params "
            "over edited code is NOT a reproduction (it would call a code change a "
            "nondeterminism). Restore the original's code, or submit the edited code as "
            "a new run. v1 does not reconstruct a moved tree."
        )


def _cost_estimate(submit: SubmitFlowSpec) -> CostEstimate:
    """The pre-dispatch cost estimate for the reproduction spec (S2 parity).

    Mirrors ``retarget_run._cost_estimate`` / ``submit_blocks._estimate_for_submit``:
    total_tasks × walltime × cores via the single ``estimate_core_hours`` kernel.
    Defensive — a missing walltime yields the kernel's zero-cost estimate whose
    ``footprint_unknown`` renders as "unknown core-hours" instead of a false "0".
    """
    from hpc_agent.infra.cost import estimate_core_hours

    resources = submit.resources
    walltime_s = resources.walltime_sec if (resources and resources.walltime_sec) else 0
    cores = resources.cpus if (resources and resources.cpus) else None
    return estimate_core_hours(
        total_tasks=submit.total_tasks,
        walltime_s=walltime_s or 0,
        cores_per_task=cores,
    )


def _completed_prior_repro(
    experiment_dir: Path, *, derived_run_id: str, original_run_id: str
) -> Any | None:
    """The COMPLETE prior reproduction already recorded at *derived_run_id*, or None.

    The tree is identical by construction (the drift guard passed), so a re-run of
    ``reproduce-run`` mints the SAME derived run_id as a prior reproduction. The
    ``reproduction_of`` dedup lever makes ``find-prior-run`` SKIP prior
    reproductions of the original, so the resolve would silently re-attach — this
    surfaces the completed prior instead, so the human verifies it (or forces a
    fresh one via ``new_run_name``) rather than re-minting blindly.

    Only a ``complete`` prior with a matching ``reproduces`` link counts (an
    in-flight / failed attempt is not a finished reproduction to compare against).
    """
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import read_run_sidecar

    record = load_run(experiment_dir, derived_run_id)
    if record is None or record.status != "complete":
        return None
    try:
        repro_sidecar = read_run_sidecar(experiment_dir, derived_run_id)
    except FileNotFoundError:
        return None
    if repro_sidecar.get("reproduces") != original_run_id:
        return None
    return record


@primitive(
    name="reproduce-run",
    verb="workflow",
    composes=["resolve-submit-inputs"],
    side_effects=[
        SideEffect(
            "writes-sidecar",
            "<experiment>/.hpc/runs/<repro_run_id>.json (the reproduction sidecar)",
        ),
    ],
    # SiblingRunLive (from the fresh resolve's gates) shares the ``spec_invalid``
    # error_code, so SpecInvalid already covers it in the envelope.
    error_codes=[errors.SpecInvalid, errors.ClusterUnknown],
    idempotent=True,
    idempotency_key="original_run_id",
    cli=CliShape(
        help=(
            "Mint a pinned-identity REPRODUCTION of a finished run (reproduction "
            "receipt, task T5): re-resolve it under a NEW run_name "
            "(<orig>-repro) against the SAME code + params + env, under a DISJOINT "
            "remote_path, recording a one-directional `reproduces` provenance link "
            "(the original is NEVER superseded). Refuses if the tree has DRIFTED "
            "since the original ran — param drift (cmd_sha) OR code drift "
            "(executor/tasks_py_sha), naming the evidence. Returns in seconds with "
            "next_block=submit-s2: the human's re-y greenlights S2, whose DETACHED "
            "worker owns the re-canary poll. Never runs the canary inline. A later "
            "verify-reproduction compares the two runs' metrics + writes the receipt."
        ),
        spec_arg=True,
        spec_model=ReproduceRunInput,
        experiment_dir_arg=True,
        requires_ssh=False,
        schema_ref=SchemaRef(input="reproduce_run"),
    ),
    agent_facing=True,
)
def reproduce_run(experiment_dir: Path, *, spec: ReproduceRunInput) -> ReproduceRunResult:
    """Re-resolve a finished run against its pinned identity, hand off to S2.

    1. Read the original's sidecar (the run-owned resolve inputs); a scope with no
       sidecar is refused (there is no resolved prior to reproduce).
    2. **The drift guard** (:func:`_assert_no_drift`): refuse a reproduction of a
       tree whose params (cmd_sha) OR code (executor / tasks_py_sha) have drifted.
    3. Short-circuit ``prior_repro_exists`` when a COMPLETE reproduction already
       occupies the derived run_id — direct the human to verify-reproduction or an
       explicit new_run_name.
    4. Re-point revise-resolved's reconstruction (empty patch) under the derived
       ``<orig_run_name>-repro`` run_name, override the DISJOINT remote_path on
       BOTH the submit + sidecar specs, carry the original's scopes VERBATIM, and
       thread ``reproduction_of`` (so the resolve's find-prior-run pierces the
       same-params dedup and stamps ``reproduces`` on the sidecar).
    5. ``resolve-submit-inputs``: a non-``resolved`` outcome surfaces as
       ``resolve_blocked`` (supersedes NOTHING). The ``resolved`` terminal hands
       off to ``submit-s2`` (``next_block`` + ``needs_decision=True``); S2's
       DETACHED worker owns the re-canary poll, so this verb never blocks on it.

    Raises :class:`errors.SpecInvalid` on a scope with no sidecar, a drifted tree,
    or an unresolvable target cluster.
    """
    from hpc_agent.state.runs import read_run_sidecar

    original_run_id = spec.original_run_id
    try:
        sidecar = read_run_sidecar(experiment_dir, original_run_id)
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(
            f"reproduce-run: no resolved-run sidecar for original_run_id={original_run_id!r} "
            "— this verb reproduces a RESOLVED prior (its per-run sidecar carries the "
            "run-owned resolve inputs + the recorded identity the drift guard pins "
            "against). Resolve + run it first, then reproduce."
        ) from exc

    # 1. The drift guard — BEFORE any mint. Refuses on param OR code drift, naming
    #    the evidence. Also materializes the current tree (so tasks.py must exist).
    _assert_no_drift(experiment_dir, original_run_id=original_run_id, sidecar=sidecar)

    original_run_name = original_run_id.rsplit("-", 1)[0] or original_run_id
    repro_run_name = (spec.new_run_name or f"{original_run_name}{_REPRO_SUFFIX}").strip()

    # 2. prior_repro_exists: the drift guard just proved the tree is identical, so
    #    a re-run mints the SAME derived run_id as a prior reproduction. Recompute
    #    it (run_name + the current cmd_sha[:8]) and surface a COMPLETE prior rather
    #    than silently re-attaching (the reproduction_of lever would skip it).
    cr = compute_run_id(experiment_dir, run_name=repro_run_name)
    derived_run_id = str(cr["run_id"])
    prior = _completed_prior_repro(
        experiment_dir, derived_run_id=derived_run_id, original_run_id=original_run_id
    )
    if prior is not None:
        return ReproduceRunResult(
            stage_reached="prior_repro_exists",
            needs_decision=True,
            reason=(
                f"a COMPLETE reproduction of {original_run_id!r} already exists at "
                f"{derived_run_id!r} — compare it via `verify-reproduction "
                f"{{original_run_id: {original_run_id!r}, repro_run_id: {derived_run_id!r}}}`, "
                "or pass an explicit new_run_name to mint a FRESH reproduction."
            ),
            run_id=derived_run_id,
            reproduces=original_run_id,
            brief={
                "reproduces": original_run_id,
                "existing_repro_run_id": derived_run_id,
                "verify_hint": {
                    "verb": "verify-reproduction",
                    "original_run_id": original_run_id,
                    "repro_run_id": derived_run_id,
                },
            },
            next_block=None,
        )

    # 3. Re-point revise-resolved's reconstruction (empty patch — same cluster) and
    #    override: run_name → the derived <orig>-repro; remote_path → the DISJOINT
    #    <orig_remote_path>-repro on BOTH submit + sidecar (finding 4); scopes →
    #    the original's VERBATIM (the scope gate composes on the repro, finding 7);
    #    reproduction_of=original so the resolve's find-prior-run pierces the
    #    same-params dedup and stamps ``reproduces`` on the sidecar.
    repro_remote_path = _repro_remote_path(str(sidecar.get("remote_path") or ""))
    base_spec = _reconstruct_resolve_spec(
        experiment_dir, run_id=original_run_id, sidecar=sidecar, patch={}
    )
    original_scopes = sidecar.get("scopes")
    resolve_spec = base_spec.model_copy(
        update={
            "run_name": repro_run_name,
            "reproduction_of": original_run_id,
            "submit": base_spec.submit.model_copy(update={"remote_path": repro_remote_path}),
            "sidecar": base_spec.sidecar.model_copy(
                update={
                    "remote_path": repro_remote_path,
                    "scopes": original_scopes,
                    "reproduces": original_run_id,
                }
            ),
        }
    )
    rr = resolve_submit_inputs(experiment_dir, spec=resolve_spec)

    base_resolved = _latest_committed_resolved(experiment_dir, "run", original_run_id)
    resolved: dict[str, Any] = {k: v for k, v in base_resolved.items() if k != "next_block"}

    if rr.stage_reached != "resolved" or rr.submit_spec is None:
        # The fresh resolve surfaced its OWN decision (an UNRELATED live same-params
        # prior, or a needed scaffold). Nothing was minted, NOTHING superseded —
        # hand the resolve brief back for the human to decide; do not canary.
        return ReproduceRunResult(
            stage_reached="resolve_blocked",
            needs_decision=True,
            reason=(
                f"reproduction re-resolve did not reach 'resolved' ({rr.stage_reached}): "
                f"{rr.reason}"
            ),
            run_id=rr.run_id,
            reproduces=original_run_id,
            brief={
                "reproduces": original_run_id,
                "resolved": resolved,
                "resolve": {
                    "stage_reached": rr.stage_reached,
                    "reason": rr.reason,
                    "run_id": rr.run_id,
                    "prior_run_id": rr.prior_run_id,
                    "prior_status": rr.prior_status,
                    "prior_cluster": rr.prior_cluster,
                },
            },
            next_block=None,
        )

    # 4. Hand the re-canary to submit-s2 (detach-by-contract). This verb finishes
    #    in seconds — the #160 canary gate runs in S2's DETACHED worker after the
    #    human's re-y, NEVER inline here (the non-blocking contract that makes it
    #    MCP-safe). The reproduction's (cmd_sha, version, cluster)-validated-fresh
    #    canary SKIP is legitimate (the tree is identical to the original by
    #    construction), so the always-canary override is NOT set. The canary + S3
    #    greenlight gates still stand, owned by submit-s2/-s3.
    submit = SubmitFlowSpec.model_validate(rr.submit_spec)
    est = _cost_estimate(submit)
    # The journaled greenlight must name submit-s2 (assert_greenlit_target reads
    # the resolved's next_block), mirroring what S1's resolved brief carries.
    resolved["next_block"] = "submit-s2"
    est_phrase = (
        "unknown core-hours (walltime unresolved — no history)"
        if est.footprint_unknown
        else f"{est.est_core_hours:g} core-hours"
    )
    brief: dict[str, Any] = {
        "run_id": rr.run_id,
        "reproduces": original_run_id,
        "cluster": sidecar.get("cluster"),
        "remote_path": repro_remote_path,
        "resolved": resolved,
        "est_core_hours": est.est_core_hours,
        # Unknown-footprint honesty (run #6): the kernel's defensive 0.0 must never
        # render as a literal "0 core-hours" — the relay reads off the brief dict.
        "footprint_unknown": est.footprint_unknown,
        "resolve": {
            "run_id": rr.run_id,
            "cmd_sha": rr.cmd_sha,
            "submit_spec": rr.submit_spec,
            "sidecar_path": rr.sidecar_path,
        },
    }
    return ReproduceRunResult(
        stage_reached="repro_pending_canary",
        needs_decision=True,
        reason=(
            f"reproduction of {original_run_id!r} resolved (est. {est_phrase}) under a "
            f"disjoint remote_path; canary PENDING — greenlight submit-s2 to stage & "
            "canary the reproduction (its detached worker owns the poll)."
        ),
        run_id=rr.run_id,
        reproduces=original_run_id,
        brief=brief,
        next_block={
            "verb": "submit-s2",
            "why": "reproduction resolved; stage & canary the reproduction for review.",
            "spec_hint": {"run_id": rr.run_id},
        },
    )
