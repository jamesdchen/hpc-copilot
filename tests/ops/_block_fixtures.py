"""Shared journal + sidecar fixtures for the submit/aggregate block-verb tests.

The sequenced block verbs (submit-s2/s3/s4, aggregate-run) refuse to act unless
the latest run-scoped decision is a ``y`` whose ``resolved.next_block`` names them
(docs/design/human-amplification-blocks.md §2). ``greenlight`` journals that
precondition; ``sidecar`` writes the per-run fingerprint the terminal replay keys
on. Both are parametrized over ``run_id`` so each test module binds its own
constant — see the thin ``_greenlight`` / ``_sidecar`` wrappers at the call sites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def greenlight(
    experiment_dir: Path,
    verb: str,
    *,
    run_id: str,
    response: str = "y",
    scope_kind: str = "run",
) -> None:
    """Journal a human decision (``y`` by default) naming *verb* as ``next_block``.

    A non-``y`` *response* records a nudge, not a greenlight — the gate's
    fail-closed paths exercise that.
    """
    from hpc_agent.state.decision_journal import append_decision

    append_decision(
        experiment_dir,
        scope_kind=scope_kind,
        scope_id=run_id,
        block="test-greenlight",
        response=response,
        resolved={"next_block": verb},
    )


def sidecar(
    experiment_dir: Path,
    *,
    cmd_sha: str,
    run_id: str,
    hpc_agent_version: str = "0.0.0-test",
    submitted_at: str = "2026-01-01T00:00:00+00:00",
    executor: str = "python run.py",
    result_dir_template: str = "results/{task_id}",
    task_count: int = 10,
    tasks_py_sha: str = "",
) -> None:
    """Write a per-run sidecar so ``read_run_cmd_sha`` has a tree fingerprint to
    key the terminal replay on (the replay refuses on an absent/empty sha)."""
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha=cmd_sha,
        hpc_agent_version=hpc_agent_version,
        submitted_at=submitted_at,
        executor=executor,
        result_dir_template=result_dir_template,
        task_count=task_count,
        tasks_py_sha=tasks_py_sha,
    )
