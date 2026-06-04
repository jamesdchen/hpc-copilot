"""Prose-contract guards for the two state-reconciliation skill fixes.

Skills are LLM-facing procedures, so the verification here is that the
load-bearing guidance is present (and named with the exact error codes /
verbs callers branch on) — the same drift-guard philosophy as
``test_lint_skill_md_literal_drift``. These fail loudly if a future edit
drops the reconcile branch (#248) or the task_generator-mismatch guard
(#247), the way both gaps originally shipped.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILLS = REPO_ROOT / "src" / "slash_commands" / "skills"


def _read(skill: str) -> str:
    return (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")


def test_aggregate_skill_reconciles_before_nothing_to_aggregate():
    # #248: the aggregate skill must reconcile a journal-only in-flight run
    # against the cluster before refusing, and surface an abandoned run as
    # run_abandoned rather than an indefinite "nothing to aggregate".
    text = _read("hpc-aggregate")
    assert "hpc-agent reconcile" in text, "aggregate skill must call `hpc-agent reconcile`"
    assert "run_abandoned" in text, "aggregate skill must surface spec_invalid: run_abandoned"
    assert "lifecycle_state" in text, "aggregate skill must branch on reconcile's lifecycle_state"
    # The trigger condition the bug missed: trusting next_step_hint == monitor.
    assert "next_step_hint" in text and "in_flight" in text
    # Symmetric to the submit path's existing reconcile.
    assert "abandoned" in text


def test_aggregate_nothing_to_aggregate_is_cluster_confirmed():
    # The bare-journal "zero terminal runs → nothing_to_aggregate" return must
    # be gated on the reconcile step, not the journal alone.
    text = _read("hpc-aggregate")
    # The reconcile step is referenced from the resolve-run decision.
    assert "Step 1b" in text
    assert (
        "confirmed against the cluster" in text.lower() or "confirmed against the cluster" in text
    )


def test_submit_skill_reconciles_before_already_in_flight():
    # #257: the submit skill must reconcile a journal-only in-flight run against
    # the cluster BEFORE refusing `already_in_flight` — the symmetric gap to
    # #248 (aggregate). It must never refuse from `next_step_hint` alone.
    text = _read("hpc-submit")
    assert "hpc-agent reconcile" in text, "submit skill must call `hpc-agent reconcile`"
    assert "lifecycle_state" in text, "submit skill must branch on reconcile's lifecycle_state"
    assert "Step 1b" in text, "submit skill must add a reconcile Step 1b"
    assert "already_in_flight" in text
    # The trigger condition the gap missed: trusting next_step_hint == monitor.
    assert "next_step_hint" in text
    # Reconcile must surface an abandoned run (frees the cmd_sha to proceed).
    assert "abandoned" in text
    assert "confirmed against the cluster" in text.lower()


def test_submit_already_in_flight_is_cluster_confirmed():
    # The `already_in_flight` refusal must be gated on the reconcile step
    # (Step 1b), not the journal's next_step_hint alone.
    text = _read("hpc-submit")
    # The reconcile section precedes the (now cluster-confirmed) refusal.
    assert "1b" in text
    assert "never" in text.lower() and "next_step_hint" in text


def test_wrap_entry_point_scopes_data_axis_hint_to_shell_command():
    # #260: data_axis_hint is only valid on shell_command entries; the skill
    # must say so explicitly, else the agent emits it on a register_run spec
    # and eats a schema-validate / retry round-trip.
    text = _read("hpc-wrap-entry-point")
    assert "data_axis_hint" in text
    # The kind constraint must be explicit and name both shapes.
    assert "shell_command" in text and "register_run" in text
    assert "only on `entry_point.kind: shell_command`" in text


def test_submit_inline_branch_forbids_shell_and_disk_prompt_extraction():
    # #262: the inline branch must forbid shell-extraction of data.prompt
    # (including PowerShell/pwsh/cmd) and reading internal tool-results files.
    text = _read("hpc-submit")
    assert "powershell" in text.lower()
    assert "pwsh" in text.lower()
    assert "tool-results" in text
    # Anchored to the inline prompt-forwarding guidance.
    assert "data.prompt" in text


def test_submit_inline_branch_handles_prompt_path_forwarding():
    # #262B/C: the inline branch must document data.prompt_path (large prompts
    # forwarded by reference) and tell the subagent to Read it — without the
    # orchestrator reading it into its own context.
    text = _read("hpc-submit")
    assert "prompt_path" in text
    assert "Read" in text
    # Both shapes are named.
    assert "data.prompt" in text


def test_sandbox_ssh_preflight_surfaced_in_worker_and_skill():
    # #265: an inline worker in a sandboxed session must detect the structural
    # cluster-SSH block UP FRONT (Step-0 preflight) and surface
    # sandbox_blocks_cluster_ssh, not run all local prep then return a buried
    # near-success.
    skill = _read("hpc-submit")
    assert "sandbox_blocks_cluster_ssh" in skill
    worker = (
        REPO_ROOT / "src" / "hpc_agent" / "_kernel" / "extension" / "worker_prompts" / "submit.md"
    ).read_text(encoding="utf-8")
    assert "sandbox_blocks_cluster_ssh" in worker
    assert "check-preflight" in worker  # the upfront preflight verb


def test_submit_skill_guards_task_generator_mismatch():
    # #247: a cached interview.json must not silently override a divergent
    # caller-supplied task_generator (the 8-vs-100 drift).
    text = _read("hpc-submit")
    assert "task_generator_mismatch" in text, "submit skill must surface task_generator_mismatch"
    assert "on_task_generator_mismatch" in text, "submit skill must document the mismatch field"
    # All three documented behaviours must be named.
    for behaviour in ("fail", "refresh", "prefer-caller"):
        assert behaviour in text, (
            f"submit skill must document on_task_generator_mismatch={behaviour}"
        )
    # The guard sits at the interview.json short-circuit.
    assert "interview.json" in text
    # `fail` is the default (loud), not silent.
    assert "default" in text.lower()
