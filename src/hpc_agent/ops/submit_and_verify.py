"""``submit-and-verify``: two-phase canary gate over submit-flow + verify-canary.

One call instead of /submit-hpc then /verify-canary. The canary is a GATE
(#160): submit the 1-task canary FIRST (``canary_only``), verify it lands and
produces output, and launch the main array ONLY on success — so a broken
dispatch never reaches the full run.

This is a workflow-composes-workflow primitive: ``submit-flow`` and
``verify-canary`` are both workflow-verb primitives in their own right;
``submit-and-verify`` chains them under one envelope.

Paths:

* ``spec.submit.canary=False`` — no canary, so the main array submits directly
  and there's nothing to verify. ``verified=False``, ``verify_result=None``.
* Phase 1 ``submit-flow`` returns ``deduped=True`` (the run already exists) —
  no fresh canary; pass the submit result through without a stale verify.
* Canary verified → Phase 2 launches the main array; ``verified=True`` with the
  main ``job_ids``.
* Canary FAILED → the main array never launches; ``verified=False``,
  ``failure_kind`` set, and ``job_ids`` empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.submit_and_verify import (
    ReducerCheckResult,
    SubmitAndVerifyResult,
    SubmitAndVerifySpec,
)
from hpc_agent._wire.workflows.verify_canary import VerifyCanaryResult
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.clusters import resolve_ssh_target
from hpc_agent.ops.aggregate.cluster_reduce import cluster_reduce
from hpc_agent.ops.submit_flow import SubmitFlowResult, fire_second_canary, submit_flow
from hpc_agent.ops.verify_canary import verify_canary

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

__all__ = ["launch_main_array", "submit_and_verify"]


def _mark_canary_terminal(experiment_dir: Path, canary_run_id: str | None, *, status: str) -> None:
    """Close the canary's RunRecord once its verdict is known (§5 watchdog).

    A canary is a 1-task run with its own ``<main>-canary`` RunRecord, created
    ``in_flight`` at submission. ``verify-canary`` is a side-effect-free query:
    it polls the canary to terminal ON THE CLUSTER but never closes the LOCAL
    record. Left alone, a verified canary lingers ``in_flight`` and the §5
    watchdog / ``doctor`` false-flags it as a stalled driver (and would draft a
    spurious re-arm). Transition it here, where the canary's lifecycle is owned
    (``submit_and_verify`` submitted it). Best-effort: a bookkeeping stamp must
    never fail the submit gate — a deduped/cache-hit canary has no fresh record
    (``FileNotFoundError`` is benign), and any other stamp error is warned, not
    raised (the next reconcile re-derives ground truth regardless).
    """
    if not canary_run_id:
        return
    try:
        from hpc_agent.state.journal import mark_run

        mark_run(experiment_dir, canary_run_id, status=status)
    except FileNotFoundError:
        pass  # deduped / cache-hit canary — no fresh record to close
    except Exception:  # noqa: BLE001 — a terminal stamp must never fail the gate
        import logging

        logging.getLogger(__name__).warning(
            "failed to mark canary %r terminal (status=%s); doctor may "
            "transiently flag it as stalled until the next reconcile",
            canary_run_id,
            status,
            exc_info=True,
        )


# The canary reducer check's bounded timeout — SMALL (not cluster_reduce's
# 1800s harvest default) so a hung reducer never stalls the S2→S3 human-review
# window the check amortizes against.
_REDUCER_CHECK_TIMEOUT_SEC = 300


def _declared_custom_reducer(experiment_dir: Path, run_id: str) -> str | None:
    """The run's declared custom ``aggregate_defaults.aggregate_cmd``, or None.

    None (⇒ the reducer check SKIPS) when the sidecar is unreadable/absent or
    declares no custom reducer — the built-in mean is framework code, nothing to
    check. Routed through the tolerant :func:`read_run_sidecar_or_empty` so a
    missing/torn sidecar degrades to "no custom reducer" (a skip is byte-identical
    to a pre-feature run) rather than raising into the gate.
    """
    from hpc_agent.state.runs import read_run_sidecar_or_empty

    try:
        defaults = read_run_sidecar_or_empty(experiment_dir, run_id).get("aggregate_defaults")
    except Exception:  # noqa: BLE001 — the gate read is best-effort; unreadable ⇒ skip
        return None
    if not isinstance(defaults, dict):
        return None
    cmd = defaults.get("aggregate_cmd")
    return cmd if isinstance(cmd, str) and cmd.strip() else None


def _check_reducer_on_canary(
    experiment_dir: Path,
    base: SubmitFlowSpec,
    canary_run_id: str | None,
) -> ReducerCheckResult:
    """Rung 2 of the reducibility ladder: EXECUTE the run's declared custom
    reducer against the verified canary's ONE real task-0 row, before the main
    array launches (``docs/plans/amortized-reduction-check-2026-07-17.md``).

    A verified canary's task-0 output is a GENUINE task artifact — the exact
    shape the main array's tasks write. Run the SAME ``cluster_reduce`` the final
    harvest runs (one-definition — no rung re-derives the reduction) with the
    canary's run_id + the main run's declared ``aggregate_cmd``, so the reducer's
    literal ``python3`` binds the run's env interpreter (the only way to catch the
    py3.8-vs-3.13 class before the array runs). Assert only the contract SHAPE
    (exit 0, parseable JSON) — never a VALUE.

    Best-effort by contract, exactly like :func:`_mint_double_canary_sample`:
    this NEVER fails a submit whose canary verified ok.

    * reducer RAN and failed (non-zero exit / missing output / non-JSON =
      :class:`errors.RemoteCommandFailed`) → ``disclosed``, verbatim stderr, the
      bare ``y`` still crosses it (the failure MIGHT be a benign "needs ≥2 rows"
      false alarm — the machinery surfaces the error and stops, never interpreting
      "broken code" vs "needs more rows").
    * check could not COMPLETE (severed / breaker open / timeout) → ``unverified``
      (UNKNOWN), NEVER a pass (positive-evidence-only).
    * no custom reducer / opted out → ``skipped``.
    """
    from hpc_agent.infra.env_flags import env_flag

    if env_flag("HPC_NO_CANARY_REDUCER_CHECK") or not canary_run_id:
        return ReducerCheckResult(status="skipped")
    aggregate_cmd = _declared_custom_reducer(experiment_dir, base.run_id)
    if aggregate_cmd is None:
        # No custom reducer declared — the built-in mean is framework code,
        # nothing to prove. Byte-identical to a pre-feature run.
        return ReducerCheckResult(status="skipped")

    try:
        result = cluster_reduce(
            experiment_dir,
            run_id=canary_run_id,
            aggregate_cmd=aggregate_cmd,
            # A distinct remote path so the check never clobbers the canary's own
            # aggregated output; cluster_reduce defaults the local pull dir.
            output_path=f"_aggregated/_reducecheck/{canary_run_id}.json",
            timeout_sec=_REDUCER_CHECK_TIMEOUT_SEC,
        )
    except errors.RemoteCommandFailed as exc:
        # The reducer RAN and produced positive evidence of a problem (non-zero
        # exit / missing output / non-JSON). DISCLOSE verbatim — NEVER block.
        stderr = str(exc)
        return ReducerCheckResult(
            status="disclosed",
            reducer_cmd=aggregate_cmd,
            stderr_tail=stderr,
            disclosure=(
                "reducer check: the declared reducer FAILED against the canary's single "
                "real row — read the error and register/fix it before aggregate (a bare "
                f"`y` proceeds regardless): {stderr}"
            ),
        )
    except (
        errors.SshUnreachable,
        errors.SshCircuitOpen,
        errors.SshSlotWaitTimeout,
        errors.ClusterTimeout,
    ) as exc:
        # The check could not COMPLETE (channel severed / breaker open / timeout).
        # UNKNOWN — never a pass (positive-evidence-only, the reporter_unreachable
        # / BATCH_END_SENTINEL truncation posture).
        return ReducerCheckResult(
            status="unverified",
            reducer_cmd=aggregate_cmd,
            stderr_tail=str(exc),
            disclosure=(
                "reducer check: could not run against the canary row "
                f"(channel severed/timeout) — UNVERIFIED, not a pass: {exc}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; unknown = unverified, never block
        import logging

        logging.getLogger(__name__).warning(
            "canary reducer check for run %r raised unexpectedly; recording UNVERIFIED "
            "(this best-effort check never fails the submit)",
            base.run_id,
            exc_info=True,
        )
        return ReducerCheckResult(
            status="unverified",
            reducer_cmd=aggregate_cmd,
            stderr_tail=str(exc),
            disclosure=(
                "reducer check: could not run against the canary row — UNVERIFIED, "
                f"not a pass: {exc}"
            ),
        )

    reduced = result.get("reduced")
    output_keys = sorted(reduced.keys()) if isinstance(reduced, dict) else []
    return ReducerCheckResult(
        status="passed",
        reducer_cmd=aggregate_cmd,
        exit_code=int(result.get("exit_code", 0) or 0),
        output_keys=output_keys,
    )


#: The per-task runtime sidecar the dispatcher writes into each result dir,
#: carrying the U-HW1 placement facts (node / cpu_model / partition). Named here
#: (not imported at module top to avoid an import cycle) in lock-step with
#: :data:`hpc_agent.ops.submit.hw_facts_capture.RUNTIME_SIDECAR_NAME`; the
#: fingerprint pull widens its include to bring this home for free.
# MIRROR: hpc_agent.ops.submit.hw_facts_capture::RUNTIME_SIDECAR_NAME pinned-by tests/ops/submit/test_hw_facts_capture.py::test_runtime_sidecar_name_mirrors_dispatcher  # noqa: E501
_RUNTIME_SIDECAR_NAME = "_runtime.json"


@dataclass(frozen=True)
class _CanaryTask0Pull:
    """A canary's task-0 pull coordinates, rendered locally (no round-trip).

    Where its task-0 summary artifact lives on the cluster: the ``ssh_target`` +
    project ``remote_path`` its RunRecord carries, the ``result_subdir`` its
    sidecar ``result_dir_template`` renders for task 0, and the declared
    ``summary_name`` (F-J). :func:`_render_canary_task0_pull` builds it; the two
    pull seams (:func:`_pull_canary_task0_metrics`, single;
    :func:`_pull_both_canary_task0_metrics`, folded) turn one/both into rsync.
    """

    ssh_target: str
    remote_path: str
    result_subdir: str
    summary_name: str


def _render_canary_task0_pull(experiment_dir: Path, canary_run_id: str) -> _CanaryTask0Pull:
    """Render a canary's task-0 pull coordinates from its journal record + sidecar.

    Pure local work — NO round-trip (the split that lets the double canary fold
    both pulls into ONE cycle, F6). Raises on a missing record / absent template
    / a template field the sidecar cannot supply — the pull callers treat any
    raise as "no sample this submit" (best-effort minting).
    """
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import read_run_sidecar, resolved_summary_artifact

    record = load_run(experiment_dir, canary_run_id)
    if record is None:
        raise errors.SpecInvalid(f"no journal record for canary {canary_run_id!r}")
    sidecar = read_run_sidecar(experiment_dir, canary_run_id)
    # The canary's declared per-task summary filename (F-J). The canary is
    # submitted through the same pipeline as the main run, so its sidecar
    # carries the SAME summary_artifact — resolve it here (absent/blank →
    # metrics.json) so the pull filter + rglob key on the real file instead of
    # the metrics.json hardcode that missed a non-default emitter (run #10).
    summary_name = resolved_summary_artifact(sidecar)
    template = sidecar.get("result_dir_template")
    if not isinstance(template, str) or not template:
        raise errors.SpecInvalid(
            f"canary {canary_run_id!r} sidecar carries no result_dir_template to render task 0"
        )
    # Task 0 result dir (relative to remote_path), rendered with task 0's REAL
    # kwargs when the sidecar recorded them (``trial_params`` — the canary IS
    # task 0), so a sweep-axis template ({estimator}/{chunk_start}/…) renders.
    # A field the record cannot supply is the documented "cannot pull" raise —
    # NEVER a bare KeyError, which escapes the callers' best-effort catch and
    # killed the whole S2 worker post-submit (run-#12 finding 18).
    trial_params = sidecar.get("trial_params")
    fields: dict[str, object] = {}
    if isinstance(trial_params, list) and trial_params and isinstance(trial_params[0], dict):
        fields = dict(trial_params[0])  # the sidecar shape: list[dict], task 0 first
    elif isinstance(trial_params, dict):
        fields = dict(trial_params)
    fields.update(task_id=0, run_id=canary_run_id)
    try:
        result_subdir = template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise errors.SpecInvalid(
            f"canary {canary_run_id!r} result_dir_template {template!r} references "
            f"a field the sidecar cannot supply ({exc!r}) — cannot render task 0 locally"
        ) from exc
    return _CanaryTask0Pull(
        ssh_target=resolve_ssh_target(record),
        remote_path=record.remote_path,
        result_subdir=result_subdir,
        summary_name=summary_name,
    )


def _pull_canary_task0_metrics(experiment_dir: Path, canary_run_id: str) -> Path:
    """Pull a canary's task-0 ``metrics.json`` locally under the fingerprint pulls dir.

    ``verify_canary`` only sha-fingerprints the metrics over SSH; the sample's
    ``bind`` recompute needs the payload ON DISK. Reuse the
    ``ops/aggregate_flow.py::_per_task_metrics_reduce`` rsync idiom: render the
    canary's task-0 result dir from its sidecar ``result_dir_template`` and pull
    the declared summary artifact into
    ``_aggregated/_fingerprints/_pulls/<canary_run_id>/`` (T3's :func:`pulls_dir`).
    Returns the local artifact path. Raises on a missing record / unrenderable
    template / failed pull / no file — the caller treats any raise as "no sample
    this submit" (best-effort minting). The double-canary path instead folds both
    canaries into ONE cycle via :func:`_pull_both_canary_task0_metrics`; this
    single-canary seam stays for its direct callers/tests.
    """
    from hpc_agent.infra.transport import rsync_pull
    from hpc_agent.state.fingerprint_store import pulls_dir

    plan = _render_canary_task0_pull(experiment_dir, canary_run_id)
    local = pulls_dir(experiment_dir, canary_run_id)
    pull = rsync_pull(
        ssh_target=plan.ssh_target,
        remote_path=plan.remote_path,
        remote_subdir=plan.result_subdir,
        local_dir=str(local),
        include=[plan.summary_name],
    )
    if pull.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"pull of {canary_run_id!r} task-0 {plan.summary_name} failed "
            f"(exit {pull.returncode}): {(pull.stderr or '').strip()[:200]}"
        )
    hits = sorted(p for p in local.rglob(plan.summary_name) if p.is_file())
    if not hits:
        raise errors.RemoteCommandFailed(
            f"no {plan.summary_name} pulled for canary {canary_run_id!r} "
            f"under {plan.result_subdir!r}"
        )
    return hits[0]


def _pull_both_canary_task0_metrics(
    experiment_dir: Path,
    *,
    first_canary_run_id: str,
    second_canary_run_id: str,
    dest_run_id: str,
) -> tuple[Path, Path]:
    """Fetch BOTH canaries' task-0 summary artifacts in ONE pull cycle (F6).

    The double canary's two 1-task executions are the SAME submission to the SAME
    cluster: they share an ``ssh_target`` + project ``remote_path`` and differ
    only by the ``-canary`` / ``-canary2`` run-id segment of their task-0 result
    dirs. Pulling them one at a time (the old ``_pull_canary_task0_metrics`` × 2)
    costs two transport round-trips — four under the native-Windows ``tar_ssh_pull``
    engine (a manifest exec + a transfer each). Instead, render both task-0 dirs
    locally (no round-trip), pull their common ancestor ONCE with an ``include``
    per canary, and return the two landed artifact paths — halving the sample's
    transport cost (latency-elimination F6) while the two payloads (hence the
    sample's two compared identities) stay distinct on disk.

    Both artifacts land under one ``_pulls/<dest_run_id>/`` root at the relative
    path each canary's result dir resolves to (the run-id segment keeps them
    apart). Raises on a missing record / unrenderable template / a host+remote_path
    mismatch (the same-submission invariant broken) / failed pull / a missing
    artifact — the caller (:func:`_mint_double_canary_sample`) treats any raise as
    "no sample this submit" (best-effort minting).
    """
    import posixpath

    from hpc_agent.infra.transport import rsync_pull
    from hpc_agent.state.fingerprint_store import pulls_dir

    plan_a = _render_canary_task0_pull(experiment_dir, first_canary_run_id)
    plan_b = _render_canary_task0_pull(experiment_dir, second_canary_run_id)
    # Same submission ⇒ same host + project root, so ONE cycle keys on one target.
    # A mismatch (should be impossible by construction) fails the fold loudly
    # rather than silently pulling the wrong host's tree; the best-effort catch
    # turns it into "no sample", never a wrong sample.
    if plan_a.ssh_target != plan_b.ssh_target or plan_a.remote_path != plan_b.remote_path:
        raise errors.SpecInvalid(
            "double-canary pull fold expects both canaries on the same host + remote_path "
            f"(got {plan_a.ssh_target!r}:{plan_a.remote_path!r} vs "
            f"{plan_b.ssh_target!r}:{plan_b.remote_path!r})"
        )
    sub_a = plan_a.result_subdir.strip("/")
    sub_b = plan_b.result_subdir.strip("/")
    file_a = posixpath.join(sub_a, plan_a.summary_name)
    file_b = posixpath.join(sub_b, plan_b.summary_name)
    # U-HW1 (gap #5): each canary's per-task ``_runtime.json`` (the placement facts
    # the dispatcher already emits — node / cpu_model / partition) lives in the
    # SAME result dir as the summary, so it rides home on THIS pull for the cost of
    # one more ``include`` pattern — the hardware-facts capture is zero new
    # round-trip. Best-effort: an old wheel that wrote no ``_runtime.json`` simply
    # transfers nothing extra (rsync ignores an absent include), and the hw stamp
    # records could-not-capture. The runtime file is NOT asserted-present below;
    # only the two summaries are required.
    rt_a = posixpath.join(sub_a, _RUNTIME_SIDECAR_NAME)
    rt_b = posixpath.join(sub_b, _RUNTIME_SIDECAR_NAME)
    # Pull the two files' common ancestor so the ONE cycle's server-side walk is
    # scoped (typically ``results/``); the per-file includes are anchored to it.
    common = posixpath.commonpath([sub_a, sub_b]) if sub_a and sub_b else ""
    inc_a = posixpath.relpath(file_a, common) if common else file_a
    inc_b = posixpath.relpath(file_b, common) if common else file_b
    inc_rt_a = posixpath.relpath(rt_a, common) if common else rt_a
    inc_rt_b = posixpath.relpath(rt_b, common) if common else rt_b
    local = pulls_dir(experiment_dir, dest_run_id)
    pull = rsync_pull(
        ssh_target=plan_a.ssh_target,
        remote_path=plan_a.remote_path,
        remote_subdir=common,
        local_dir=str(local),
        include=sorted({inc_a, inc_b, inc_rt_a, inc_rt_b}),
    )
    if pull.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"double-canary pull of {first_canary_run_id!r}+{second_canary_run_id!r} failed "
            f"(exit {pull.returncode}): {(pull.stderr or '').strip()[:200]}"
        )
    path_a = local.joinpath(*inc_a.split("/"))
    path_b = local.joinpath(*inc_b.split("/"))
    for pth, rid, inc in (
        (path_a, first_canary_run_id, inc_a),
        (path_b, second_canary_run_id, inc_b),
    ):
        if not pth.is_file():
            raise errors.RemoteCommandFailed(
                f"no task-0 artifact pulled for canary {rid!r} under {inc!r}"
            )
    return path_a, path_b


def _mint_double_canary_sample(
    experiment_dir: Path,
    *,
    base: SubmitFlowSpec,
    first_canary_run_id: str,
    second_canary_run_id: str,
) -> None:
    """Fetch both canaries' task-0 metrics, diff them, and append the n=2 prior.

    Best-effort by contract: evidence collection must NEVER fail a submit whose
    two canaries both verified ok. Any failure (pull miss, unrenderable template,
    empty identity, malformed metrics) is warned and swallowed — the fingerprint
    simply doesn't grow on this submit. The sample is ``source="double-canary"``,
    ``scale="canary"``, ``same_submission=True``, ``verdict="auto_cleared"`` (both
    executions verified ok — the passing code verdict; admitted by construction,
    D-consume). Identity (``cmd_sha``/``tasks_py_sha``/``executor``) is lifted
    from the main run's sidecar.
    """
    import json

    from hpc_agent.infra.time import utcnow_iso
    from hpc_agent.state import determinism, fingerprint_store
    from hpc_agent.state.runs import read_run_sidecar

    try:
        main_sidecar = read_run_sidecar(experiment_dir, base.run_id)
        identity = {
            "cmd_sha": str(main_sidecar.get("cmd_sha") or ""),
            "tasks_py_sha": str(main_sidecar.get("tasks_py_sha") or ""),
            "executor": str(main_sidecar.get("executor") or ""),
        }
        # Data-identity leg (Phase-3 amendment, ruled 0b): stamp the run's data
        # identity onto the sample so a LATER comparison under rebuilt input files
        # reads this prior as DATA DRIFT, not nondeterminism. Only when KNOWN — an
        # absent data_manifest_sha leaves the leg off (disclosed-unknown, the
        # exclude-none spirit; the wire SampleIdentity.data_sha defaults null).
        data_sha = main_sidecar.get("data_manifest_sha")
        if data_sha:
            identity["data_sha"] = str(data_sha)
        path_a, path_b = _pull_both_canary_task0_metrics(
            experiment_dir,
            first_canary_run_id=first_canary_run_id,
            second_canary_run_id=second_canary_run_id,
            dest_run_id=base.run_id,
        )
        # U-HW1 (gap #5): the canary's placement facts (node / cpu_model /
        # partition) rode home in ``_runtime.json`` on the SAME pull above —
        # reduce them to an additive hw_sha on the MAIN run's sidecar so a later
        # reproduction on different hardware is DISCLOSED (never gated). Its OWN
        # guard, so a hw stamp never disturbs the fingerprint sample below; zero
        # new round-trip (rides the existing pull).
        _capture_hw_facts_best_effort(
            experiment_dir, run_id=base.run_id, canary_summary_path=path_a
        )
        payload_a = json.loads(path_a.read_text(encoding="utf-8"))
        payload_b = json.loads(path_b.read_text(encoding="utf-8"))
        per_key = determinism.diff_metrics(payload_a, payload_b)
        content_sha = fingerprint_store.content_sha_over_payloads(payload_a, payload_b)
        record = determinism.build_sample_record(
            ts=utcnow_iso(),
            content_sha=content_sha,
            identity=identity,
            source="double-canary",
            run_ids=[first_canary_run_id, second_canary_run_id],
            cluster=base.cluster,
            scale="canary",
            verdict="auto_cleared",
            per_key=per_key,
            same_submission=True,
        )
        fingerprint_store.append_sample(
            experiment_dir, record=record, artifact_a=path_a, artifact_b=path_b
        )
    except Exception:  # noqa: BLE001 — evidence minting never fails a passing submit
        import logging

        logging.getLogger(__name__).warning(
            "double-canary fingerprint sample not minted for run %r (both canaries "
            "verified ok; the fingerprint simply did not grow this submit)",
            base.run_id,
            exc_info=True,
        )


def _capture_hw_facts_best_effort(
    experiment_dir: Path, *, run_id: str, canary_summary_path: Path
) -> None:
    """Reduce + stamp the run's hardware facts off the pulled canary runtime (U-HW1).

    The canary's per-task ``_runtime.json`` (the placement facts the dispatcher
    emitted — node / cpu_model / partition) rode home NEXT TO its task-0 summary
    on the fingerprint pull, so it sits at ``canary_summary_path.parent /
    _runtime.json``. This reads THAT already-landed file (NO new round-trip),
    reduces it to an additive ``hw_sha``, and stamps it on the MAIN run's sidecar
    (:func:`hpc_agent.ops.submit.hw_facts_capture.capture_and_stamp_hw_facts`), so
    a later reproduction on different hardware is DISCLOSED (never gated) — gap #5
    closed on the capture side.

    Best-effort by contract, exactly like :func:`_capture_env_lock_best_effort`:
    the capture is itself never-raising (an absent/torn runtime stamps an honest
    ``could_not_capture`` status), but the call is wrapped here too so no
    unexpected error can fail a submit whose canary verified.
    """
    from hpc_agent.ops.submit.hw_facts_capture import (
        RUNTIME_SIDECAR_NAME,
        capture_and_stamp_hw_facts,
    )

    runtime_path = canary_summary_path.parent / RUNTIME_SIDECAR_NAME

    def _load(*, canary_run_id: str) -> dict[str, object] | None:
        if not runtime_path.is_file():
            return None
        import json

        data = json.loads(runtime_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None

    try:
        capture_and_stamp_hw_facts(experiment_dir, run_id=run_id, canary_run_id=run_id, load=_load)
    except Exception:  # noqa: BLE001 — hw-facts capture never fails a passing submit
        import logging

        logging.getLogger(__name__).warning(
            "hw_facts capture for run %r raised unexpectedly; the sidecar simply "
            "carries no hw_facts this submit (disclosed as not-captured at verify)",
            run_id,
            exc_info=True,
        )


def _capture_env_lock_best_effort(experiment_dir: Path, *, run_id: str, canary_run_id: str) -> None:
    """Resolve + stamp the run's env_lock off the verified canary (U-ENV1).

    The canary just verified — it ran a real task under the run's env, so this is
    where the RESOLVED-environment snapshot (pip freeze / lockfile / python -V) is
    captured over SSH (in the canary's activation) and reduced to an additive
    ``env_lock_sha`` on the MAIN run's sidecar
    (:func:`hpc_agent.ops.submit.env_lock_capture.capture_and_stamp_env_lock`).
    A later reproduction under a drifted env is then DISCLOSED (never gated) —
    the #2 crisis gap closed on the capture side.

    Best-effort by contract, exactly like :func:`_mint_double_canary_sample`: the
    capture is itself never-raising (an unresolvable env stamps an honest
    ``could_not_capture`` status), but the call is wrapped here too so no
    unexpected error can fail a submit whose canary verified.
    """
    from hpc_agent.ops.submit.env_lock_capture import capture_and_stamp_env_lock

    try:
        capture_and_stamp_env_lock(experiment_dir, run_id=run_id, canary_run_id=canary_run_id)
    except Exception:  # noqa: BLE001 — env-lock capture never fails a passing submit
        import logging

        logging.getLogger(__name__).warning(
            "env_lock capture for run %r raised unexpectedly; the sidecar simply "
            "carries no env_lock this submit (disclosed as not-captured at verify)",
            run_id,
            exc_info=True,
        )


def _fire_second_canary_concurrent(
    experiment_dir: Path,
    spec: SubmitAndVerifySpec,
    canary_submit: SubmitFlowResult,
) -> str | None:
    """Fire the SECOND canary NOW so it queues CONCURRENTLY with the first (RANK 8).

    The two canaries are independent 1-task executions of the SAME command — the
    n=2 determinism prior's two samples — and the diff consumer
    (:func:`_mint_double_canary_sample`) reads both metrics order-independently, so
    nothing requires serializing them. Firing the second here — right after Phase 1
    deployed + fired the first, BEFORE the first is verified — lets the scheduler
    parallelize both queue-waits + runtimes: the canary stage costs
    ``max(two)`` instead of the old ``2×(queue+run+verify)`` (finding 2). Its
    sidecar is SHIPPED in this leg (``fire_second_canary`` → ``ship_sidecar=True``,
    finding 7) since Phase 1's rsync already ran.

    Returns the second canary's run id (verified later by
    :func:`_verify_second_canary_and_mint`), or ``None`` when the double canary is
    off (``HPC_NO_DOUBLE_CANARY=1``, the operator env idiom — no agent-reachable
    spec field) or the fire failed. A FIRE failure degrades to a single canary
    (warned, sample simply doesn't mint) rather than failing a submit whose first
    canary is about to be verified on its own merits.
    """
    from hpc_agent.infra.env_flags import env_flag

    if env_flag("HPC_NO_DOUBLE_CANARY"):
        return None

    base = spec.submit
    second_canary_run_id = f"{base.run_id}-canary2"
    try:
        # Fresh ``-canary2`` id → submit_flow's existing-canary replay branch never
        # reuses the completed first canary; ships its sidecar before the qsub.
        fire_second_canary(experiment_dir, spec=base, canary_run_id=second_canary_run_id)
    except Exception:  # noqa: BLE001 — a fire failure degrades to a single canary
        import logging

        logging.getLogger(__name__).warning(
            "second canary %r could not be fired; proceeding with a single canary "
            "(the n=2 determinism sample simply will not mint this submit)",
            second_canary_run_id,
            exc_info=True,
        )
        return None
    return second_canary_run_id


def _verify_second_canary_and_mint(
    experiment_dir: Path,
    spec: SubmitAndVerifySpec,
    canary_submit: SubmitFlowResult,
    second_canary_run_id: str,
) -> SubmitAndVerifyResult | None:
    """Verify the ALREADY-FIRED second canary, then mint the n=2 determinism prior.

    Called after the FIRST canary verified ok. The second canary was fired
    concurrently by :func:`_fire_second_canary_concurrent`, so by now it is
    usually already terminal — this verify is a cheap terminal read, not a fresh
    queue+run wait.

    Returns ``None`` to let the submit proceed (both verified ok; the sample mints
    best-effort). Returns a BLOCKING :class:`SubmitAndVerifyResult`
    (``verified=False``, empty ``job_ids``) when the second canary FAILS — the
    same-code-passed-then-failed nondeterminism finding, blocking the main array
    exactly like a failed first canary.
    """
    base = spec.submit
    first_canary_run_id = canary_submit.canary_run_id  # ``<main>-canary``
    canary_job_ids = list(canary_submit.canary_job_ids) if canary_submit.canary_job_ids else None

    # Verify it the SAME way. Substitute the ``-canary2`` run_id by OMITTING
    # expect_output/fingerprint: a path built for ``-canary`` cannot contain
    # ``-canary2`` and verify_canary REFUSES an expect_output not naming the
    # canary run_id (the completion count still verifies the second canary's
    # output). checkpoint_result_dir is likewise derived from the ``-canary2``
    # sidecar rather than the first canary's path.
    second_verify = VerifyCanaryResult.model_validate(
        verify_canary(
            experiment_dir,
            canary_run_id=second_canary_run_id,
            expect_output=None,
            fingerprint=None,
            verify_checkpoint=base.auto_resume_on_kill,
            checkpoint_result_dir=None,
            poll_interval_sec=spec.poll_interval_sec,
            wait_budget_sec=spec.wait_budget_sec,
            log_dir=spec.log_dir,
            file_glob=spec.file_glob,
        )
    )

    if not second_verify.ok:
        # LOUD nondeterminism finding: the SAME code passed then failed. Block the
        # main array exactly like a failed first canary, and close the second
        # canary's record so it doesn't linger in_flight (§5).
        _mark_canary_terminal(experiment_dir, second_canary_run_id, status="failed")
        return SubmitAndVerifyResult(
            run_id=canary_submit.run_id,
            job_ids=[],
            total_tasks=canary_submit.total_tasks,
            deduped=False,
            canary_run_id=first_canary_run_id,
            canary_job_ids=canary_job_ids,
            verified=False,
            # The second verify always sets a failure_kind on ok=False; surface it
            # verbatim (the wire vocabulary is closed) — the nondeterminism framing
            # lives in the details/verify_result, not a new failure_kind literal.
            failure_kind=second_verify.failure_kind,
            verify_result=second_verify,
        )

    # Second canary verified too. Close its record, then mint the n=2 prior from
    # both executions' task-0 metrics (best-effort — never fails the gate).
    _mark_canary_terminal(experiment_dir, second_canary_run_id, status="complete")
    _mint_double_canary_sample(
        experiment_dir,
        base=base,
        first_canary_run_id=first_canary_run_id or f"{base.run_id}-canary",
        second_canary_run_id=second_canary_run_id,
    )
    return None


def _record_canary_gate_validated(base: SubmitFlowSpec) -> None:
    """#249: record this ``cmd_sha`` as canary-validated after the FULL gate passed.

    B7: minting moved here from ``verify_canary``'s per-canary success path.
    ``verify_canary`` runs once PER canary; the old mint stamped the #249 TTL
    cache on the FIRST canary's success — mid-gate — so a failed SECOND canary
    blocked the main once, then a retry inside the 4h TTL cache-skipped BOTH
    canaries on a ``cmd_sha`` that never fully validated. This site is reached
    only past BOTH canary verdicts, so the cache reflects the whole gate.

    Keyed on the SAME ``(cmd_sha, version, cluster)`` the readers compute —
    :func:`_gated_canary_cache_decision` and ``submit_blocks._assert_canary_verified``
    both read ``base.job_env['HPC_CMD_SHA']`` + ``base.cluster``; minting off the
    same triple is what makes the skip hit (and never mints when there is no
    ``HPC_CMD_SHA`` to key on). Best-effort: ``record_canary_validated`` swallows
    its own write errors and no-ops when the cache is disabled.
    """
    from hpc_agent import __version__ as _pkg_version
    from hpc_agent.state import canary_cache

    cmd_sha = (base.job_env or {}).get("HPC_CMD_SHA") or ""
    if not cmd_sha:
        return
    canary_cache.record_canary_validated(
        canary_cache.canary_cache_key(
            cmd_sha=cmd_sha, version=_pkg_version or "", cluster=base.cluster
        )
    )


def _calibrated_base(
    experiment_dir: Path, base: SubmitFlowSpec, canary_run_id: str | None
) -> SubmitFlowSpec:
    """Return *base* with the array walltime shrunk to the MEASURED canary runtime.

    The two-phase canary measured a full real task; ``verify-canary`` stamped its
    wall-clock onto the canary sidecar (``canary_elapsed_sec``). Before the main
    array launches, size its walltime against that measurement via the ONE
    calibration kernel (shrink-only, never above the approved ceiling —
    :func:`hpc_agent.ops.submit.canary_calibration.calibrate_array_walltime`).

    No-op (returns *base* unchanged) whenever there is nothing to shrink: no
    ``canary_run_id`` (a cache-skipped gate never ran a fresh canary), no stamped
    measurement, no ``resources.walltime_sec`` to tighten, or an MPI job (its
    canary is a shrunk 2-rank probe whose wall-clock is NOT representative of the
    full multi-rank run). Best-effort by contract: calibration is an
    optimization, never a correctness gate.
    """
    from hpc_agent.ops.submit.canary_calibration import calibrate_array_walltime
    from hpc_agent.state.runs import read_canary_elapsed_sec

    resources = base.resources
    if resources is None or resources.walltime_sec is None or resources.mpi is not None:
        return base
    if not canary_run_id:
        return base
    elapsed = read_canary_elapsed_sec(experiment_dir, canary_run_id)
    if elapsed is None:
        return base
    calibration = calibrate_array_walltime(
        canary_elapsed_sec=elapsed,
        requested_walltime_sec=resources.walltime_sec,
    )
    if not calibration.applied or calibration.walltime_sec is None:
        return base
    new_resources = resources.model_copy(update={"walltime_sec": calibration.walltime_sec})
    return base.model_copy(update={"resources": new_resources})


def _launch_main_array(
    experiment_dir: Path, base: SubmitFlowSpec, *, canary_run_id: str | None = None
) -> SubmitFlowResult:
    """Phase-2 of the two-phase gate: launch the main array after a verified canary.

    Extracted so both the fused path (``submit_and_verify`` continuing past the
    canary) and the block-split S3 path (:func:`launch_main_array`) issue the
    IDENTICAL deterministic Phase-2 submit-flow call: canary off, and skip the
    rsync+deploy+preflight Phase 1 already paid (#185/#275/#283). Those skips
    ride internal operator-trusted kwargs — "Phase 1 just deployed this tree" is
    a structural fact the code knows here — never agent-visible spec fields.

    The main-array walltime is calibrated DOWN to the measured canary runtime
    here (:func:`_calibrated_base`), the single seam both launch paths share, so
    the array never requests a padded cold-start ceiling for a task the canary
    proved short. Shrink-only — the approved walltime is the ceiling.
    """
    base = _calibrated_base(experiment_dir, base, canary_run_id)
    return submit_flow(
        experiment_dir,
        spec=base.model_copy(update={"canary": False, "canary_only": False}),
        _skip_preflight=True,
        _skip_rsync_deploy=True,
    )


def _assert_no_post_greenlight_drift(experiment_dir: Path, base: SubmitFlowSpec) -> None:
    """Refuse the S3 main-array launch if the tree drifted since the S2 greenlight.

    The S2→S3 seam has a human review gap: S2 verified a canary against the tree
    as it stood then and recorded that tree's identity onto the run's durable
    per-experiment SIDECAR (``.hpc/runs/<run_id>.json`` — ``tasks_py_sha`` /
    ``executor``). S3 skips rsync+deploy (Phase 1 already shipped that tree), so a
    local edit to ``.hpc/tasks.py`` AFTER the greenlight would silently launch the
    full array on code the canary NEVER verified. This guard closes that gap.

    The journal holds NO main-run record at S3 (the record is minted only when
    ``submit_and_record`` runs, inside the launch we are about to gate), so the
    canary-time baseline is read from the sidecar. Current values are freshly
    derived, mirroring the layer-1 dedup gate (``ops/submit/runner.py``): the
    ``tasks.py`` drift sha is recomputed from ``<experiment_dir>/.hpc/tasks.py``,
    and the current executor is the sidecar's own recorded command (the one S3
    will dispatch — the sidecar is not rewritten between S2 and S3). The
    comparison routes through the single drift predicate
    (:func:`hpc_agent.state.code_drift.detect_code_drift`) — never an inline sha
    compare — whose symmetric rule disables any dimension whose baseline is
    absent (absence ≠ drift), so a missing sidecar / tasks.py just launches.
    """
    import json

    from hpc_agent.state.code_drift import detect_code_drift
    from hpc_agent.state.run_sha import compute_tasks_py_sha
    from hpc_agent.state.runs import read_run_sidecar

    run_id = base.run_id
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError, errors.HpcError):
        # No readable canary-time baseline → cannot prove drift → launch.
        return

    recorded_executor = str(sidecar.get("executor") or "") or None
    recorded_tasks_py_sha = str(sidecar.get("tasks_py_sha") or "") or None

    # Current executor: the sidecar-recorded command S3 dispatches (mirrors
    # layer-1's self-sidecar read). Unchanged between S2 and S3, so this
    # dimension is disabled by the symmetric rule unless the sidecar itself
    # changed — the fireable dimension here is the tasks.py sha below.
    current_executor = recorded_executor
    tasks_py = experiment_dir / ".hpc" / "tasks.py"
    current_tasks_py_sha: str | None = None
    if tasks_py.is_file():
        try:
            current_tasks_py_sha = compute_tasks_py_sha(tasks_py)
        except OSError:
            current_tasks_py_sha = None

    drift = detect_code_drift(
        recorded_executor=recorded_executor,
        recorded_tasks_py_sha=recorded_tasks_py_sha,
        current_executor=current_executor,
        current_tasks_py_sha=current_tasks_py_sha,
    )
    if drift.drifted:
        raise errors.SpecInvalid(
            "tasks.py/executor drifted since the canary greenlight — re-run "
            "submit-s2 so the canary verifies the current tree "
            f"(run_id={run_id!r})."
        )


def launch_main_array(
    experiment_dir: Path,
    *,
    spec: SubmitAndVerifySpec,
    canary_run_id: str | None = None,
    canary_job_ids: list[str] | None = None,
) -> SubmitAndVerifyResult:
    """Launch the main array after a canary was ALREADY verified — the S3 seam.

    The two-phase gate, split across the human boundary (docs/design/
    human-amplification-blocks.md §3): S2 ran ``submit_and_verify(...,
    stop_after_canary=True)`` and handed the human "canary green, est N
    core-hours"; on greenlight, S3 calls this to launch the main array. This
    path does NOT re-verify — the caller asserts the canary passed (that is what
    the human greenlit), so ``verified`` is True. ``canary_run_id`` /
    ``canary_job_ids`` from S2 are threaded onto the result for provenance.

    Before launching, a loud drift guard (:func:`_assert_no_post_greenlight_drift`)
    refuses the launch if ``tasks.py`` drifted since the greenlight — S3 skips
    rsync+deploy, so a post-greenlight local edit would otherwise run the full
    array on code the canary never verified.
    """
    _assert_no_post_greenlight_drift(experiment_dir, spec.submit)
    main_submit = _launch_main_array(experiment_dir, spec.submit, canary_run_id=canary_run_id)
    return SubmitAndVerifyResult(
        run_id=main_submit.run_id,
        job_ids=list(main_submit.job_ids),
        total_tasks=main_submit.total_tasks,
        deduped=main_submit.deduped,
        canary_run_id=canary_run_id,
        canary_job_ids=(list(canary_job_ids) if canary_job_ids else None),
        verified=True,
        failure_kind=None,
        verify_result=None,
    )


@dataclass(frozen=True)
class _GatedCanaryCacheDecision:
    """The gated submit-s2 canary decision against the #249 TTL cache.

    ``skip=True`` → honour the cache: skip the canary, ``reason`` is the mandatory
    disclosure line (fallback-inventory S1), ``validated_age_sec`` the age.
    ``skip=False`` with a ``reason`` → a fresh cache hit was IGNORED by event
    invalidation (an ssh-breaker incident on the host after the validation
    timestamp); ``reason`` is the why-line to disclose while the canary runs
    anyway. A ``None`` decision (see :func:`_gated_canary_cache_decision`) means
    the cache was not consulted / not fresh — the ordinary canary runs, no
    disclosure.
    """

    skip: bool
    reason: str | None
    validated_age_sec: int | None


def _disclose_canary(message: str) -> None:
    """Surface a canary-cache disclosure to the operator (logger + stderr).

    Mirrors ``submit_flow._disclose_smoke``: a degrade that changes freshness
    must SAY so at the moment it degrades (the run-#11 'disclose, don't hide'
    lesson). Used for the event-invalidation 'cache hit ignored' path, where the
    canary runs anyway and there is no skip-result field to carry the why-line.
    """
    import logging
    import sys

    logging.getLogger(__name__).warning(message)
    print(message, file=sys.stderr, flush=True)


def _breaker_incident_after_validation(host: str, validated_at: datetime) -> str | None:
    """READ-ONLY event invalidation: was the host disturbed since *validated_at*?

    The #249 skip trusts a 4h TTL, which is time-only and blind to a cluster that
    DEGRADED inside the window (the S1 blind spot: the key excludes env state).
    Couple — read-only, no writes, no purge plumbing — to the ssh circuit breaker
    (``infra.ssh_circuit``): if the breaker OPENED, or a still-live
    preamble-degradation incident STARTED, on *host* AFTER the cached canary's
    validation, the boot proof is stale and the gated path must re-run the canary.

    Returns a why-line (for disclosure) when such an incident is recorded, else
    ``None`` (honour the cache). Fail-open by breaker doctrine: an absent /
    unreadable state file, or one with no post-validation event, yields ``None``
    — the breaker is a protection layer, never a correctness gate, so its silence
    never blocks the cache decision we'd make without events.
    """
    import json
    import time

    from hpc_agent.infra import ssh_circuit

    try:
        path = ssh_circuit.circuit_state_path(host)
        doc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    validated_epoch = validated_at.timestamp()
    now = time.time()
    opened_at = doc.get("opened_at")
    incident_at = doc.get("incident_started_at")
    candidates: list[tuple[float, str]] = []
    # A breaker currently OPEN whose last open is after the validation — the host
    # started failing connections since we proved the runtime boots.
    if isinstance(opened_at, (int, float)) and float(opened_at) > validated_epoch:
        candidates.append((float(opened_at), "the ssh circuit breaker opened"))
    # A live preamble-degradation incident (module/conda hang livelock) that began
    # after the validation — the exact "env drifted inside the TTL" anatomy.
    if (
        ssh_circuit.is_preamble_degraded(doc, now=now)
        and isinstance(incident_at, (int, float))
        and float(incident_at) > validated_epoch
    ):
        candidates.append((float(incident_at), "a preamble-degradation incident started"))
    if not candidates:
        return None
    event_epoch, what = min(candidates, key=lambda c: c[0])
    from hpc_agent.infra.time import humanize_age_sec

    delay = humanize_age_sec(event_epoch - validated_epoch)
    return (
        f"canary cache hit ignored: {what} on {host} {delay} after validation "
        "— the cluster may have drifted since the boot proof; running the canary"
    )


def _gated_canary_cache_decision(base: SubmitFlowSpec) -> _GatedCanaryCacheDecision | None:
    """The gated submit-s2 canary decision against the #249 TTL cache + events.

    submit-flow's own #249 arm never fires under the two-phase gate: Phase 1
    forces ``canary_only=True``, which ``_canary_decision`` reads as "always
    canary." So the gate re-ran a canary even when this exact
    ``(cmd_sha, version, cluster)`` was canary-validated within the 4h TTL — the
    latency-audit #10 finding. This honours the SAME cache the fused submit-flow
    path does, at the gate, with the mandatory disclosure (fallback-inventory S1)
    AND read-only EVENT invalidation (an ssh-breaker incident since validation
    invalidates the boot proof).

    Returns:

    * ``None`` — run the ordinary canary, no disclosure. The operator forced it
      (``HPC_AGENT_ALWAYS_CANARY`` / ``HPC_NO_CANARY_SKIP``), the spec forced it
      (``force_canary``), the spec carries no ``cmd_sha`` to key on, or the cache
      is absent / stale / expired.
    * ``skip=True`` — honour the cache: skip the canary, ``reason`` = the S1
      disclosure line, ``validated_age_sec`` = the age.
    * ``skip=False`` with a ``reason`` — a fresh hit was IGNORED by a
      post-validation breaker incident; run the canary and disclose the why-line.
    """
    from hpc_agent import __version__ as _pkg_version
    from hpc_agent.infra.time import humanize_age_sec
    from hpc_agent.ops.submit_flow import _always_canary
    from hpc_agent.state import canary_cache

    # Operator/spec forces — reuse the ungated arm's overrides, do not mint new
    # knobs (HPC_NO_CANARY_SKIP is honoured inside canary_cache via cache_disabled).
    if _always_canary() or getattr(base, "force_canary", False):
        return None
    cmd_sha = (base.job_env or {}).get("HPC_CMD_SHA") or ""
    if not cmd_sha or canary_cache.cache_disabled():
        return None
    key = canary_cache.canary_cache_key(
        cmd_sha=cmd_sha, version=_pkg_version or "", cluster=base.cluster
    )
    validated_at = canary_cache.canary_validated_at(key)
    if validated_at is None:
        return None  # absent / stale / expired → run the canary
    age_sec = canary_cache.canary_validated_age_sec(key) or 0

    # Read-only event invalidation (latency-audit #10 refinement): a breaker
    # incident on the host SINCE the validation timestamp rejects the fresh hit.
    host = base.ssh_target.rsplit("@", 1)[-1].strip()
    ignored = _breaker_incident_after_validation(host, validated_at) if host else None
    if ignored is not None:
        return _GatedCanaryCacheDecision(skip=False, reason=ignored, validated_age_sec=age_sec)

    reason = (
        f"canary skipped: cmd_sha {cmd_sha[:8]} validated {humanize_age_sec(age_sec)} "
        f"ago on {base.cluster} (HPC_NO_CANARY_SKIP=1 to force)"
    )
    return _GatedCanaryCacheDecision(skip=True, reason=reason, validated_age_sec=age_sec)


def _gated_cache_skip_result(
    experiment_dir: Path,
    base: SubmitFlowSpec,
    decision: _GatedCanaryCacheDecision,
    *,
    stop_after_canary: bool,
) -> SubmitAndVerifyResult:
    """Build the verified=True result for an honoured #249 gate skip.

    STAGES the tree without a canary — the prelude (rsync+deploy) + sidecar
    mirror still run via a ``canary_only`` submit-flow whose canary decision is
    overridden to skip — so S3's skip-rsync-deploy launch lands on a FRESH tree
    (the cache key excludes remote_path, so the tree is not assumed present). The
    cached validation stands in for a green canary: ``verified=True`` with
    ``canary_run_id=None`` and the disclosure on ``canary_skipped_reason`` — a
    state DISTINCT from a ``canary=false`` opt-out (``verified=False``) and a
    failed canary. For the fused path a canary-free main array launches directly.
    """
    reason = decision.reason
    age = decision.validated_age_sec
    if stop_after_canary:
        # Stage only (prelude + sidecar mirror), no canary, no main — canary_only
        # holds main back; the override skips the probe. S3 launches later.
        staged = submit_flow(
            experiment_dir,
            spec=base.model_copy(update={"canary": True, "canary_only": True}),
            _canary_decision_override=(False, reason),
        )
        return SubmitAndVerifyResult(
            run_id=staged.run_id,
            job_ids=[],
            total_tasks=staged.total_tasks,
            deduped=staged.deduped,
            canary_run_id=None,
            canary_job_ids=None,
            verified=True,
            failure_kind=None,
            verify_result=None,
            canary_skipped_reason=reason,
            validated_age_sec=age,
        )
    # Fused path: canary=false stages AND launches the main array in one call
    # (no canary probe) — the cached validation is the go-ahead.
    main = submit_flow(experiment_dir, spec=base.model_copy(update={"canary": False}))
    return SubmitAndVerifyResult(
        run_id=main.run_id,
        job_ids=list(main.job_ids),
        total_tasks=main.total_tasks,
        deduped=main.deduped,
        canary_run_id=None,
        canary_job_ids=None,
        verified=True,
        failure_kind=None,
        verify_result=None,
        canary_skipped_reason=reason,
        validated_age_sec=age,
    )


@primitive(
    name="submit-and-verify",
    verb="workflow",
    # composes lists the top-level composition ATOMS (submit-flow + verify-canary).
    # The rung-2 canary reducer check also invokes cluster-reduce, but as a
    # conditional best-effort leg (docs/plans/amortized-reduction-check-2026-07-17.md);
    # that cross-subject reach is declared to the layering lint via ROLE_ROOT_ALLOW
    # (scripts/lint_subject_imports.py), the same mechanism verify-canary's own
    # ops/aggregate reach uses — NOT a top-level composition atom.
    composes=["submit-flow", "verify-canary"],
    side_effects=[
        SideEffect("scheduler-submit", "<cluster>"),
        SideEffect("ssh", "<cluster> (canary poll + log scan)"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="submit.run_id",
    cli=CliShape(
        help=(
            "Submit a run plus its canary, then verify the canary lands "
            "before returning. One call instead of /submit-hpc then "
            "/verify-canary. Returns {run_id, job_ids, deduped, "
            "verified, failure_kind, verify_result}."
        ),
        spec_arg=True,
        spec_model=SubmitAndVerifySpec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="submit_and_verify"),
    ),
    agent_facing=True,
)
def submit_and_verify(
    experiment_dir: Path,
    *,
    spec: SubmitAndVerifySpec,
    stop_after_canary: bool = False,
) -> SubmitAndVerifyResult:
    """Two-phase canary gate (#160): submit the canary, verify it, then launch
    the main array ONLY on a verified canary — never before.

    Phase 1 submits the canary alone (``canary_only=True``); on a verified
    canary, Phase 2 submits the main array (``canary=False``). A failed canary
    returns ``verified=False`` with empty ``job_ids`` — the main NEVER launches.

    ``stop_after_canary`` inserts the human boundary of the block decomposition
    (docs/design/human-amplification-blocks.md §3): when True, a VERIFIED canary
    returns immediately with ``verified=True`` and EMPTY ``job_ids`` — the main
    array is NOT launched. The human reviews "canary green, est N core-hours"
    (submit-s2) and, on greenlight, S3 launches the main array via
    :func:`launch_main_array`. The default (False) preserves the fused behavior
    for every existing caller: Phase 1 flows straight into Phase 2 in one call.
    """
    base = spec.submit

    # No canary requested → submit the main array directly; nothing to gate.
    if not base.canary:
        result = submit_flow(experiment_dir, spec=base)
        return SubmitAndVerifyResult(
            run_id=result.run_id,
            job_ids=list(result.job_ids),
            total_tasks=result.total_tasks,
            deduped=result.deduped,
            canary_run_id=result.canary_run_id,
            canary_job_ids=(list(result.canary_job_ids) if result.canary_job_ids else None),
            verified=False,
            failure_kind=None,
            verify_result=None,
        )

    # Latency-audit #10 / fallback-inventory S1: the two-phase GATE must HONOUR
    # the #249 canary TTL cache. Phase 1 forces canary_only=True, which submit-
    # flow reads as "always canary", so the gate re-ran a canary even for a
    # cmd_sha canary-validated within the 4h TTL on this cluster. On a fresh hit
    # (and no post-validation ssh-breaker incident — read-only event
    # invalidation), skip the canary here: the cached validation stands in for a
    # green canary, with the mandatory disclosure + structured age so the skip is
    # never silent and is distinguishable from a canary=false opt-out.
    cache_decision = _gated_canary_cache_decision(base)
    if cache_decision is not None and cache_decision.skip:
        return _gated_cache_skip_result(
            experiment_dir, base, cache_decision, stop_after_canary=stop_after_canary
        )
    if cache_decision is not None and cache_decision.reason:
        # Fresh hit IGNORED by event invalidation — disclose why, run the canary.
        _disclose_canary(cache_decision.reason)

    # Phase 1 — submit ONLY the canary; the main array does NOT launch yet.
    canary_submit = submit_flow(
        experiment_dir, spec=base.model_copy(update={"canary": True, "canary_only": True})
    )

    # Deduped (the run already exists) or no canary landed → don't gate; pass
    # the submit result through without pulling a stale verify.
    if canary_submit.deduped or canary_submit.canary_run_id is None:
        return SubmitAndVerifyResult(
            run_id=canary_submit.run_id,
            job_ids=list(canary_submit.job_ids),
            total_tasks=canary_submit.total_tasks,
            deduped=canary_submit.deduped,
            canary_run_id=canary_submit.canary_run_id,
            canary_job_ids=(
                list(canary_submit.canary_job_ids) if canary_submit.canary_job_ids else None
            ),
            verified=False,
            failure_kind=None,
            verify_result=None,
        )

    # THE DOUBLE CANARY, FIRED CONCURRENTLY (RANK 8, docs/design/
    # determinism-fingerprint.md D-double-canary). Fire the SECOND canary
    # (``<main>-canary2``) NOW — before verifying the first — so both 1-task jobs
    # queue + run in PARALLEL instead of serially: the canary stage costs
    # ``max(two)`` rather than ``2×(queue+run+verify)`` (finding 2). The n=2 diff
    # consumer reads both metrics order-independently, so nothing requires
    # ordering. Its sidecar ships in this leg (finding 7). ``None`` when the double
    # canary is off (``HPC_NO_DOUBLE_CANARY=1``) or the fire failed (degrade to a
    # single canary). A cache-skipped first canary never reaches here (Phase 1
    # always fires a canary_only probe), so "skip skips BOTH executions" holds.
    second_canary_run_id = _fire_second_canary_concurrent(experiment_dir, spec, canary_submit)

    # Verify the canary — THE GATE. #294 PR4: an auto_resume_on_kill run fired a
    # CHECKPOINT canary (HPC_CHECKPOINT_CANARY=1), so verification swaps to the
    # round-trip assertion (a loadable checkpoint survived the kill) instead of
    # the exit-0/output criteria — a preempted canary is the expected outcome.
    verify_result = VerifyCanaryResult.model_validate(
        verify_canary(
            experiment_dir,
            canary_run_id=canary_submit.canary_run_id,
            expect_output=spec.expect_output,
            fingerprint=spec.fingerprint,
            verify_checkpoint=base.auto_resume_on_kill,
            checkpoint_result_dir=spec.checkpoint_result_dir,
            poll_interval_sec=spec.poll_interval_sec,
            wait_budget_sec=spec.wait_budget_sec,
            log_dir=spec.log_dir,
            file_glob=spec.file_glob,
        )
    )
    canary_job_ids = list(canary_submit.canary_job_ids) if canary_submit.canary_job_ids else None

    if not verify_result.ok:
        # Canary failed → refuse to launch the main array (#160). job_ids is
        # empty: the main never went out. Close the canary record as failed so
        # it doesn't linger in_flight and false-flag as a stalled driver (§5).
        _mark_canary_terminal(experiment_dir, canary_submit.canary_run_id, status="failed")
        # The second canary was fired concurrently (RANK 8) before this verdict —
        # on a broken first canary it is now an orphaned probe; close its record so
        # the §5 watchdog doesn't scan it as a live driver. No second verify: the
        # first canary already proved the code broken, so the main stays blocked.
        if second_canary_run_id is not None:
            _mark_canary_terminal(experiment_dir, second_canary_run_id, status="failed")
        return SubmitAndVerifyResult(
            run_id=canary_submit.run_id,
            job_ids=[],
            total_tasks=canary_submit.total_tasks,
            deduped=False,
            canary_run_id=canary_submit.canary_run_id,
            canary_job_ids=canary_job_ids,
            verified=False,
            failure_kind=verify_result.failure_kind,
            verify_result=verify_result,
        )

    # Canary verified → its 1-task job is terminal on the cluster. Close the
    # canary's RunRecord so the §5 watchdog / doctor stop scanning it as a live
    # in_flight run (both the stop_after_canary S2 return and the Phase-2 launch
    # below are reached only past this point, so one call covers both).
    _mark_canary_terminal(experiment_dir, canary_submit.canary_run_id, status="complete")

    # U-ENV1 (reproducibility program): the canary verified ⇒ it ran a real task
    # under the run's env, so resolve that env NOW (pip freeze / lockfile / python
    # -V, over SSH in the canary's activation) and stamp its reduced env_lock_sha
    # on the MAIN run's sidecar. Environment drift (a mutated conda env, a silent
    # package bump) is invisible today — env_hash is captured but never compared;
    # this records the RESOLVED env so a later reproduction under a drifted env is
    # DISCLOSED (never gated, mirroring the data leg). Best-effort + never-raising:
    # an unresolvable env stamps an honest could_not_capture, disclosed at verify.
    # Runs once per verified gate for BOTH the single- and double-canary paths, and
    # before the stop_after_canary S2 return and the fused Phase-2 launch.
    _capture_env_lock_best_effort(
        experiment_dir, run_id=canary_submit.run_id, canary_run_id=canary_submit.canary_run_id
    )

    # THE DOUBLE CANARY, verify half (docs/design/determinism-fingerprint.md,
    # D-double-canary). The first canary verified ok — verify the SECOND canary
    # (fired concurrently above) and mint the n=2 determinism-fingerprint prior
    # from the two executions' task-0 metrics. The second is usually already
    # terminal by now (it queued in parallel with the first), so this is a cheap
    # terminal read, not a fresh queue+run wait. Placed past the first verify and
    # before BOTH the stop_after_canary S2 return and the fused Phase-2 launch, so
    # every fingerprint-minting submit runs it exactly once. A FAILED second canary
    # is a loud nondeterminism finding that blocks the main array exactly like a
    # failed first canary (returns non-None). ``second_canary_run_id is None``
    # (double canary off or fire failed) skips straight to the single-canary path.
    if second_canary_run_id is not None:
        blocked = _verify_second_canary_and_mint(
            experiment_dir, spec, canary_submit, second_canary_run_id
        )
        if blocked is not None:
            return blocked

    # #249 / B7: mint the canary-skip TTL cache ONLY here — past BOTH canary
    # verdicts (a failed FIRST returns at the ``not verify_result.ok`` branch
    # above; a failed SECOND returns the blocking result just above), so the
    # cache reflects the FULL gate. The old mint sat in verify_canary's
    # per-canary success path, so a failed second canary left the first verify's
    # mint standing and a retry inside the 4h TTL cache-skipped both canaries on a
    # cmd_sha that never fully validated. Reached before BOTH the stop_after_canary
    # S2 return and the fused Phase-2 launch, so every verified gate mints once.
    _record_canary_gate_validated(base)

    # Rung 2 of the reducibility ladder (docs/plans/amortized-reduction-check-
    # 2026-07-17.md): the canary verified ⇒ its task-0 output is a GENUINE task
    # artifact. Before the main array launches, EXECUTE the run's declared custom
    # reducer against that ONE real row (the SAME cluster_reduce the final harvest
    # runs) so a broken reducer — py3.8-vs-3.13, a missing import, a wrong output
    # path, non-JSON output — is discovered NOW, at zero critical-path wall-clock
    # (the reduce compute amortizes into the S2→S3 human-review dead time), not
    # hours later mid-harvest after the whole array computed. Placed past BOTH
    # canary verdicts (a failed FIRST/SECOND canary already returned above), once
    # per proceeding submit — the sanctioned "folded into the double-canary block"
    # position (memo §4.2), which avoids a wasted reduce exec on a nondeterminism-
    # blocked run. Best-effort + disclose-never-block: a reducer that RAN and
    # failed is a loud brief disclosure the bare `y` still crosses; a severed check
    # is `unverified`, never a pass; a run with no custom reducer SKIPS (byte-
    # identical to before).
    reducer_check = _check_reducer_on_canary(experiment_dir, base, canary_submit.canary_run_id)

    # Canary verified. The block boundary (submit-s2): STOP before the main
    # array so the human can review "canary green, est N core-hours" and
    # greenlight (§3). ``verified=True`` but ``job_ids`` is empty — the main did
    # NOT launch; S3 launches it post-greenlight via ``launch_main_array``.
    if stop_after_canary:
        return SubmitAndVerifyResult(
            run_id=canary_submit.run_id,
            job_ids=[],
            total_tasks=canary_submit.total_tasks,
            deduped=False,
            canary_run_id=canary_submit.canary_run_id,
            canary_job_ids=canary_job_ids,
            verified=True,
            failure_kind=None,
            verify_result=verify_result,
            reducer_check=reducer_check,
        )

    # Phase 2 — canary verified → launch the main array. The deterministic
    # Phase-2 flips (#279, mirrored by the prepare-phase2-spec primitive): no
    # canary, launch main, and skip the rsync+deploy Phase 1 already did (#185).
    # No ``skip_preflight`` as a spec field — preflight is operator-gated now
    # (#275 Fix 2); Phase 1's probe plus the #255 TTL cache cover the re-check.
    # The same deterministic call backs the block-split S3 path, so both share
    # ``_launch_main_array`` (and its measured-canary walltime calibration).
    main_submit = _launch_main_array(
        experiment_dir, base, canary_run_id=canary_submit.canary_run_id
    )
    return SubmitAndVerifyResult(
        run_id=main_submit.run_id,
        job_ids=list(main_submit.job_ids),
        total_tasks=main_submit.total_tasks,
        deduped=main_submit.deduped,
        canary_run_id=canary_submit.canary_run_id,
        canary_job_ids=canary_job_ids,
        verified=True,
        failure_kind=None,
        verify_result=verify_result,
        reducer_check=reducer_check,
    )
