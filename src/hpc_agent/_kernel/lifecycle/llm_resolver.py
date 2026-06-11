"""LLM-at-the-residue :data:`JudgementResolver` — code decides, an LLM adjudicates parks.

The two existing resolvers for the headless tick-loop
(:mod:`hpc_agent._kernel.lifecycle.drive`) are the extremes:

* :func:`~hpc_agent._kernel.lifecycle.drive.default_judgement_resolver`
  spawns a whole fresh-context agent worker for every judgement step —
  maximal capability, maximal cost, control flow inside the LLM.
* A code resolver (e.g. ``meta.campaign.DeterministicCampaignResolver``)
  runs the same steps with zero LLM spawn and **halts-and-parks** the
  moment a backing primitive escalates (its *residue*).

:class:`LlmJudgementResolver` is the middle: it wraps any inner code
resolver and, when the inner parks, makes **one bounded
:func:`~hpc_agent._kernel.lifecycle.structured.structured` call** to
adjudicate the residue against a *closed menu* of candidate outcomes —
then feeds the choice back to the inner resolver and retries. Control
flow stays in code; the LLM is consulted exactly at the typed judgement
points, picks from a menu the caller authored, and its rationale is
recorded as a contract-valid :class:`WorkerDecision` (judgement points
require a non-empty ``why`` — ``parse_worker_report`` enforces it).

Mechanism vs policy (the ``drive.py`` doctrine, one level up)
=============================================================

This class owns only the protocol: run inner → on residue, adjudicate →
apply → retry, bounded, never looping on no progress, parking on any
doubt. Everything domain-shaped is injected by the caller:

* ``menu`` — which residue points are adjudicable at all, and the closed
  candidate list for each. A residue point absent from the menu parks
  immediately with **no LLM call** (genuine interviews — cold-start
  context, credentials — are not menu-shaped and must stay parked).
* ``apply_decision`` — how a chosen outcome feeds back into the next
  inner attempt. The default writes it into
  ``spawn_request["fields"]["resolved"][<point>]`` — the same
  pre-resolved-values channel skills use — which an inner resolver may
  honor (``DeterministicCampaignResolver`` honors ``resolved["path"]``)
  or ignore, in which case the no-progress guard parks on the repeat
  residue rather than spinning.
* ``model`` — any :class:`~hpc_agent._kernel.lifecycle.structured.ChatModel`
  (``get_model("openai-compat")``, or a stub in tests).

``"park"`` is always offered and always honored: the adjudicator may
conclude the escalation is real, and that verdict (with its rationale)
is itself worth recording.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent import errors
from hpc_agent._kernel.extension.spawn_prompt import (
    WorkerDecision,
    WorkerReport,
    parse_worker_report,
)
from hpc_agent._kernel.lifecycle.structured import ChatMessage, structured

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._kernel.lifecycle.drive import JudgementResolver
    from hpc_agent._kernel.lifecycle.structured import ChatModel

__all__ = ["PARK", "ResidueAdjudication", "LlmJudgementResolver", "default_apply_decision"]

# The always-offered, always-honored "the escalation is real" verdict.
PARK = "park"

# Exit code a code resolver returns on halt-and-park (the residue
# convention ``DeterministicCampaignResolver`` established). The wrapper
# treats any OTHER exit code as pass-through.
_EXIT_RESIDUE = 3


class ResidueAdjudication(BaseModel):
    """The structured() target for one residue adjudication."""

    model_config = ConfigDict(extra="forbid", title="residue adjudication")

    chosen: str = Field(description="Exactly one of the offered candidate outcomes (verbatim).")
    why: str = Field(
        min_length=1,
        description=(
            "The rationale for the choice — recorded verbatim on the "
            "WorkerDecision audit trail (judgement points require it)."
        ),
    )


def default_apply_decision(
    point: str, chosen: str, spawn_request: dict[str, Any]
) -> dict[str, Any]:
    """Feed *chosen* back via ``fields.resolved`` — the pre-resolved-values channel.

    Returns a NEW spawn_request (the input is never mutated) whose
    ``fields["resolved"][point]`` carries the adjudicated outcome, for the
    inner resolver's next attempt to honor. An inner resolver that ignores
    the channel re-emits the same residue and the wrapper's no-progress
    guard parks — feeding back is safe even against an oblivious inner.
    """
    updated = dict(spawn_request)
    fields = dict(updated.get("fields") or {})
    resolved = dict(fields.get("resolved") or {})
    resolved[point] = chosen
    fields["resolved"] = resolved
    updated["fields"] = fields
    return updated


_SYSTEM_PROMPT = """\
You adjudicate one parked decision in an autonomous HPC campaign loop.
The deterministic layer handled everything it could and escalated this
single point. Choose exactly one of the offered candidate outcomes —
verbatim — and give your rationale. Choose "park" when the evidence is
insufficient or the escalation should reach a human. Reply with ONLY a
JSON object: {"chosen": "<candidate>", "why": "<rationale>"}.
"""


class LlmJudgementResolver:
    """Wrap a code resolver; adjudicate its residues with one-shot LLM calls.

    Callable with the :data:`JudgementResolver` signature
    ``(spawn_request, experiment_dir) -> (WorkerReport, exit_code)``, so it
    injects anywhere the inner resolver did (``CampaignLoopConfig``,
    ``drive_once(resolver=...)``).
    """

    def __init__(
        self,
        *,
        inner: JudgementResolver,
        model: ChatModel,
        menu: Mapping[str, Sequence[str]],
        apply_decision: Callable[[str, str, dict[str, Any]], dict[str, Any] | None] | None = None,
        max_rounds: int = 2,
        max_repairs: int = 2,
    ) -> None:
        self._inner = inner
        self._model = model
        self._menu = {point: list(candidates) for point, candidates in menu.items()}
        self._apply = apply_decision or default_apply_decision
        self._max_rounds = max(1, int(max_rounds))
        self._max_repairs = int(max_repairs)

    # -- JudgementResolver entry point --------------------------------------

    def __call__(
        self, spawn_request: dict[str, Any], experiment_dir: Path
    ) -> tuple[WorkerReport, int]:
        workflow = spawn_request.get("workflow")
        workflow = workflow if isinstance(workflow, str) else "campaign"
        adjudications: list[WorkerDecision] = []
        seen_residues: set[tuple[str, str]] = set()
        request = spawn_request

        for _ in range(self._max_rounds + 1):
            report, exit_code = self._inner(request, experiment_dir)
            if exit_code != _EXIT_RESIDUE:
                # Pass-through (success or a non-residue failure) — attach the
                # adjudication audit trail so the tick record shows how the
                # park was resolved.
                return self._with_decisions(report, adjudications, workflow), exit_code

            point = str(report.result.get("point") or "")
            outcome = str(report.result.get("outcome") or "")
            detail = report.anomalies or (report.decisions[-1].why if report.decisions else "")

            candidates = self._menu.get(point)
            if candidates is None:
                # Not menu-shaped (a genuine interview / credentials park):
                # no LLM call, the inner's park stands.
                return self._with_decisions(report, adjudications, workflow), exit_code
            if (point, outcome) in seen_residues:
                # No progress: the inner re-emitted the residue the decision
                # was supposed to resolve. Park rather than spin (and spend).
                return self._parked(
                    report,
                    adjudications,
                    workflow,
                    note=(
                        f"adjudicated {point!r} but the inner resolver re-emitted the "
                        "same residue (decision not honored or insufficient) — parked."
                    ),
                )
            seen_residues.add((point, outcome))

            decision = self._adjudicate(
                workflow=workflow,
                request=request,
                point=point,
                outcome=outcome,
                detail=detail,
                candidates=candidates,
            )
            if decision is None:
                return self._parked(
                    report,
                    adjudications,
                    workflow,
                    note=f"adjudication for {point!r} failed structured validation — parked.",
                )

            offered = [*candidates, PARK] if PARK not in candidates else list(candidates)
            adjudications.append(
                WorkerDecision(
                    point=point,
                    outcome=decision.chosen,
                    why=decision.why,
                    chosen=decision.chosen,
                    rejected=[c for c in offered if c != decision.chosen],
                )
            )
            if decision.chosen == PARK:
                return self._parked(
                    report,
                    adjudications,
                    workflow,
                    note=f"adjudicator chose to park {point!r}: {decision.why}",
                )

            applied = self._apply(point, decision.chosen, request)
            if applied is None:
                return self._parked(
                    report,
                    adjudications,
                    workflow,
                    note=f"no apply path for {point!r}={decision.chosen!r} — parked.",
                )
            request = applied

        return self._parked(
            report,
            adjudications,
            workflow,
            note=f"adjudication round budget ({self._max_rounds}) exhausted — parked.",
        )

    # -- the one-shot LLM call ----------------------------------------------

    def _adjudicate(
        self,
        *,
        workflow: str,
        request: dict[str, Any],
        point: str,
        outcome: str,
        detail: str,
        candidates: Sequence[str],
    ) -> ResidueAdjudication | None:
        offered = [*candidates, PARK] if PARK not in candidates else list(candidates)

        def _member(instance: Any) -> None:
            if instance.chosen not in offered:
                raise ValueError(
                    f"chosen must be one of {offered!r} (verbatim), got {instance.chosen!r}"
                )

        evidence = {
            "workflow": workflow,
            "fields": request.get("fields") or {},
            "residue": {"point": point, "outcome": outcome, "detail": detail},
            "candidates": offered,
        }
        messages = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content=json.dumps(evidence, sort_keys=True, default=str)),
        ]
        try:
            result: ResidueAdjudication = structured(
                self._model,
                ResidueAdjudication,
                messages,
                max_repairs=self._max_repairs,
                post_validate=_member,
            )
        except errors.StructuredOutputError:
            return None
        return result

    # -- report synthesis ----------------------------------------------------

    def _with_decisions(
        self, report: WorkerReport, extra: list[WorkerDecision], workflow: str
    ) -> WorkerReport:
        if not extra:
            return report
        merged = WorkerReport(
            result=report.result,
            decisions=[*report.decisions, *extra],
            anomalies=report.anomalies,
        )
        return self._validated(merged, workflow)

    def _parked(
        self,
        report: WorkerReport,
        adjudications: list[WorkerDecision],
        workflow: str,
        *,
        note: str,
    ) -> tuple[WorkerReport, int]:
        anomalies = f"{report.anomalies} | {note}" if report.anomalies else note
        merged = WorkerReport(
            result=report.result,
            decisions=[*report.decisions, *adjudications],
            anomalies=anomalies,
        )
        return self._validated(merged, workflow), _EXIT_RESIDUE

    @staticmethod
    def _validated(report: WorkerReport, workflow: str) -> WorkerReport:
        """Round-trip through ``parse_worker_report`` — the same self-check the
        deterministic resolver applies: every decision point must be enumerated
        for *workflow* and judgement points must carry a non-empty ``why``.

        A resolver's contract is to RETURN ``(report, exit_code)``, never to
        raise — so a contract violation (a third-party inner emitting an
        unenumerated point, or a menu keyed on a point invalid for this
        workflow) is annotated on the report instead of crashing the tick
        loop; the caller's exit code (a park, for every path that can get
        here with bad decisions) stands.
        """
        from hpc_agent._kernel.extension.spawn_prompt import SpawnContractError

        try:
            return parse_worker_report(
                json.dumps(report.model_dump(mode="json")), workflow=workflow
            )
        except SpawnContractError as exc:
            note = f"WORKER-REPORT CONTRACT VIOLATION (returned unvalidated): {exc}"
            anomalies = f"{report.anomalies} | {note}" if report.anomalies else note
            return WorkerReport(
                result=report.result, decisions=report.decisions, anomalies=anomalies
            )
