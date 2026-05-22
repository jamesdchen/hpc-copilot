"""Canonical subagent prompt for a delegated workflow.

The four workflow slash commands (``/submit-hpc``, ``/monitor-hpc``,
``/aggregate-hpc``, ``/campaign-hpc``) delegate their skill to a
fresh-context subagent. The prompt that subagent runs on must be
*deterministic* — it depends only on on-disk state and the invocation's
mutable fields, never on whatever rotted in the parent conversation.

The main agent cannot be trusted to type that prompt verbatim into the
``Task`` tool: it is an LLM composing a call, free to append, prepend,
or paraphrase. So the prompt is never authored at the call site. The
agent passes a small structured request — ``{"hpc_spawn": {workflow,
experiment_dir, fields}}`` — and the ``spawn_guard`` PreToolUse hook
calls :func:`render_spawn_prompt` to replace it with the canonical text
before the spawn runs. The agent's only influence is the *workflow*
(constrained to four values) and the *fields* data; the prompt
scaffold around them is code.
"""

from __future__ import annotations

import json
from typing import Any

# workflow id → skill name the subagent invokes via the Skill tool.
WORKFLOW_SKILLS: dict[str, str] = {
    "submit": "hpc-submit",
    "status": "hpc-status",
    "aggregate": "hpc-aggregate",
    "campaign": "hpc-campaign",
}


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
        "Return ONLY the skill's result envelope plus a free-text `anomalies` "
        "field. Keep verbose intermediate output — discovery transcripts, "
        "scheduler dumps, rsync logs — out of your final message; it stays in "
        "your context, not the caller's."
    )
