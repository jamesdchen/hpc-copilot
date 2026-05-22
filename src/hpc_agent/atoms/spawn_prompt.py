"""``build-spawn-prompt`` — content-addressed subagent spawn specs.

The four workflow slash commands (``/submit-hpc``, ``/monitor-hpc``,
``/aggregate-hpc``, ``/campaign-hpc``) delegate their skill to a
fresh-context subagent. The prompt that subagent runs on must be
*deterministic* — it depends only on on-disk state and the invocation's
mutable fields, never on whatever rotted in the parent conversation.

The main agent cannot be trusted to type that prompt verbatim into the
``Task`` tool: it is an LLM composing a call, free to append, prepend,
or paraphrase. So the prompt is never authored at the call site. It is
*generated here* (code), written to ``.hpc/spawn/<sha256>.json``, and
the agent passes only a ``spec://<sha256>`` reference. A ``PreToolUse``
hook (``hpc_agent.hooks.spawn_guard``) resolves that reference back to
the canonical prompt before the spawn runs — see that module.

The filename *is* the SHA-256 of the file's exact bytes, so the hook's
integrity check is a one-liner: re-hash the file, compare to the stem.
Any edited byte breaks the match and the spawn is denied.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# workflow id → skill name the subagent invokes via the Skill tool.
WORKFLOW_SKILLS: dict[str, str] = {
    "submit": "hpc-submit",
    "status": "hpc-status",
    "aggregate": "hpc-aggregate",
    "campaign": "hpc-campaign",
}


def _render_fields(fields: dict[str, Any]) -> str:
    if not fields:
        return "(none — run the skill's own discovery / interview steps)"
    lines = []
    for key in sorted(fields):
        value = fields[key]
        rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        lines.append(f"- {key}: {rendered}")
    return "\n".join(lines)


def render_spawn_prompt(
    *, workflow: str, experiment_dir: str, fields: dict[str, Any]
) -> str:
    """Render the canonical subagent prompt for *workflow*.

    Pure function of its inputs — the same ``(workflow, experiment_dir,
    fields)`` always yields byte-identical output, which is what makes
    the content-addressed hash stable.
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


def build_spawn_prompt(
    *, experiment_dir: Path, workflow: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Render the spawn prompt, persist it, and return its reference.

    Writes ``<experiment_dir>/.hpc/spawn/<sha256>.json`` and returns
    ``{workflow, spawn_ref, spec_path, sha256}``. ``spawn_ref`` is the
    only value the agent passes to the ``Task`` tool.
    """
    if workflow not in WORKFLOW_SKILLS:
        raise ValueError(
            f"unknown workflow {workflow!r}; expected one of "
            f"{sorted(WORKFLOW_SKILLS)}"
        )

    prompt = render_spawn_prompt(
        workflow=workflow, experiment_dir=str(experiment_dir), fields=fields
    )
    # The file content is canonical JSON; the filename is its SHA-256.
    record = {"fields": fields, "prompt": prompt, "workflow": workflow}
    content = json.dumps(record, sort_keys=True, separators=(",", ":"))
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

    spawn_dir = experiment_dir / ".hpc" / "spawn"
    spawn_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spawn_dir / f"{sha}.json"
    spec_path.write_text(content, encoding="utf-8")

    return {
        "workflow": workflow,
        "spawn_ref": f"spec://{sha}",
        "spec_path": str(spec_path),
        "sha256": sha,
    }
