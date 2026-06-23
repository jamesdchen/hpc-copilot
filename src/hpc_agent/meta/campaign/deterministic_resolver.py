"""A code-only :data:`JudgementResolver` for the campaign loop (#220 Phase 1).

The neutral tick-loop (:mod:`hpc_agent._kernel.lifecycle.drive`) executes a
judgement (``kind == "agent"``) step through an injected
:data:`JudgementResolver`. The default one
(:func:`hpc_agent._kernel.lifecycle.drive.default_judgement_resolver`) spawns a
fresh-context LLM worker (``claude -p`` via ``run_workflow``). This module is
an **alternate** resolver that executes the same judgement steps *in code* by
chaining the EXISTING deterministic primitives — so a campaign can advance
its ``decide`` / cold-``submit`` steps with **zero worker / LLM spawn** when
the common path is fully ``decided_by="code"``.

It is a NEW, injectable artifact. It does NOT replace the default resolver:
a caller opts in by constructing it and injecting it into
``CampaignLoopConfig(resolver=...)`` — see :func:`deterministic_campaign_config`.
Phase 2 (#220) decides whether/when to make it the default.

The chain (verified against the on-disk primitives, not the prose hypothesis)
=============================================================================

For the campaign ``decide`` step
(``spawn_request.workflow == "campaign"``, ``fields.step == "decide"``):

1. ``classify-campaign-path`` (`.hpc/tasks.py`) — manual grid vs strategy.
   A confident hit is ``decided_by="code"``; an unparseable / unrecognized
   tasks.py escalates (``decided_by="judgement"``) → **residue**.
2. ``campaign-advance`` — the deterministic decision ladder. ``continue`` is
   the only branch that submits the next iteration; every ``stop_*`` /
   ``wait_in_flight`` is a *decided* clean terminal (NOT residue — the loop
   correctly stops), reported and exited 0.
3. On ``continue``: reconstruct the next iteration's submit context from the
   prior run's sidecar config snapshot (cluster / profile / remote_path /
   executor / result_dir_template / resources / runtime — the values
   ``build-submit-spec`` + ``write-run-sidecar`` need), then
   ``resolve-submit-inputs`` builds + validates the submit-flow spec and
   writes the per-run sidecar. A ``needs_decision`` outcome
   (``prior_run_found`` / ``needs_scaffold_interview``) is **residue**.
4. Submit the built spec via the injected *submit_fn* (the only cluster I/O —
   stubbed in tests, so no SSH / qsub), then ``advance_cursor``.

For the cold ``submit`` step
(``spawn_request.workflow == "submit"``): the same reconstruct →
``resolve-submit-inputs`` → submit → (no cursor) chain, but a cold first
submission of a fresh experiment that has no prior run sidecar to rebuild the
context from is genuine judgement (the executor-discovery / axis-classification
interview) the headless resolver cannot run — it escalates as **residue**.

Residue policy: **halt-and-park, never guess.**
================================================

When a backing primitive escalates (``classify-campaign-path`` →
``unclassifiable``, ``resolve-submit-inputs`` → ``needs_decision``,
``submit-pipeline`` → a gate failure), the resolver does NOT improvise. It
surfaces the :class:`Escalation`-as-data in the synthesized
:class:`WorkerReport` (in ``anomalies`` plus a deterministic decision entry)
and returns a NON-ZERO exit code so the neutral loop stops cleanly. This
mirrors #240's "code resolves it, or park" posture — the deterministic layer
handles the common ``code`` path and hands the genuine judgement back rather
than blindly proceeding.

This module lives in ``meta.campaign`` because the dependency points
campaign -> drive (``drive.py`` MUST NOT import campaign). It is the campaign
*caller* supplying a policy to the neutral mechanism, exactly as
``driver.py`` supplies the step table + default resolver.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from hpc_agent._kernel.extension.spawn_prompt import (
    WorkerDecision,
    WorkerReport,
    parse_worker_report,
)

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._kernel.lifecycle.drive import JudgementResolver
    from hpc_agent._wire._shared import BackendName
    from hpc_agent.meta.campaign.driver import CampaignLoopConfig

__all__ = [
    "SubmitFn",
    "DeterministicCampaignResolver",
    "deterministic_campaign_config",
]

# How the resolver actually submits a built submit-flow spec. Takes the
# ``experiment_dir`` and the validated ``submit_flow.input.json`` dict that
# ``resolve-submit-inputs`` produced; returns the submit result as a dict
# (the ``SubmitFlowResult.to_envelope_data()`` shape: run_id / job_ids / ...).
# Injected so the cluster I/O (SSH / rsync / qsub) is a seam a test can stub
# without standing up a scheduler — the resolver itself never shells out to a
# cluster.
SubmitFn = Callable[["Path", dict[str, Any]], dict[str, Any]]

# Exit codes the resolver returns. 0 = the step advanced (or cleanly stopped);
# non-zero = halt-and-park (residue surfaced, loop should stop). Distinct codes
# let an operator/test tell a clean stop from a parked escalation.
_EXIT_OK = 0
_EXIT_RESIDUE = 3


def _dispatch_primitive(name: str, /, *args: Any, **kwargs: Any) -> Any:
    """Invoke an ``ops`` primitive by its registry name.

    ``meta.campaign`` must not import ``ops`` internals directly — the
    subject-import boundary (``scripts/lint_subject_imports.py``) keeps subjects
    composing only through the shared ``infra``/``state``/``_kernel`` substrate.
    The resolver therefore dispatches the ops primitives it chains *by name*
    through the kernel registry — the same by-name dispatch the headless loop
    uses for CLI verbs, run in-process. ``register_primitives`` is idempotent.
    """
    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives

    register_primitives()
    return get_registry()[name].func(*args, **kwargs)


def _default_submit_fn(experiment_dir: Path, submit_spec: dict[str, Any]) -> dict[str, Any]:
    """Submit a built spec through the real ``submit-flow`` pipeline.

    The production seam: validate the ``resolve-submit-inputs`` ``submit_spec``
    dict into a :class:`SubmitFlowSpec` and run ``submit-flow`` (rsync + deploy
    + canary + qsub + record). Tests inject a stub in its place so no SSH /
    scheduler is touched.
    """
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

    spec = SubmitFlowSpec.model_validate(submit_spec)
    result = _dispatch_primitive("submit-flow", experiment_dir, spec=spec)
    return cast("dict[str, Any]", result.to_envelope_data())


class DeterministicCampaignResolver:
    """A code-only campaign :data:`JudgementResolver` (#220 Phase 1).

    Constructed with the cluster-I/O seam (*submit_fn*, defaulting to the real
    ``submit-flow``) and injected into ``CampaignLoopConfig(resolver=...)``. The
    instance is callable with the :data:`JudgementResolver` signature
    ``(spawn_request, experiment_dir) -> (WorkerReport, exit_code)``.
    """

    def __init__(self, *, submit_fn: SubmitFn | None = None) -> None:
        self._submit_fn: SubmitFn = submit_fn or _default_submit_fn

    # -- JudgementResolver entry point -------------------------------------

    def __call__(
        self, spawn_request: dict[str, Any], experiment_dir: Path
    ) -> tuple[WorkerReport, int]:
        """Resolve a judgement step in code; return ``(report, exit_code)``.

        Dispatches on the spawn request's ``workflow`` (and, for campaign,
        ``fields.step``) to the matching in-code chain. An unrecognized step
        is residue — the resolver only orchestrates the two ``agent`` steps a
        campaign loop emits (``submit`` cold, ``campaign``/``decide`` next).
        """
        workflow = spawn_request.get("workflow")
        fields = spawn_request.get("fields") or {}

        if workflow == "campaign" and fields.get("step") == "decide":
            campaign_id = fields.get("campaign_id")
            if not campaign_id:
                return self._residue(
                    "campaign",
                    point="decide",
                    outcome="missing_campaign_id",
                    detail="decide spawn_request carried no campaign_id; cannot advance.",
                )
            resolved = fields.get("resolved") if isinstance(fields.get("resolved"), dict) else {}
            return self._resolve_decide(experiment_dir, campaign_id, resolved=resolved or {})

        if workflow == "submit":
            return self._resolve_cold_submit(experiment_dir)

        return self._residue(
            workflow if isinstance(workflow, str) else "campaign",
            point="decide",
            outcome="unsupported_step",
            detail=(
                f"deterministic resolver does not orchestrate workflow={workflow!r} "
                f"step={fields.get('step')!r}; only campaign/decide and cold submit "
                "are code-resolvable. Spawn an agent worker for this step."
            ),
        )

    # -- decide chain ------------------------------------------------------

    def _resolve_decide(
        self,
        experiment_dir: Path,
        campaign_id: str,
        *,
        resolved: dict[str, Any] | None = None,
    ) -> tuple[WorkerReport, int]:
        """``classify-campaign-path`` -> ``campaign-advance`` -> (continue) submit."""
        from hpc_agent.incorporation.classify_campaign_path import classify_campaign_path
        from hpc_agent.meta.campaign.atoms.advance import campaign_advance

        # 1. Classify the path. An unclassifiable tasks.py escalates -> residue,
        #    UNLESS the caller pre-resolved the judgement via the
        #    ``fields.resolved`` channel (an LlmJudgementResolver adjudication,
        #    or any orchestrator that answered the park). The code
        #    classification always wins when it is confident — the hint exists
        #    only to break the tie the AST scan could not.
        resolved = resolved or {}
        source_path = str(experiment_dir / ".hpc" / "tasks.py")
        path_res = classify_campaign_path(source_path=source_path)
        if path_res["decided_by"] == "judgement":
            hint = resolved.get("path")
            if hint in ("manual", "strategy"):
                path_res = {
                    "path": hint,
                    "reason": (
                        f"classify-campaign-path escalated ({path_res['reason']}); "
                        f"resolved {hint!r} by the caller's fields.resolved adjudication."
                    ),
                }
            else:
                return self._residue(
                    "campaign",
                    point="path",
                    outcome="unclassifiable",
                    detail=(
                        f"classify-campaign-path could not resolve {source_path}: "
                        f"{path_res['reason']}; candidates={path_res['candidates']}. "
                        "Manual grid vs strategy is genuine judgement — parked."
                    ),
                )

        path_decision = WorkerDecision(
            point="path",
            outcome=path_res["path"],
            why=path_res["reason"],
        )

        # 2. Advance: the deterministic decision ladder.
        advance = campaign_advance(experiment_dir=experiment_dir, campaign_id=campaign_id)
        decision = advance["decision"]
        advance_decision = WorkerDecision(
            point="decide",
            outcome=decision,
            why=advance["reason"],
        )

        # A non-`continue` decision is a DECIDED clean terminal, not residue: the
        # loop is correctly stopping / waiting. Report it and exit 0.
        if decision != "continue":
            return self._ok_report(
                "campaign",
                decisions=[path_decision, advance_decision],
                result={"decision": decision, "campaign_id": campaign_id},
                anomalies=(
                    f"campaign-advance decided {decision!r}: {advance['reason']} "
                    "No next iteration submitted (this is the deterministic stop "
                    "ladder, not an escalation)."
                ),
            )

        # 3+4. continue -> build + submit the next iteration.
        return self._submit_next_iteration(
            experiment_dir,
            campaign_id=campaign_id,
            extra_decisions=[path_decision, advance_decision],
        )

    # -- cold submit chain -------------------------------------------------

    def _resolve_cold_submit(self, experiment_dir: Path) -> tuple[WorkerReport, int]:
        """Cold first submit: rebuild context from a prior run, else escalate."""
        return self._submit_next_iteration(
            experiment_dir, campaign_id=None, extra_decisions=[], workflow="submit"
        )

    # -- the shared continue->submit spine ---------------------------------

    def _submit_next_iteration(
        self,
        experiment_dir: Path,
        *,
        campaign_id: str | None,
        extra_decisions: list[WorkerDecision],
        workflow: str = "campaign",
    ) -> tuple[WorkerReport, int]:
        """Reconstruct the submit context, ``resolve-submit-inputs``, submit, advance.

        The next-iteration config (cluster / profile / remote_path / executor /
        result_dir_template / resources / runtime) is reused from the prior run
        sidecar's config snapshot — the same values a campaign re-submits each
        iteration; only the swept params (driven by tasks.py) change. When no
        prior sidecar exists to rebuild from, this is the cold-start interview
        the headless resolver cannot run -> residue.
        """
        context = self._reconstruct_submit_context(experiment_dir, campaign_id=campaign_id)
        if context is None:
            return self._residue(
                workflow,
                point="decide" if workflow == "campaign" else "axis_class",
                outcome="needs_interview",
                detail=(
                    "no prior run sidecar to rebuild the submit context from; the "
                    "next iteration's cluster / executor / axes come from an "
                    "interview (executor-discovery + axis-classification) that is "
                    "genuine judgement the headless resolver cannot run — parked."
                ),
            )

        spec, run_name = context
        resolved = _dispatch_primitive("resolve-submit-inputs", experiment_dir, spec=spec)
        if resolved.needs_decision:
            return self._residue(
                workflow,
                point="decide" if workflow == "campaign" else "prior_run",
                outcome=resolved.stage_reached,
                detail=(
                    f"resolve-submit-inputs escalated ({resolved.stage_reached}): "
                    f"{resolved.reason} — parked, not guessed."
                ),
            )

        # `resolved` carries the built + validated submit-flow spec; the per-run
        # sidecar is already written (#171). Submit via the injected seam.
        assert resolved.submit_spec is not None  # the `resolved` terminal guarantees it
        submit_result = self._submit_fn(experiment_dir, resolved.submit_spec)

        result_run_id = submit_result.get("run_id", resolved.run_id)
        decisions = list(extra_decisions)

        # Advance the campaign cursor only on the campaign path (the cold submit
        # may not be campaign-tagged).
        cursor_iteration: int | None = None
        if campaign_id:
            from hpc_agent.meta.campaign.cursor import advance_cursor

            cursor = advance_cursor(
                experiment_dir, campaign_id, last_run_id=str(result_run_id or "")
            )
            cursor_iteration = cursor.get("iteration")

        return self._ok_report(
            workflow,
            decisions=decisions,
            result={
                "run_id": result_run_id,
                "job_ids": submit_result.get("job_ids", []),
                "deduped": submit_result.get("deduped", False),
                "campaign_id": campaign_id,
                "cursor_iteration": cursor_iteration,
                "submitted": True,
            },
            anomalies="",
        )

    def _reconstruct_submit_context(
        self, experiment_dir: Path, *, campaign_id: str | None
    ) -> tuple[Any, str] | None:
        """Rebuild a :class:`ResolveSubmitInputsSpec` from the prior run.

        Returns ``(spec, run_name)`` or ``None`` when no prior run with the full
        cluster contract exists (the cold-start case -> residue). The config is
        sourced from the prior run's two records, each authoritative for its
        half: the **journal record** carries ``ssh_target`` / ``profile`` /
        ``cluster`` / ``remote_path`` (the submit-target identity), and the
        **sidecar** carries the v2 config snapshot (``executor`` /
        ``result_dir_template`` / ``resources`` / ``env`` / ``runtime``). The
        ``run_id`` / ``cmd_sha`` on the inner specs are placeholders;
        ``resolve-submit-inputs`` overrides them with the freshly-computed
        values, so the next iteration gets its own run_id off the re-imported
        tasks.py.
        """
        from hpc_agent import errors
        from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
        from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
        from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec
        from hpc_agent.state.journal import load_run
        from hpc_agent.state.runs import find_existing_runs, read_run_sidecar

        # The prior run: prefer the most recent run OF THIS campaign so the
        # rebuilt context matches the campaign's own cluster/profile.
        prior_run_id: str | None = None
        if campaign_id:
            from hpc_agent.state.index import find_runs_by_campaign

            campaign_runs = find_runs_by_campaign(experiment_dir, campaign_id)
            if campaign_runs:
                prior_run_id = campaign_runs[-1].run_id  # oldest-first -> newest last
        if prior_run_id is None:
            runs = find_existing_runs(experiment_dir)
            if runs:
                prior_run_id = runs[0].stem  # newest-first
        if prior_run_id is None:
            return None

        record = load_run(experiment_dir, prior_run_id)
        try:
            sidecar = read_run_sidecar(experiment_dir, prior_run_id)
        except (FileNotFoundError, OSError, ValueError, errors.HpcError):
            sidecar = None
        if record is None or sidecar is None:
            return None

        executor = sidecar.get("executor")
        result_dir_template = sidecar.get("result_dir_template")
        # ssh_target lives on the journal record (the submit-target identity),
        # not the sidecar config snapshot. Fall back to deriving it from the
        # cluster config when the record omits it.
        ssh_target = record.ssh_target or self._ssh_target_for(record.cluster)
        cluster = record.cluster or sidecar.get("cluster")
        profile = record.profile or sidecar.get("profile")
        remote_path = record.remote_path or sidecar.get("remote_path")
        # The reconstruct is only sound when the prior carried the cluster
        # contract a submit needs. Anything missing means we'd be guessing —
        # treat as cold-start residue rather than fabricate.
        required = (executor, result_dir_template, cluster, profile, remote_path, ssh_target)
        if not all(required):
            return None

        run_name = str(profile)
        placeholder_run_id = f"{run_name}-00000000"
        placeholder_sha = "0" * 64
        # total_tasks is the prior run's; the next iteration re-imports tasks.py
        # which may resize it, but build-submit-spec only needs a valid ge=1
        # seed (compute-run-id re-derives the real count from the task list).
        task_count = int(record.total_tasks or sidecar.get("task_count") or 1) or 1

        # Env-activation: the prior sidecar's ``env`` snapshot carries the
        # modules / conda_source / conda_env the run activated (the same keys
        # ``infra.clusters`` reads back off a sidecar). build-submit-spec's
        # resolve_activation REQUIRES at least one, so thread them through —
        # reusing the prior run's activation, the soundest source, rather than
        # re-deriving from clusters.yaml (which a headless box may not carry).
        env_snapshot = sidecar.get("env") or {}
        modules = env_snapshot.get("modules") if isinstance(env_snapshot, dict) else None
        conda_source = env_snapshot.get("conda_source") if isinstance(env_snapshot, dict) else None
        conda_env = env_snapshot.get("conda_env") if isinstance(env_snapshot, dict) else None

        build_submit = BuildSubmitSpecInput(
            profile=run_name,
            cluster=str(cluster),
            ssh_target=str(ssh_target),
            remote_path=str(remote_path),
            run_id=placeholder_run_id,
            cmd_sha=placeholder_sha,
            total_tasks=task_count,
            backend=self._backend_for(record, sidecar),
            runtime=sidecar.get("runtime"),
            campaign_id=campaign_id,
            result_dir_template=str(result_dir_template),
            modules=modules,
            conda_source=conda_source,
            conda_env=conda_env,
        )
        sidecar_spec = WriteRunSidecarInput(
            run_id=placeholder_run_id,
            cmd_sha=placeholder_sha,
            executor=str(executor),
            result_dir_template=str(result_dir_template),
            task_count=task_count,
            cluster=str(cluster),
            profile=run_name,
            campaign_id=campaign_id,
            remote_path=str(remote_path),
            resources=sidecar.get("resources") or None,
            env=sidecar.get("env") or None,
            env_group=sidecar.get("env_group") or None,
            runtime=sidecar.get("runtime"),
        )
        spec = ResolveSubmitInputsSpec(
            run_name=run_name,
            submit=build_submit,
            sidecar=sidecar_spec,
        )
        return spec, run_name

    @staticmethod
    def _ssh_target_for(cluster: str | None) -> str | None:
        """Derive ``ssh_target`` from the cluster config (the record's fallback)."""
        if not cluster:
            return None
        from hpc_agent.infra.clusters import load_clusters_config

        cfg = load_clusters_config().get(cluster)
        if not isinstance(cfg, dict):
            return None
        target = cfg.get("ssh_target")
        return str(target) if target else None

    @staticmethod
    def _backend_for(record: Any, sidecar: dict[str, Any]) -> BackendName:
        """Pick the scheduler backend the prior run used.

        Neither the journal record nor the sidecar config snapshot records the
        backend directly; resolve it from the cluster config the same way
        submit-flow does, falling back to slurm. An unregistered scheduler
        string falls back to slurm rather than shipping a backend the submit
        spec's ``BackendName`` validator would reject downstream — and the
        accepted set is the live registry (built-ins + plugin backends), not a
        frozen list (#337).
        """
        from hpc_agent.infra.backends import registered_backend_names
        from hpc_agent.infra.clusters import load_clusters_config

        cluster = record.cluster or sidecar.get("cluster")
        cfg = load_clusters_config().get(cluster, {}) if cluster else {}
        backend = cfg.get("scheduler") if isinstance(cfg, dict) else None
        resolved = str(backend) if backend else "slurm"
        if resolved not in registered_backend_names():
            resolved = "slurm"
        return cast("BackendName", resolved)

    # -- report synthesis --------------------------------------------------

    def _ok_report(
        self,
        workflow: str,
        *,
        decisions: list[WorkerDecision],
        result: dict[str, Any],
        anomalies: str,
    ) -> tuple[WorkerReport, int]:
        """Build a valid success report (round-trips through ``parse_worker_report``)."""
        report = WorkerReport(result=result, decisions=decisions, anomalies=anomalies)
        return self._validated(report, workflow), _EXIT_OK

    def _residue(
        self,
        workflow: str,
        *,
        point: str,
        outcome: str,
        detail: str,
    ) -> tuple[WorkerReport, int]:
        """Build a halt-and-park report surfacing the escalation as data.

        The escalation rides in ``anomalies`` (the free-form residue channel)
        and on a deterministic decision entry (``decided_by="code"`` points,
        so no ``why`` is required). Returns a NON-ZERO exit so the neutral loop
        stops cleanly rather than proceeding past an unresolved judgement.
        """
        report = WorkerReport(
            result={"residue": True, "point": point, "outcome": outcome},
            decisions=[WorkerDecision(point=point, outcome=outcome, why=detail)],
            anomalies=f"ESCALATION (parked, not guessed): {detail}",
        )
        return self._validated(report, workflow), _EXIT_RESIDUE

    @staticmethod
    def _validated(report: WorkerReport, workflow: str) -> WorkerReport:
        """Round-trip the synthesized report through ``parse_worker_report``.

        The same validation the LLM path's reports pass: it asserts every
        decision ``point`` is enumerated for *workflow* and that judgement
        points carry a non-empty ``why``. The deterministic resolver reports
        only ``code``-decided points, so this is a self-check that the
        synthesized report is contract-valid before it leaves the resolver.
        """
        import json

        return parse_worker_report(json.dumps(report.model_dump(mode="json")), workflow=workflow)


def deterministic_campaign_config(*, submit_fn: SubmitFn | None = None) -> CampaignLoopConfig:
    """A :class:`CampaignLoopConfig` wired to the deterministic resolver.

    The opt-in entry point for #220 Phase 1: a caller passes the returned
    config to ``hpc_agent.meta.campaign.driver.main(config=...)`` (or builds
    ``drive_once`` directly) to drive a campaign with **no LLM spawn** on the
    common ``decided_by="code"`` path. The default ``step_table`` (the
    monitor/aggregate cli map) is unchanged; only the judgement *resolver*
    seam is swapped. Phase 2 decides whether this becomes the default.
    """
    from hpc_agent.meta.campaign.driver import CampaignLoopConfig

    resolver: JudgementResolver = DeterministicCampaignResolver(submit_fn=submit_fn)
    return CampaignLoopConfig(resolver=resolver)
