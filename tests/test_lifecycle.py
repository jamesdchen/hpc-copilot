"""Tests for ``claude_hpc._internal.lifecycle``.

The B2 refactor introduced four StrEnums to replace the four scattered,
drifting string vocabularies. The cross-validation tests here exist
specifically to make the drift class unrepresentable: any future bug
of the form "classifier emits X but resubmit rejects X" or "schema
enum diverges from monitor_flow's literal" fails CI.
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_hpc._internal.lifecycle import (
    TERMINAL_STATUSES,
    FailureCategory,
    JournalStatus,
    LifecycleState,
    TaskStatus,
)

REPO = Path(__file__).resolve().parent.parent
SCHEMAS = REPO / "claude_hpc" / "schemas"


def _load_lifecycle_enum(schema_name: str) -> set[str]:
    schema = json.loads((SCHEMAS / schema_name).read_text())
    return set(schema["properties"]["lifecycle_state"]["enum"])


def test_journal_status_str_coercion() -> None:
    """StrEnum values round-trip as plain strings in JSON."""
    assert json.dumps({"status": JournalStatus.COMPLETE}) == '{"status": "complete"}'
    assert JournalStatus.IN_FLIGHT == "in_flight"


def test_terminal_statuses_match_journal_status() -> None:
    assert {
        JournalStatus.COMPLETE,
        JournalStatus.FAILED,
        JournalStatus.ABANDONED,
    } == TERMINAL_STATUSES
    assert JournalStatus.IN_FLIGHT not in TERMINAL_STATUSES


def test_lifecycle_state_matches_monitor_flow_schema() -> None:
    """The schema enum must equal the StrEnum's value set."""
    enum = _load_lifecycle_enum("monitor_flow.output.json")
    # monitor_flow emits only the four terminal-or-budget values.
    assert enum == {
        LifecycleState.COMPLETE,
        LifecycleState.FAILED,
        LifecycleState.ABANDONED,
        LifecycleState.TIMEOUT,
    }


def test_lifecycle_state_matches_status_schema() -> None:
    enum = _load_lifecycle_enum("status.output.json")
    # status includes in_flight (workflow may still be running).
    assert enum == set(LifecycleState)


def test_lifecycle_state_matches_reconcile_schema() -> None:
    enum = _load_lifecycle_enum("reconcile.output.json")
    assert enum == set(LifecycleState)


def test_failure_category_includes_classifier_emissions() -> None:
    """Every category the classifier emits must round-trip through FailureCategory."""
    from slash_commands import runner

    classifier_emits = {cat for cat, _ in runner._FAILURE_CATEGORY_PATTERNS}
    canonical = {fc.value for fc in FailureCategory}
    missing = classifier_emits - canonical
    assert not missing, f"classifier emits categories not in FailureCategory: {missing}"


def test_failure_category_includes_resubmit_validation() -> None:
    """Every category the resubmit path accepts must round-trip through FailureCategory."""
    from claude_hpc import agent_cli

    accepted = set(agent_cli._VALID_RESUBMIT_CATEGORIES)
    canonical = {fc.value for fc in FailureCategory}
    missing = accepted - canonical
    assert not missing, f"resubmit accepts categories not in FailureCategory: {missing}"


def test_classifier_emissions_subset_of_resubmit_accepted() -> None:
    """A4: every emitted category must be accepted by the resubmit path.

    This is the asymmetric-overlap bug we explicitly want to make
    unrepresentable. If the classifier ever emits a category the
    resubmit silently rejects, this fails.
    """
    from claude_hpc import agent_cli
    from slash_commands import runner

    classifier_emits = {cat for cat, _ in runner._FAILURE_CATEGORY_PATTERNS}
    accepted = set(agent_cli._VALID_RESUBMIT_CATEGORIES)
    rejected = classifier_emits - accepted
    assert not rejected, f"classifier emits categories the resubmit path rejects: {rejected}"


def test_task_status_distinct_from_journal_status() -> None:
    """TaskStatus and JournalStatus overlap on 'complete'/'failed' but
    are intentionally distinct types — workflow vs per-task semantics."""
    assert TaskStatus.RUNNING.value == "running"
    assert TaskStatus.PENDING.value == "pending"
    assert TaskStatus.UNKNOWN.value == "unknown"
    # Overlapping values exist but the types are not assignable in mypy.
    assert TaskStatus.COMPLETE.value == JournalStatus.COMPLETE.value
