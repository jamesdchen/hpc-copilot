"""Exhaustive tests for the #294 Layer-2 auto-resume decision core (the gate)."""

from __future__ import annotations

from hpc_agent.recovery.auto_resume import (
    AutoResumeDecision,
    decide_auto_resume,
    resumable_task_ids,
)


def _sidecar(*preempted_ids: int, other: dict | None = None) -> dict:
    tasks: dict[str, dict] = {
        str(i): {"preempt": {"at": "2026-01-01T00:00:00Z", "grace_sec": 25}} for i in preempted_ids
    }
    if other:
        tasks.update(other)
    return {"tasks": tasks}


# ── resumable_task_ids ────────────────────────────────────────────────────


def test_resumable_ids_returns_only_preempt_marked_sorted() -> None:
    sc = _sidecar(3, 1, other={"2": {"exit_code": 137}, "4": {}})  # 2=OOM, 4=plain
    assert resumable_task_ids(sc) == [1, 3]


def test_resumable_ids_empty_when_no_marks_or_no_tasks() -> None:
    assert resumable_task_ids({"tasks": {"0": {"exit_code": 1}}}) == []
    assert resumable_task_ids({}) == []
    assert resumable_task_ids({"tasks": "not-a-dict"}) == []


def test_resumable_ids_skips_non_integer_keys() -> None:
    sc = {"tasks": {"batch": {"preempt": {}}, "0": {"preempt": {}}}}
    assert resumable_task_ids(sc) == [0]


# ── decide_auto_resume (the three hard gates) ─────────────────────────────


def test_gate_policy_off_escalates_even_with_preempted_tasks() -> None:
    d = decide_auto_resume(_sidecar(0, 1), policy_on=False, count=0, cap=3)
    assert d.action == "escalate" and "not enabled" in d.reason


def test_gate_no_resumable_tasks_escalates() -> None:
    # OOM-style failure (no preempt mark) must NOT auto-resume — it would re-OOM.
    sc = {"tasks": {"0": {"exit_code": 137}}}
    d = decide_auto_resume(sc, policy_on=True, count=0, cap=3)
    assert d.action == "escalate" and "not a resumable kill" in d.reason
    assert d.task_ids == ()


def test_gate_under_cap_resumes_with_ids() -> None:
    d = decide_auto_resume(_sidecar(2, 0), policy_on=True, count=1, cap=3)
    assert d == AutoResumeDecision("resume", (0, 2), d.reason)
    assert d.action == "resume" and d.task_ids == (0, 2)


def test_gate_at_cap_escalates() -> None:
    d = decide_auto_resume(_sidecar(0), policy_on=True, count=3, cap=3)
    assert d.action == "escalate" and "cap reached (3/3)" in d.reason
    # carries the ids so the escalation can name what would have resumed
    assert d.task_ids == (0,)


def test_gate_over_cap_escalates() -> None:
    d = decide_auto_resume(_sidecar(0), policy_on=True, count=5, cap=3)
    assert d.action == "escalate"


def test_gate_count_zero_under_cap_one_resumes() -> None:
    # The minimal opt-in: cap=1 allows exactly one auto-resume.
    assert decide_auto_resume(_sidecar(0), policy_on=True, count=0, cap=1).action == "resume"
    assert decide_auto_resume(_sidecar(0), policy_on=True, count=1, cap=1).action == "escalate"
