"""Spawn-prompt rendering, report parsing, and shared spawn-contract helpers.

The four workflow slash commands (``/submit-hpc``, ``/monitor-hpc``,
``/aggregate-hpc``, ``/campaign-hpc``) delegate a skill to a
fresh-context subagent. The prompt that subagent runs on must be
*deterministic*: it depends only on on-disk state and the invocation's
mutable fields, never on whatever rotted in the parent conversation.

The agent is not trusted to type that prompt: it is an LLM composing a
call. Instead it passes a structured request — ``{"hpc_spawn":
{workflow, experiment_dir, fields}}`` — and the consumer calls
:func:`validate_and_render` to replace it with the canonical text. The
worker returns a structured :class:`WorkerReport`, parsed back by
:func:`parse_worker_report`.

This module is the single import surface for every consumer of the
spawn contract: the contract data (registry, request model, decision
points, report model) is re-exported from
:mod:`hpc_agent._schema_models.spawn_contract`, and the logic over it
lives here. A consumer imports these; it does not re-declare them.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from pydantic import ValidationError

from hpc_agent._schema_models.spawn_contract import (
    DECISION_POINTS,
    SPAWN_KEY,
    WORKFLOW_SKILLS,
    DecisionPoint,
    SpawnRequest,
    WorkerDecision,
    WorkerReport,
    WorkflowName,
)

__all__ = [
    "DECISION_POINTS",
    "SPAWN_KEY",
    "WORKFLOW_SKILLS",
    "DecisionPoint",
    "SpawnContractError",
    "SpawnRequest",
    "WorkerDecision",
    "WorkerReport",
    "WorkflowName",
    "extract_spawn_payload",
    "is_unpinned_workflow_directive",
    "parse_worker_report",
    "render_spawn_prompt",
    "validate_and_render",
]


class SpawnContractError(ValueError):
    """A spawn request or worker report violated the shared contract."""


def _render_fields(fields: dict[str, Any]) -> str:
    """Render the invocation fields as a fenced JSON block.

    Going through ``json.dumps`` is load-bearing, not cosmetic: it
    escapes newlines and control characters inside string values, so a
    field value cannot break out of the data block and inject fake
    prompt structure (a fabricated "Return ONLY ..." line, say).
    """
    if not fields:
        return "(none — run the skill's own discovery / interview steps)"
    return "```json\n" + json.dumps(fields, indent=2, sort_keys=True) + "\n```"


def _render_decision_points(workflow: str) -> str:
    points = DECISION_POINTS.get(workflow, ())
    if not points:
        return "(none enumerated)"
    return "\n".join(f"- `{p.id}` ({p.kind})" for p in points)


def render_spawn_prompt(*, workflow: str, experiment_dir: str, fields: dict[str, Any]) -> str:
    """Render the canonical subagent prompt for *workflow*.

    Pure function of its inputs — the same ``(workflow, experiment_dir,
    fields)`` always yields byte-identical output.
    """
    skill = WORKFLOW_SKILLS[workflow]
    return (
        f"You are an isolated hpc-agent subagent executing the `{workflow}` "
        "workflow. Your context is fresh and you must keep it that way: depend "
        "only on on-disk state and the invocation inputs below, never on any "
        "prior conversation.\n\n"
        f"1. Bootstrap: run `hpc-agent load-context --experiment-dir "
        f"{experiment_dir}` and read the result.\n"
        f"2. Invoke the `{skill}` skill (skills/{skill}/SKILL.md) via the "
        "Skill tool and execute its workflow exactly — the skill is the "
        "canonical source of truth for the call sequence.\n"
        "3. Apply the invocation inputs below as you run the skill.\n\n"
        "Invocation inputs:\n"
        f"{_render_fields(fields)}\n\n"
        "Return ONLY a single JSON object as your final message, nothing "
        'else: {"result": <the skill\'s result envelope>, "decisions": '
        '[...], "anomalies": "<free text, or empty>"}.\n\n'
        'Each `decisions` entry is {"point": "<id>", "outcome": "<what you '
        'decided>", "why": "<the deciding input>"}. Record one entry per '
        f"decision point you reach. The `{workflow}` workflow's decision "
        f"points are:\n{_render_decision_points(workflow)}\n\n"
        "Keep verbose intermediate output — discovery transcripts, scheduler "
        "dumps, rsync logs — out of this object; it stays in your context, "
        "not the caller's."
    )


# The directive grammar is built from the registry — no second spelling
# of the workflow set. A non-request prompt that imperatively invokes a
# workflow skill (verb + "the" + the skill name + "skill") is an
# unpinned workflow run; a mere mention ("summarize the hpc-submit
# skill") is not a directive and is left alone.
_WORKFLOW_DIRECTIVE_RE = re.compile(
    r"\b(?:invoke|run|execute)\s+the\s+[`*]?(?:"
    + "|".join(re.escape(skill) for skill in WORKFLOW_SKILLS.values())
    + r")[`*]?\s+skill\b",
    re.IGNORECASE,
)


def is_unpinned_workflow_directive(prompt: str) -> bool:
    """True if *prompt* imperatively invokes a workflow skill in prose."""
    return _WORKFLOW_DIRECTIVE_RE.search(prompt) is not None


def extract_spawn_payload(prompt: str) -> tuple[bool, Any]:
    """``(is_request, payload)`` — parse a Task prompt as a spawn request.

    ``is_request`` is False when *prompt* is not an ``{"hpc_spawn": ...}``
    JSON object at all (an ordinary subagent prompt). When True,
    ``payload`` is the unvalidated request body for
    :func:`validate_and_render`.
    """
    stripped = prompt.strip()
    if not stripped.startswith("{"):
        return (False, None)
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return (False, None)
    if not isinstance(obj, dict) or SPAWN_KEY not in obj:
        return (False, None)
    return (True, obj[SPAWN_KEY])


def validate_and_render(payload: Any) -> str:
    """Validate a spawn-request *payload* and return the canonical prompt.

    Raises :class:`SpawnContractError` when *payload* is not a valid
    :class:`SpawnRequest`. This is the one entry point every consumer
    calls — validation and rendering never fork.
    """
    try:
        request = SpawnRequest.model_validate(payload)
    except ValidationError as exc:
        raise SpawnContractError(str(exc)) from exc
    return render_spawn_prompt(
        workflow=request.workflow,
        experiment_dir=request.experiment_dir,
        fields=request.fields,
    )


def _last_json_object(text: str) -> dict[str, Any] | None:
    """Return the last top-level JSON object in *text*, or ``None``.

    Tries a whole-string parse first (the worker is told to emit only
    the object); falls back to the last balanced ``{...}`` span so a
    worker that prefixes chatter still parses.
    """
    stripped = text.strip()
    with contextlib.suppress(json.JSONDecodeError):
        whole = json.loads(stripped)
        if isinstance(whole, dict):
            return whole
    depth = 0
    start = -1
    last: str | None = None
    for i, char in enumerate(stripped):
        if char == "{":
            if depth == 0:
                start = i
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                last = stripped[start : i + 1]
    if last is None:
        return None
    with contextlib.suppress(json.JSONDecodeError):
        obj = json.loads(last)
        if isinstance(obj, dict):
            return obj
    return None


def parse_worker_report(output: str, *, workflow: str) -> WorkerReport:
    """Parse a delegated worker's final JSON object into a :class:`WorkerReport`.

    Raises :class:`SpawnContractError` when no JSON object is found, the
    object fails :class:`WorkerReport` validation, or a decision names a
    ``point`` not enumerated in :data:`DECISION_POINTS` for *workflow*.
    """
    obj = _last_json_object(output)
    if obj is None:
        raise SpawnContractError("no JSON object found in worker output")
    try:
        report: WorkerReport = WorkerReport.model_validate(obj)
    except ValidationError as exc:
        raise SpawnContractError(str(exc)) from exc
    known = {point.id for point in DECISION_POINTS.get(workflow, ())}
    unknown = sorted({d.point for d in report.decisions if d.point not in known})
    if unknown:
        raise SpawnContractError(
            f"worker reported decision point(s) not defined for {workflow!r}: "
            f"{unknown}; known: {sorted(known)}"
        )
    return report
