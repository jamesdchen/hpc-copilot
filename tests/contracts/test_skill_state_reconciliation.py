"""Prose-contract guards for the four workflow skills under the fork.

Skills are LLM-facing procedures, so the verification here is that the
load-bearing guidance is present (and named with the exact verbs /
fields callers branch on) — the same drift-guard philosophy as
``test_lint_skill_md_literal_drift``.

Rewritten for the **hpc-copilot human-amplification fork**
(``docs/design/human-amplification-blocks.md``). The pre-fork guards in
this file pinned reconcile / inline-worker / sandbox-preflight prose that
lived *inside* the skill bodies. That prose is gone: the workflow skills
are now the **block-loop relay** — they start a block verb, relay its
code-digested brief, collect the human's ``y``/nudge, journal it via
``append-decision``, and invoke exactly the block the envelope's
``next_block`` names. The reconcile / preflight / harvest mechanics moved
*inside* the block verbs (``ops/*_blocks.py``); the skill no longer
carries them. These guards pin the new contract and fail loudly if a
future edit regresses a skill back toward resolving decisions itself or
re-introduces the stranded ``hpc-agent run`` worker handoff.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILLS = REPO_ROOT / "src" / "hpc_agent" / "slash_commands" / "skills"

# (skill_id, first block verb the relay must start).
_FIRST_BLOCK = {
    "hpc-submit": "submit-s1",
    "hpc-status": "status-snapshot",
    "hpc-aggregate": "aggregate-check",
    "hpc-campaign": "campaign-greenlight",
}


def _read(skill: str) -> str:
    return (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("skill,first_block", sorted(_FIRST_BLOCK.items()))
def test_skill_starts_its_first_block_verb(skill: str, first_block: str) -> None:
    # Deliverable 1: each workflow skill shrinks to a block-loop relay that
    # starts the FIRST block verb and drives the y/nudge loop.
    text = _read(skill)
    assert first_block in text, f"{skill} must start the {first_block!r} block verb"


@pytest.mark.parametrize("skill", sorted(_FIRST_BLOCK))
def test_skill_runs_the_propose_loop(skill: str) -> None:
    # The relay contract (design §2): surface the brief, collect y/nudge,
    # journal via append-decision, and invoke exactly next_block.verb.
    text = _read(skill)
    assert "next_block" in text, f"{skill} must invoke the envelope's next_block, not a fixed chain"
    assert "append-decision" in text, f"{skill} must journal every y/nudge exchange"
    assert "nudge" in text, f"{skill} must document the y/nudge interaction primitive"


@pytest.mark.parametrize("skill", sorted(_FIRST_BLOCK))
def test_skill_never_resolves_or_interprets(skill: str) -> None:
    # The hard rule (design §1): the LLM never resolves a decision point and
    # never interprets raw results — code digests, the human decides.
    text = _read(skill).lower()
    assert "never resolves a decision" in text
    assert "never interpret" in text or "never re-interpret" in text


@pytest.mark.parametrize("skill", sorted(_FIRST_BLOCK))
def test_worker_handoff_is_stranded(skill: str) -> None:
    # Deliverable 4: the `hpc-agent run --workflow` worker handoff and the
    # inline-worker (HPC_AGENT_INVOKER) branch are removed from the skill
    # bodies — the blocks ARE the execution now (design §6).
    text = _read(skill)
    assert "hpc-agent run --workflow" not in text, (
        f"{skill} must not hand off to the stranded worker"
    )
    assert "HPC_AGENT_INVOKER" not in text, f"{skill} must not carry the inline-worker branch"


def test_submit_speculative_canary_opt_in() -> None:
    # Deliverable 5: the submit skill documents the speculative-canary opt-in
    # (submit-speculate during S1 review; nudges never cancel).
    text = _read("hpc-submit")
    assert "submit-speculate" in text
    assert "never" in text.lower() and "cancel" in text.lower()


def test_status_session_tail_loop() -> None:
    # Deliverable 5: the status skill instructs a background tail of the local
    # supervisor output while a run is live (design §5).
    text = _read("hpc-status").lower()
    assert "tail" in text and "supervisor" in text
    assert "status-watch" in _read("hpc-status")


def test_aggregate_reducer_is_sole_source_of_metrics() -> None:
    # The #355 doctrine survives the rewrite: the reducer computes every
    # aggregate number; the skill never fabricates one.
    text = _read("hpc-aggregate")
    assert "NEVER compute" in text or "never compute" in text.lower()
    assert "reducer" in text
    assert "aggregate-run" in text


def test_campaign_greenlit_once_then_async() -> None:
    # Design §4: the campaign spec is greenlit once at start; execution then
    # runs fully asynchronously with no per-iteration human boundary.
    text = _read("hpc-campaign").lower()
    assert "greenlit" in text and "once" in text
    assert "asynchronous" in text or "async" in text
    assert "no per-iteration" in text
