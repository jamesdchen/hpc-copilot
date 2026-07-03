"""Single in-code source of truth for the four HPC workflow entries (§6).

``docs/design/block-drive.md`` §6 — surface consolidation. The endpoint is
**one canonical entry per workflow** (the workflow name + its thin
start-the-driver instruction, in the registry) with the typed block tools as
the substrate the driver composes. Both the Claude-Code slash command AND the
MCP prompt are *projections* of this one table, not independently authored
lists:

* the MCP prompt **name list** and its **start instruction** are sourced here
  (see :data:`hpc_agent._kernel.extension.mcp_server._PROMPT_NAMES` and
  :meth:`~hpc_agent._kernel.extension.mcp_server.McpServer.get_prompt`, which
  still read the packaged ``slash_commands/commands/<name>.md`` for the human
  description body but derive the *set* of prompts from this table);
* the ``block-drive`` verb (the code-driven chain) is what every entry's
  ``start_instruction`` invokes — the LLM no longer reads ``next_block`` and
  dispatches the next verb itself; code drives the chain and the LLM only
  translates at decision points.

``next_block`` is re-homed from an LLM affordance to the driver's internal
chaining table; ``first_block`` here is only the *entry* the driver starts at.

TODO(wave4): project the slash command bodies from this table in the
surface-deletion pass. Today the hand-authored
``src/slash_commands/commands/<slash_name>.md`` files remain the projected
slash body — deleting/auto-generating them now risks breaking the install and
``scripts/lint_skill_command_sync.py``. This table is the SoT that pass will
project from; the slash bodies are the second copy that lint exists to police.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowEntry:
    """One canonical workflow entry — the SoT the slash + MCP prompt project.

    Fields:

    * ``name`` — the canonical workflow name (registry key for the entry).
    * ``prompt_name`` — the MCP prompt name; equals the slash command stem
      (the ``.md`` basename under ``slash_commands/commands/``).
    * ``slash_name`` — the Claude-Code slash command stem (same value today;
      kept a distinct field so a future harness that renames one surface does
      not silently couple the two).
    * ``skill_name`` — the paired workflow skill id under
      ``slash_commands/skills/`` (the relay-loop prose).
    * ``first_block`` — the block ``block-drive`` starts the chain at.
    * ``start_instruction`` — the thin "start the driver" instruction: invoke
      ``block-drive`` and render its brief for the human's ``y``/nudge.
    """

    name: str
    prompt_name: str
    slash_name: str
    skill_name: str
    first_block: str
    start_instruction: str


def _start(workflow: str, first_block: str) -> str:
    """Render the thin, uniform start-the-driver instruction for a workflow."""
    return (
        f"Start the {workflow} workflow with the code-driven chain: invoke the "
        f"`block-drive` verb (it starts at the `{first_block}` block and chains "
        "the deterministic spans in code). Render the brief the driver returns "
        "as a proposal; the human answers `y` or nudges. On `y`, commit the "
        "approved input spec to the decision journal's `resolved`, then invoke "
        "`block-drive` again to advance. NEVER hand-compute a decision or "
        "interpret raw results — code digests the evidence into the brief; the "
        "human decides; you only translate at the rendezvous."
    )


WORKFLOW_ENTRIES: tuple[WorkflowEntry, ...] = (
    WorkflowEntry(
        name="submit",
        prompt_name="submit-hpc",
        slash_name="submit-hpc",
        skill_name="hpc-submit",
        first_block="submit-s1",
        start_instruction=_start("submit", "submit-s1"),
    ),
    WorkflowEntry(
        name="status",
        prompt_name="monitor-hpc",
        slash_name="monitor-hpc",
        skill_name="hpc-status",
        first_block="status-snapshot",
        start_instruction=_start("status", "status-snapshot"),
    ),
    WorkflowEntry(
        name="aggregate",
        prompt_name="aggregate-hpc",
        slash_name="aggregate-hpc",
        skill_name="hpc-aggregate",
        first_block="aggregate-check",
        start_instruction=_start("aggregate", "aggregate-check"),
    ),
    WorkflowEntry(
        name="campaign",
        prompt_name="campaign-hpc",
        slash_name="campaign-hpc",
        skill_name="hpc-campaign",
        first_block="campaign-greenlight",
        start_instruction=_start("campaign", "campaign-greenlight"),
    ),
)

# Prompt-name → entry, for the MCP prompt projection (name list + fallback body).
WORKFLOW_ENTRIES_BY_PROMPT: dict[str, WorkflowEntry] = {e.prompt_name: e for e in WORKFLOW_ENTRIES}


__all__ = [
    "WORKFLOW_ENTRIES",
    "WORKFLOW_ENTRIES_BY_PROMPT",
    "WorkflowEntry",
]
