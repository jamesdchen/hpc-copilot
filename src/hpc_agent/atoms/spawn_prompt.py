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
import functools
import json
import re
from importlib.resources import files
from typing import Any

from pydantic import ValidationError

from hpc_agent._internal.invoke import RenderedPrompt
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
    "RenderedPrompt",
    "SpawnContractError",
    "SpawnRequest",
    "WorkerDecision",
    "WorkerReport",
    "WorkflowName",
    "extract_spawn_payload",
    "is_unpinned_workflow_directive",
    "parse_worker_report",
    "render_spawn_parts",
    "render_spawn_prompt",
    "validate_and_render",
    "validate_and_render_parts",
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


@functools.cache
def _skill_body(skill: str) -> str:
    """The markdown body of a bundled ``SKILL.md``, frontmatter stripped.

    Read from the installed ``slash_commands`` package data and cached.
    The worker prompt *inlines* this rather than telling the worker to
    invoke the Skill tool: a headless ``claude -p`` worker has no skill
    discovery (``--bare`` skips it, and headless mode does not support
    user-invoked skills), so the procedure must travel inside the
    prompt itself.
    """
    raw = (files("slash_commands") / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    if raw.startswith("---"):
        close = raw.find("\n---", 3)
        if close != -1:
            raw = raw[close + 4 :]
    return raw.strip()


# Splits the cacheable per-workflow prefix from the per-invocation
# suffix. Everything before this marker — scaffold, inlined skill body,
# return contract — is byte-identical for every run of the same
# workflow; only what follows (experiment_dir, fields) varies. Keeping
# the variable parts strictly last is what lets the large fixed prefix
# be prompt-cached. test_render_prefix_is_stable_across_invocations
# guards the byte-identity.
_SUFFIX_MARKER = "─── invocation context ───"


def render_spawn_parts(
    *, workflow: str, experiment_dir: str, fields: dict[str, Any]
) -> RenderedPrompt:
    """Render the worker prompt split into cacheable + variable parts.

    Deterministic given the installed package. The ``cacheable_prefix``
    (scaffold + inlined skill + return contract) is byte-identical for
    every run of *workflow*; the ``variable_suffix`` carries this
    invocation's experiment_dir and fields. The split is what lets an
    invoker prompt-cache the large prefix — see :class:`RenderedPrompt`.
    """
    skill = WORKFLOW_SKILLS[workflow]
    cacheable_prefix = (
        f"You are an isolated hpc-agent subagent executing the `{workflow}` "
        "workflow. Your context is fresh — depend only on on-disk state and "
        "the invocation context at the end of this prompt, never on any "
        "prior conversation.\n\n"
        f"Execute the `{skill}` skill below exactly as written — it is the "
        "canonical procedure for this workflow. Before you begin, run "
        "`hpc-agent load-context --experiment-dir <experiment_dir>` (the "
        "value is in the invocation context below) and treat its data as "
        "the source of truth.\n\n"
        "The skill below is the canonical procedure, but it is written for "
        "an interactive, slash-command-fronted session — which you are "
        "not. Read it with these standing adjustments:\n"
        '- Anything it attributes to "the slash command" (parsing the '
        "user's request, rendering prompts, running a sub-interview) has "
        "already happened; its results are in the invocation context at "
        "the end of this prompt. Never wait for a slash command.\n"
        "- Where it says to hand off to or invoke another skill, you "
        "cannot — you have no Skill tool. If the workflow genuinely needs "
        "another workflow, do not attempt it: record it in `decisions` / "
        "`anomalies` and stop at that boundary for the caller to handle.\n"
        "- Ignore `docs/...` and `../...` links — they are repo-internal "
        "and unreadable from here; the skill text itself carries what you "
        "need.\n"
        "- Where it says to surface or prompt something to the user, you "
        "have no interactive user — put it in the returned JSON instead.\n\n"
        f"=== BEGIN {skill} SKILL ===\n"
        f"{_skill_body(skill)}\n"
        f"=== END {skill} SKILL ===\n\n"
        "When the workflow is complete, return ONLY a single JSON object as "
        'your final message: {"result": <the skill\'s result envelope>, '
        '"decisions": [...], "anomalies": "<free text, or empty>"}. Each '
        '`decisions` entry is {"point": "<id>", "outcome": "<what you '
        'decided>", "why": "<the deciding input>"}; record one per decision '
        f"point you reach. The `{workflow}` workflow's decision points:\n"
        f"{_render_decision_points(workflow)}\n\n"
        "Keep verbose intermediate output — discovery transcripts, scheduler "
        "dumps, rsync logs — out of that object; it stays in your context, "
        "not the caller's."
    )
    variable_suffix = (
        f"{_SUFFIX_MARKER}\n"
        f"experiment_dir: {experiment_dir}\n"
        f"invocation inputs:\n{_render_fields(fields)}"
    )
    return RenderedPrompt(cacheable_prefix=cacheable_prefix, variable_suffix=variable_suffix)


def render_spawn_prompt(*, workflow: str, experiment_dir: str, fields: dict[str, Any]) -> str:
    """Render the canonical worker prompt for *workflow* as one string.

    The joined form of :func:`render_spawn_parts` — used where a single
    prompt string is needed (the ``Task`` tool, the ``delegate.prompt``
    field). Byte-identical output for byte-identical inputs.
    """
    return render_spawn_parts(
        workflow=workflow, experiment_dir=experiment_dir, fields=fields
    ).joined


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


def _validated_request(payload: Any) -> SpawnRequest:
    """Validate *payload* as a :class:`SpawnRequest`, or raise SpawnContractError."""
    try:
        request: SpawnRequest = SpawnRequest.model_validate(payload)
    except ValidationError as exc:
        raise SpawnContractError(str(exc)) from exc
    return request


def validate_and_render(payload: Any) -> str:
    """Validate a spawn-request *payload* and return the joined prompt string.

    For the ``Task``-tool path (the spawn_guard hook), which needs a
    single string. Raises :class:`SpawnContractError` on an invalid
    payload.
    """
    request = _validated_request(payload)
    return render_spawn_prompt(
        workflow=request.workflow,
        experiment_dir=request.experiment_dir,
        fields=request.fields,
    )


def validate_and_render_parts(payload: Any) -> RenderedPrompt:
    """Validate a spawn-request *payload* and return the split prompt.

    For the code-orchestrated path (``hpc-agent run`` → an invoker that
    prompt-caches the prefix). Raises :class:`SpawnContractError` on an
    invalid payload — validation never forks from
    :func:`validate_and_render`.
    """
    request = _validated_request(payload)
    return render_spawn_parts(
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
