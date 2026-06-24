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

import functools
import json
from typing import Any

from pydantic import ValidationError

from hpc_agent._kernel.contract.json_extract import last_json_object
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
    judgement_point_ids,
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
    invoke a skill: a headless worker has no skill discovery (under
    Claude Code ``claude -p --bare`` skips it, and headless mode does
    not support user-invoked skills; the other harness drivers likewise
    spawn a bare, skill-less worker), so the procedure must travel
    inside the prompt itself. The directory name ``worker_prompts/``
    reflects that — these are not skills. See
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
        f"You are an isolated hpc-agent worker for the `{workflow}` workflow. "
        "Fresh context — use on-disk state + the invocation context below; "
        "ignore any prior conversation.\n\n"
        f"Execute the `{procedure}` procedure verbatim. First `cd` to the "
        "`experiment_dir` named in the invocation context — resolve all "
        "relative paths (`.hpc/...`, `Path.cwd()`) against it; do NOT assume "
        "the process started there. Then `hpc-agent load-context "
        "--experiment-dir <experiment_dir>` and treat its data as truth.\n\n"
        "Standing adjustments to the procedure below:\n"
        "- Caller-side work (slash parsing, sub-interviews) already happened — "
        "results are in the invocation context. Never wait for a slash command.\n"
        "- Primitive references (`submit-flow`, `plan-throughput`) are "
        "fetchable: `hpc-agent describe <name>` + `hpc-agent <primitive> "
        "--help`. Fetch and follow inline.\n"
        "- A reference to another workflow (submit/status/aggregate/campaign) "
        "is a boundary, not your job — stop, record in "
        "`decisions`/`anomalies`.\n"
        "- Helper skills needing interactive confirmation can't run headless. "
        "No interactive user, ever — surface everything in the returned JSON.\n"
        "- Escalate over improvise. If you hit an ambiguous choice, missing "
        "input, or unfixable error — record what blocked you in "
        "`decisions`/`anomalies` and stop. Do not guess, retry blindly, or "
        "spawn further subagents.\n"
        "- Work tersely and in parallel — 'parallel' means parallel TOOL "
        "CALLS: multiple reads / `describe` / greps with no data dependency → "
        "multiple tool-call blocks in ONE message (the harness runs them "
        "concurrently), NOT shell-level concurrency in one Bash call (`cmd1 & "
        "cmd2 & wait`, `parallel`, `xargs -P`).\n\n"
        f"=== BEGIN {procedure} PROCEDURE ===\n"
        f"{_procedure_body(procedure)}\n"
        f"=== END {procedure} PROCEDURE ===\n\n"
        "Return ONLY a single JSON as your final message: "
        '`{"result": <procedure\'s result envelope>, "decisions": [...], '
        '"anomalies": "<text or empty>"}`. Each `decisions` entry: '
        '`{"point": "<id>", "outcome": "...", "why": "..."}` — one per '
        f"decision point reached. `{workflow}` workflow's decision points:\n"
        f"{_render_decision_points(workflow)}\n\n"
        "Keep verbose intermediate output (discovery, scheduler dumps, rsync "
        "logs) out of the JSON — it stays in your context."
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


def parse_worker_report(output: str, *, workflow: str) -> WorkerReport:
    """Parse a delegated worker's final JSON object into a :class:`WorkerReport`.

    Raises :class:`SpawnContractError` when no JSON object is found, the
    object fails :class:`WorkerReport` validation, or a decision names a
    ``point`` not enumerated in :data:`DECISION_POINTS` for *workflow*.
    """
    obj = last_json_object(output)
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
    # A judgement point is a genuine break in control flow the deterministic
    # layer could not decide; its rationale is the thing worth capturing
    # (spawn_contract.DecisionPoint). Reject an empty ``why`` there — a branch
    # taken with no recorded reason is a bug to surface, not a state to accept.
    # Deterministic (code/plan) points are exempt: their backing primitive's
    # envelope is the authoritative on-disk record.
    judgement = judgement_point_ids(workflow)
    missing_why = sorted(
        {d.point for d in report.decisions if d.point in judgement and not d.why.strip()}
    )
    if missing_why:
        raise SpawnContractError(
            f"judgement decision point(s) {missing_why} for {workflow!r} must record "
            "a non-empty 'why' — the rationale for a genuine control-flow branch is "
            "the thing worth capturing (deterministic points are exempt)"
        )
    return report
