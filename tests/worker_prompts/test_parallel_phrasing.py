"""Guard the disambiguated 'parallelize tool calls' prose (#259).

The Execution-style bullet in every orchestrator skill (and the worker-spawn
scaffold) used to say "Batch independent tool calls into one parallel message",
which an LLM could (mis)read as shell-level concurrency (`cmd1 & cmd2`,
`parallel`, `xargs -P`) inside one Bash call. These guards fail loudly if a
future edit drops the clarification that 'parallel' means multiple tool-call
blocks in one assistant message, NOT in-shell concurrency.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILLS = REPO_ROOT / "src" / "slash_commands" / "skills"


def _skill_bodies():
    return {p.parent.name: p.read_text(encoding="utf-8") for p in _SKILLS.rglob("SKILL.md")}


def test_every_skill_disambiguates_parallel_tool_calls():
    for name, text in _skill_bodies().items():
        # The pre-#259 ambiguous phrasing must be gone.
        assert "into one parallel message" not in text, (
            f"{name}: stale ambiguous 'into one parallel message' phrasing"
        )
        # The clarified meaning must be present.
        assert "tool-call block" in text, f"{name}: missing 'tool-call block' clarification"
        # It must explicitly warn off shell-level concurrency.
        assert "xargs -P" in text, f"{name}: missing the shell-concurrency anti-pattern callout"


def test_worker_spawn_scaffold_disambiguates_parallel():
    # The clarified phrasing lives as a static literal in the cacheable_prefix
    # scaffold; rendering for a real workflow embeds it verbatim.
    from hpc_agent._kernel.extension.spawn_prompt import WORKFLOW_PROCEDURES, render_spawn_parts

    workflow = next(iter(WORKFLOW_PROCEDURES))
    rendered = render_spawn_parts(workflow=workflow, experiment_dir="/tmp/x", fields={})
    prefix = rendered.cacheable_prefix
    assert "parallel TOOL CALLS" in prefix
    assert "xargs -P" in prefix
    assert "into one parallel message" not in prefix
