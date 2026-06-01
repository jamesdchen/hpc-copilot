"""Spawn-prompt rendering, report parsing, and shared spawn-contract helpers.

The workflow slash commands trigger a workflow that runs in a
fresh-context worker. The prompt that worker runs on must be
*deterministic*: it depends only on on-disk state and the invocation's
mutable fields, never on whatever rotted in the parent conversation.

The prompt is not hand-typed by an LLM: the code-orchestrated
entrypoints (``hpc-agent run`` and ``hpc-campaign-driver``) build a
structured request — ``{workflow, experiment_dir, fields}`` — and call
:func:`validate_and_render_parts` to render the canonical text. The
worker returns a structured :class:`WorkerReport`, parsed back by
:func:`parse_worker_report`.

This module is the single import surface for every consumer of the
spawn contract: the contract data (registry, request model, decision
points, report model) is re-exported from
:mod:`hpc_agent._wire.spawn_contract`, and the logic over it
lives here. A consumer imports these; it does not re-declare them.
"""

from __future__ import annotations

import contextlib
import functools
import json
from typing import Any

from pydantic import ValidationError

from hpc_agent._kernel.lifecycle.invoke import RenderedPrompt
from hpc_agent._wire.spawn_contract import (
    DECISION_POINTS,
    SPAWN_KEY,
    WORKFLOW_PROCEDURES,
    DecisionPoint,
    SpawnRequest,
    WorkerDecision,
    WorkerReport,
    WorkflowName,
)

__all__ = [
    "DECISION_POINTS",
    "SPAWN_KEY",
    "WORKFLOW_PROCEDURES",
    "DecisionPoint",
    "RenderedPrompt",
    "SpawnContractError",
    "SpawnRequest",
    "WorkerDecision",
    "WorkerReport",
    "WorkflowName",
    "parse_worker_report",
    "render_spawn_parts",
    "render_spawn_prompt",
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
def _procedure_body(workflow: str) -> str:
    """Return the worker-prompt body for *workflow* as inert text.

    Resolution order: every plugin's ``worker_prompt_assets`` tree
    (first plugin to provide ``<workflow>.md`` wins) is checked before
    the host's ``hpc_agent._kernel.extension.worker_prompts`` package data. This is what
    lets a plugin ship an overriding procedure
    that the worker actually sees.

    Cached: procedure text is process-stable (plugin set cannot change
    in-process). Tests that swap plugins call ``cache_clear()``.

    The worker prompt *inlines* this rather than telling the worker to
    invoke the Skill tool: a headless ``claude -p`` worker has no skill
    discovery (``--bare`` skips it, and headless mode does not support
    user-invoked skills), so the procedure must travel inside the
    prompt itself. The directory name ``worker_prompts/`` reflects
    that — these are not skills. See
    ``docs/internals/skill-policy.md``.
    """
    from hpc_agent._kernel.extension.worker_prompts import read_procedure
    from hpc_agent._kernel.registry.plugins import plugin_worker_prompt_roots

    for root in plugin_worker_prompt_roots():
        candidate = root / f"{workflow}.md"
        if candidate.is_file():
            text: str = candidate.read_text(encoding="utf-8")
            return text.strip()
    return read_procedure(workflow).strip()


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
    (scaffold + inlined procedure + return contract) is byte-identical
    for every run of *workflow*; the ``variable_suffix`` carries this
    invocation's experiment_dir and fields. The split is what lets an
    invoker prompt-cache the large prefix — see :class:`RenderedPrompt`.
    """
    procedure = WORKFLOW_PROCEDURES[workflow]
    cacheable_prefix = (
        f"You are an isolated hpc-agent subagent executing the `{workflow}` "
        "workflow. Your context is fresh — depend only on on-disk state and "
        "the invocation context at the end of this prompt, never on any "
        "prior conversation.\n\n"
        f"Execute the `{procedure}` procedure below exactly as written — it "
        "is the canonical procedure for this workflow. The invocation context "
        "at the end of this prompt names an `experiment_dir`: make it your "
        "working directory before anything else (`cd` into it) and resolve "
        "every relative path the procedure uses (`.hpc/...`, a bare "
        "`--experiment-dir .`, `Path.cwd()`) against it. Do NOT assume the "
        "process started in that directory; it may not have. Then run "
        "`hpc-agent load-context --experiment-dir <experiment_dir>` (the same "
        "value) and treat its data as the source of truth.\n\n"
        "The procedure below is the canonical sequence to follow. Read it "
        "with these standing adjustments:\n"
        '- Anything attributed to "the slash command" or "the caller" '
        "(parsing the user's request, rendering prompts, running a "
        "sub-interview) has already happened; its results are in the "
        "invocation context at the end of this prompt. Never wait for a "
        "slash command.\n"
        "- A reference to a *primitive* (`submit-flow`, `plan-throughput`, "
        "a `docs/...` link to one) is fetchable: `hpc-agent describe "
        "<name>` prints its contract and `hpc-agent <primitive> --help` "
        "its exact CLI. Fetch what the branch you are on needs and follow "
        "it inline.\n"
        "- A reference handing off to another *workflow* — submit, status, "
        "aggregate, campaign — is a boundary, not your job: that workflow "
        "is a separate run the caller starts next. Stop, and record it as "
        "the next step in `decisions` / `anomalies`. Never run another "
        "workflow inside this one.\n"
        "- A reference to a helper skill that needs interactive user "
        "confirmation (e.g. `hpc-classify-axis`, executor scaffolding) "
        "cannot be completed headless — record it in `decisions` / "
        "`anomalies` and stop at that boundary for the caller to handle.\n"
        "- When in doubt, escalate — do not improvise. If you hit anything you "
        "cannot resolve deterministically by following the procedure (an "
        "ambiguous choice, missing input, an unexpected error you cannot fix "
        "from the steps as written), do NOT guess, retry blindly, or invent a "
        "workaround. Record what blocked you in `decisions` / `anomalies` and "
        "stop for the caller — a clean escalation is always preferred over a "
        "speculative action.\n"
        "- Work tersely and in parallel. Lead with actions, not narration; "
        "skip preamble and restatements of what tool output already shows. "
        "When steps have no data dependency — multiple reads, independent "
        "`hpc-agent describe`/`--help` lookups, separate greps — issue them in "
        "one parallel tool batch rather than serially.\n"
        "- If the procedure advises delegating verbose steps to a "
        "fresh-context subagent, ignore that advice — you are already the "
        "delegated worker. Run every step yourself in this context; do "
        "not spawn further subagents.\n"
        "- Where it says to surface or prompt something to the user, you "
        "have no interactive user — put it in the returned JSON instead.\n\n"
        f"=== BEGIN {procedure} PROCEDURE ===\n"
        f"{_procedure_body(procedure)}\n"
        f"=== END {procedure} PROCEDURE ===\n\n"
        "When the workflow is complete, return ONLY a single JSON object as "
        'your final message: {"result": <the procedure\'s result envelope>, '
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
    prompt string is needed (the ``delegate.prompt`` field of
    ``load-context``). Byte-identical output for byte-identical inputs.
    """
    return render_spawn_parts(
        workflow=workflow, experiment_dir=experiment_dir, fields=fields
    ).joined


def _validated_request(payload: Any) -> SpawnRequest:
    """Validate *payload* as a :class:`SpawnRequest`, or raise SpawnContractError."""
    try:
        request: SpawnRequest = SpawnRequest.model_validate(payload)
    except ValidationError as exc:
        raise SpawnContractError(str(exc)) from exc
    return request


def validate_and_render_parts(payload: Any) -> RenderedPrompt:
    """Validate a spawn-request *payload* and return the split prompt.

    For the code-orchestrated path (``hpc-agent run`` and
    ``hpc-campaign-driver`` → an invoker that prompt-caches the prefix).
    Raises :class:`SpawnContractError` on an invalid payload.
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
