"""``resolve-submit-inputs``: the deterministic submit input-resolution chain in one call.

Folds ``worker_prompts/submit.md`` Steps 6a-6d — scaffold-or-reuse
``.hpc/tasks.py``, compute the run_id, detect a resumable prior run, then
assemble the validated submit-flow spec — into ONE workflow primitive.
Those steps are mechanical: each is a verb call followed by a deterministic
branch on its result. ``resolve-submit-inputs`` runs the branches in code
and returns a single typed ``stage_reached`` outcome, so the agent stops
hand-walking (and hand-branching) the verbs.

This is the control-flow-out-of-the-LLM pattern ``submit-pipeline`` started,
applied one ring *earlier*: where ``submit-pipeline`` absorbs the
post-resolution submit spine, ``resolve-submit-inputs`` absorbs the
input-resolution spine that runs entirely on the laptop (no cluster, no SSH)
to produce a ready-to-submit context.

Composition:

    compute-run-id  →  find-prior-run  →  (build-tasks-py if tasks.py absent)
                    →  build-submit-spec  →  write-run-sidecar

The ``resolved`` terminal is fully submit-ready: the submit-flow spec is built
AND the per-run sidecar is written (the #171 write-first precondition), so the
caller hands ``submit_spec`` straight to ``submit-pipeline`` with no
intervening deterministic step.

The genuine JUDGEMENT that precedes this spine stays UPSTREAM as escalations
— parsing the user's natural-language intent (Step 2), classifying the
data-axis when unresolved (Step 3), and env selection (Step 4). This
composite is what runs once those are resolved.

Escalation-as-data (#231): only two outcomes set ``needs_decision=True`` —
a live prior run was found (only the user picks resume-vs-fresh) or
``.hpc/tasks.py`` is absent and no deterministic scaffold spec was supplied
(the scaffold sub-interview the headless worker can't run). The clean
``resolved`` terminal hands back the built submit spec and needs no decision.

**Additive.** Does not replace the per-verb worker-prompt path — it is a new
verb the prompt may adopt. Nothing breaks if it is not yet wired in.

I/O contracts:

* Input: ``schemas/resolve_submit_inputs.input.json`` (from ``ResolveSubmitInputsSpec``).
* Output: ``schemas/resolve_submit_inputs.output.json`` (from ``ResolveSubmitInputsResult``).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.resolve_submit_inputs import (
    ResolveSubmitInputsResult,
    ResolveSubmitInputsSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.cli.setup_actions import find_prior_run
from hpc_agent.incorporation.build.compute_run_id import compute_run_id
from hpc_agent.incorporation.build.submit_spec import build_submit_spec
from hpc_agent.incorporation.build.tasks_py import build_tasks_py
from hpc_agent.ops.write_run_sidecar import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["resolve_submit_inputs"]

# find-prior-run statuses that count as a LIVE prior (#276): a record the
# caller must NOT submit over without a user resume-vs-fresh decision. A
# terminal-but-not-complete record (failed / abandoned) is forensic, not
# live — the chain proceeds as fresh and re-submits over it (mirrors
# submit.md Step 6c branching).
_LIVE_PRIOR_STATUSES: frozenset[str] = frozenset({"complete", "in_flight"})


def _materialized_executor_cmd(experiment_dir: Path) -> str | None:
    """The per-task executor the interview materialized, if any — read in CODE.

    The interview persists ``_materialized.entry_point.executor_cmd`` for every
    entry kind that has a deterministic per-task command: ``shell_command``'s
    wrapper, ``register_run``, and ``python_module``'s ``run-module`` dispatch.
    Resolving it here keeps executor selection out of the LLM — the orchestrator
    never threads it into ``spec.sidecar.executor`` (and so can't divine a broken
    ``python3 -m <module>`` for a ``python_module`` entry, the ridge_imp exit-127
    class). The interview is the source of truth, so its command WINS over a
    caller-supplied one when present.

    Defensive, mirroring ``submit_flow``'s ``interview.json`` read (#195): a
    missing / unreadable / non-object / ``executor_cmd``-less ``interview.json``
    returns ``None`` and the caller-supplied executor stands — the canonical
    bare-``@register_run`` path (no interview) is unchanged.
    """
    path = experiment_dir / "interview.json"
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    entry = (doc.get("_materialized") or {}).get("entry_point")
    if not isinstance(entry, dict):
        return None
    cmd = entry.get("executor_cmd")
    return cmd if isinstance(cmd, str) and cmd.strip() else None


def _live_canary_attempt(experiment_dir: Path, run_id: str) -> dict[str, Any] | None:
    """A LIVE canary-only prior attempt for *run_id*, or None — read in CODE.

    ``find-prior-run`` keys on the main sidecar's ``cmd_sha`` and reads a
    ``status`` the sidecar never carries (status lives on the journal RunRecord
    only), so it is blind to an attempt that died *pre-main-submit*: the main
    array never launched, but its ``<run_id>-canary`` sub-record + the S2
    worker's detached lease are still live. Re-resolving over that live attempt
    (e.g. to retarget a different cluster) would sail past the resume-vs-fresh
    fork and only meet it at the S2 backstop — too late for the human to pick.

    This surfaces the live canary attempt so the SAME ``prior_run_found`` fork
    fires at S1. Liveness mirrors the supersession doctrine verbatim (the #276
    corpse rule): a *non-terminal* ``<run_id>-canary`` record OR a live detached
    lease for the id counts as live; a TERMINAL canary (failed / complete) with
    no live lease is a corpse and does NOT block re-resolve. Returns the canary
    record's ``run_id`` / ``status`` / ``cluster`` for the decision brief.
    """
    from hpc_agent.ops.monitor.reconcile import _sibling_run_ids
    from hpc_agent.ops.supersession import _live_lease
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.run_record import TERMINAL_STATUSES

    # The paired ``<run_id>-canary`` entry via the one #258 suffix definition —
    # never a second hardcoded ``-canary`` (the `_sibling_run_ids` docstring owns
    # the pairing convention; supersession's `_supersede_missing_main` uses it too).
    (canary_id,) = _sibling_run_ids(run_id)
    canary = load_run(experiment_dir, canary_id)
    lease = _live_lease(run_id) or _live_lease(canary_id)
    canary_live = (
        canary is not None and canary.status not in TERMINAL_STATUSES
    ) or lease is not None
    if not canary_live:
        return None
    return {
        "prior_run_id": canary_id,
        "status": canary.status if canary is not None else "in_flight",
        "cluster": canary.cluster if canary is not None else None,
    }


@primitive(
    name="resolve-submit-inputs",
    verb="workflow",
    composes=[
        "compute-run-id",
        "find-prior-run",
        "build-tasks-py",
        "build-submit-spec",
        "write-run-sidecar",
    ],
    side_effects=[
        SideEffect("writes-sidecar", "<experiment>/.hpc/tasks.py (when scaffolded)"),
        SideEffect("writes-sidecar", "<experiment>/.hpc/cli.py (when scaffolded)"),
        SideEffect("writes-sidecar", "<experiment>/.hpc/runs/<run_id>.json (the per-run sidecar)"),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="submit.resolve.run_id",
    cli=CliShape(
        help=(
            "Deterministic submit input-resolution chain in one call: "
            "(build-tasks-py if tasks.py absent) → compute-run-id → "
            "find-prior-run → build-submit-spec. Returns a ready-to-submit "
            "context as one typed stage_reached outcome; sets needs_decision "
            "only on a live prior run or a needed scaffold interview."
        ),
        spec_arg=True,
        spec_model=ResolveSubmitInputsSpec,
        experiment_dir_arg=True,
        requires_ssh=False,
        schema_ref=SchemaRef(input="resolve_submit_inputs"),
    ),
    agent_facing=True,
)
def resolve_submit_inputs(
    experiment_dir: Path, *, spec: ResolveSubmitInputsSpec
) -> ResolveSubmitInputsResult:
    """Run the deterministic input-resolution chain to a ready-to-submit context.

    Returns a single :class:`ResolveSubmitInputsResult`; ``stage_reached`` is
    the deterministic dispatch the caller branches on, and ``needs_decision``
    is set only on the genuine escalations — a live prior run (the user picks
    resume-vs-fresh) or an absent ``.hpc/tasks.py`` with no deterministic
    scaffold spec to materialize it from. The clean ``resolved`` terminal
    carries the built submit-flow spec under ``submit_spec``.

    Ordering note: ``compute-run-id`` hashes ``.hpc/tasks.py``, so tasks.py
    must exist before it can run. The composite therefore ensures tasks.py
    first (scaffold or escalate), then computes the run_id — which is the
    submit.md Step 6a/6b-before-6c order, even though the pinned chain lists
    build-tasks-py after compute-run-id.
    """
    tasks_py = experiment_dir / ".hpc" / "tasks.py"

    # 1. Ensure .hpc/tasks.py exists. If absent and no deterministic scaffold
    #    spec was supplied, the scaffold needs a sub-interview the headless
    #    worker can't run — escalate (submit.md Step 6a/6b semantics).
    if not tasks_py.is_file():
        if spec.build_tasks is None:
            return ResolveSubmitInputsResult(
                stage_reached="needs_scaffold_interview",
                needs_decision=True,
                reason=(
                    ".hpc/tasks.py is absent and no build_tasks scaffold spec was "
                    "supplied; the scaffold needs an interactive sub-interview "
                    "(executor-discovery + axes) the headless worker can't run. "
                    "Resolve the axes upstream and re-invoke with build_tasks set, "
                    "or scaffold .hpc/tasks.py first."
                ),
            )
        # Deterministic scaffold: the caller pre-resolved axes/flags (and any
        # data_axis). build-tasks-py materializes .hpc/tasks.py + .hpc/cli.py.
        build_tasks_py(experiment_dir, spec=spec.build_tasks)

    # 2. compute-run-id: hash the (now-present) task list → run_id + cmd_sha.
    cr = compute_run_id(experiment_dir, run_name=spec.run_name)
    run_id = cr["run_id"]
    cmd_sha = cr["cmd_sha"]

    # 2b. Cross-check the agent-authored task counts against the ground truth
    #     compute-run-id just materialized (proving-run #5, finding 21). Both the
    #     submit spec's job-array size (`submit.total_tasks`) and the sidecar's
    #     `sidecar.task_count` are agent-authored and were otherwise NEVER checked
    #     against the real task list. An UNDERCOUNT fails SILENT: it sizes the job
    #     array `1-total_tasks`, the higher task_ids never dispatch, and the run
    #     returns incomplete results discovered only at harvest (the finding-16
    #     expensive class). compute-run-id is the ONE place the task list is
    #     materialized (`total` == len(trial_params)), so its count is
    #     authoritative — refuse a mismatch LOUDLY, naming both the declared
    #     value(s) and the true count. Mirrors the `interview` primitive's
    #     `tasks.total() != intent.task_count` cross-check (ops/memory/interview.py).
    true_total = cr["total"]
    if spec.submit.total_tasks != true_total or spec.sidecar.task_count != true_total:
        raise errors.SpecInvalid(
            "declared task count disagrees with the materialized task list: "
            f"submit.total_tasks={spec.submit.total_tasks}, "
            f"sidecar.task_count={spec.sidecar.task_count}, but .hpc/tasks.py "
            f"produces {true_total} tasks (tasks.total()). An undercount would "
            f"size the job array 1-{spec.submit.total_tasks} and silently drop "
            "the higher task_ids (incomplete results found only at harvest); fix "
            f"the declared count(s) to match the true {true_total}."
        )

    # 3. find-prior-run: branch on the resume contract (submit.md Step 6c).
    #    A live prior (found, not orphan, status complete/in_flight) is the
    #    only resume-vs-fresh decision the user owns; a terminal-but-not-
    #    complete record (failed/abandoned, #276) is forensic — proceed fresh.
    #    Reproduction-receipt lever (#207 sibling): when this resolution is a
    #    deliberate reproduction of an ORIGINAL run (same params → same cmd_sha),
    #    find-prior-run skips the original (and any prior reproduction of it), so
    #    a `complete` original no longer terminates resolve here — but ANY OTHER
    #    live prior with the same params still fires the guard (the fork keeps
    #    its fire path).
    fp = find_prior_run(experiment_dir, cmd_sha=cmd_sha, reproduction_of=spec.reproduction_of)
    if fp["found"] and not fp["is_orphan"] and (fp["status"] in _LIVE_PRIOR_STATUSES):
        return ResolveSubmitInputsResult(
            stage_reached="prior_run_found",
            needs_decision=True,
            reason=(
                f"a live prior run ({fp['prior_run_id']}, status={fp['status']}) "
                "matches this cmd_sha; only the user can choose resume-vs-fresh. "
                "Do NOT re-submit until they decide."
            ),
            run_id=run_id,
            cmd_sha=cmd_sha,
            prior_run_id=fp["prior_run_id"],
            prior_status=fp["status"],
            prior_cluster=fp["cluster"],
        )

    # 3b. Canary-only liveness (proving-run #5): find-prior-run above reads a
    #     status off the SIDECAR (which never carries one) and so is blind to an
    #     attempt that died pre-main-submit — the main array never launched, but
    #     the <run_id>-canary journal record + the S2 worker's detached lease are
    #     still live. Surface that as the SAME resume-vs-fresh fork so a retarget
    #     meets the human at S1, not the S2 backstop. A TERMINAL canary is a
    #     corpse and does NOT block (the #276 spirit; see _live_canary_attempt).
    canary = _live_canary_attempt(experiment_dir, run_id)
    if canary is not None:
        return ResolveSubmitInputsResult(
            stage_reached="prior_run_found",
            needs_decision=True,
            reason=(
                f"a live canary-only prior attempt ({canary['prior_run_id']}, "
                f"status={canary['status']}, cluster={canary['cluster']}) is in "
                "flight for this run_id — its main array never launched, so it is "
                "invisible to cmd_sha resume-detection. Only the user can choose "
                "resume-vs-fresh (and whether to retarget); do NOT re-submit until "
                "they decide."
            ),
            run_id=run_id,
            cmd_sha=cmd_sha,
            prior_run_id=canary["prior_run_id"],
            prior_status=canary["status"],
            prior_cluster=canary["cluster"],
        )

    # 4. build-submit-spec: assemble + validate the submit-flow spec. Inject the
    #    compute-run-id values — the spec's run_id/cmd_sha are placeholders — so
    #    the built spec always matches the reported run_id, not a stale caller value.
    # Pass experiment_dir (#292): the bare-script / $VAR guards resolve the
    # EXECUTOR's script path and load .hpc/tasks.py against it, not this
    # worker's CWD — the empirical path where the 0.10.11 register_run guard
    # silently no-op'd because Path(script).is_file() was CWD-relative.
    submit_spec = build_submit_spec(
        experiment_dir,
        spec=spec.submit.model_copy(update={"run_id": run_id, "cmd_sha": cmd_sha}),
    )
    # Thread the reproduction-receipt lever onto the built submit-flow spec so
    # the DETACHED submit-flow worker's submit-time layer-2 cmd_sha dedup pierces
    # the same original (the S1 find-prior-run skip above only covers this call).
    # BuildSubmitSpecInput carries no such field, so inject it into the built
    # dict directly — SubmitFlowSpec.reproduction_of accepts it (None-default).
    if spec.reproduction_of is not None:
        submit_spec["reproduction_of"] = spec.reproduction_of

    def _caller_executor_warning(executor: str, exp_dir: Path) -> str | None:
        """Re-capture ``check_per_task_executor``'s interface-blind RuntimeWarning.

        The sidecar write above already ran the check (a refusal would have
        raised there); this re-run is static and idempotent, existing only to
        CAPTURE the warning text for the S1 reason instead of letting it die in
        a detached worker's log. Never raises — a refusal-class executor never
        reaches here, and any surprise degrades to "no warning".
        """
        import warnings as _warnings

        from hpc_agent.incorporation.build.submit_spec import check_per_task_executor

        try:
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter("always")
                check_per_task_executor(executor, experiment_dir=exp_dir)
        except Exception:  # noqa: BLE001 — capture-only seam; the write already gated
            return None
        for w in caught:
            if issubclass(w.category, RuntimeWarning):
                return str(w.message)
        return None

    # 5. write-run-sidecar: write the per-run sidecar so the #171 write-first
    #    precondition is satisfied BEFORE submit-pipeline runs — the `resolved`
    #    output is fully submit-ready. Same run_id/cmd_sha injection (after the
    #    find-prior-run resume check cleared, so no sidecar is written for a
    #    resume-or-escalate path). The per-task executor is resolved in CODE from
    #    the interview's materialized entry point when present (see
    #    _materialized_executor_cmd) — so it's the framework, not the LLM, that
    #    decides a python_module dispatches via run-module.
    #    compute-run-id is the ONE place the task list is materialized, so it is
    #    authoritative for the per-task round-trip: inject its trial_tokens
    #    (opaque reconciliation key) AND trial_params (the cmd_sha pre-image,
    #    persisted for provenance) — not the caller's placeholders — so both land
    #    on the sidecar without the caller hand-threading them and getting them
    #    wrong. trial_tokens stays None for ordinary submits (omitted on write);
    #    trial_params makes every run's params recoverable from its sidecar.
    #    Reproduction-receipt provenance: stamp `reproduces` onto the sidecar so
    #    the derived run records which ORIGINAL it reproduces — a LATER
    #    reproduction of the same original then reads it back (find_run_by_cmd_sha
    #    reproduction_of lever) to skip this derived run too, not just the
    #    original. None for an ordinary submit (omitted on write).
    sidecar_spec = spec.sidecar.model_copy(
        update={
            "run_id": run_id,
            "cmd_sha": cmd_sha,
            "trial_tokens": cr["trial_tokens"],
            "trial_params": cr["trial_params"],
            "reproduces": spec.reproduction_of,
        }
    )
    materialized_executor = _materialized_executor_cmd(experiment_dir)
    if materialized_executor is not None:
        sidecar_spec = sidecar_spec.model_copy(update={"executor": materialized_executor})
    written = write_run_sidecar(experiment_dir=experiment_dir, spec=sidecar_spec)

    reason = (
        "inputs resolved: tasks.py present, no live prior, submit-flow spec "
        "built + validated, and the per-run sidecar written (#171) — hand "
        "submit_spec to submit-pipeline."
    )
    if materialized_executor is None:
        # No interview materialized the executor → the CALLER supplied it, and
        # the "framework, not the LLM, decides" invariant did not apply. The
        # write above already ran check_per_task_executor, but its
        # interface-blind RuntimeWarning lands in a detached worker's log where
        # no human sees it until the canary fails (run #8 live: a hand-onboarded
        # `executor: "run"` sailed to a failed canary on TWO clusters). Surface
        # it HERE, in the S1 resolved reason, so the y/nudge boundary shows it
        # pre-SSH and pre-cost. Warn-not-refuse stands (a legit $PATH wrapper
        # reading $HPC_TASK_ID is the false-positive; the canary is the hard
        # backstop) — this is legibility, not a new gate.
        blind = _caller_executor_warning(str(sidecar_spec.executor or ""), experiment_dir)
        if blind:
            reason = (
                f"WARNING (caller-supplied executor; no interview.json to derive "
                f"from): {blind} Complete the wrap-entry-point interview to "
                f"derive the executor mechanically, or supply a real per-task "
                f"command. — {reason}"
            )

    return ResolveSubmitInputsResult(
        stage_reached="resolved",
        needs_decision=False,
        reason=reason,
        run_id=run_id,
        cmd_sha=cmd_sha,
        submit_spec=submit_spec,
        sidecar_path=written.get("path"),
    )
