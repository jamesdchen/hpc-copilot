"""Tests for the ``dag-frontier`` query — the walker's observation instrument.

Builds a small recorded graph from real sidecars + journal records and
asserts the reconstructed view: per-node state, the complete-runs
frontier, transitive blocking ancestors, and the dangling-edge case (a
parent whose sidecar was pruned).
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent.ops.dag_frontier import dag_frontier
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar


def _seed_node(
    experiment_dir: Path,
    *,
    run_id: str,
    parents: list[str] | None = None,
    status: str | None = "complete",
) -> None:
    """Write a sidecar (+ journal record unless ``status=None``)."""
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-06-10T00:00:00Z",
        executor="python3 src/test.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=1,
        tasks_py_sha="",
        wave_map={"0": [0]},
        parent_run_ids=parents,
        # The frontier view reads lineage, not identity — a constant
        # placeholder digest keeps the seed helper simple.
        node_sha=("b" * 64) if parents else None,
    )
    if status is not None:
        upsert_run(
            experiment_dir,
            RunRecord(
                run_id=run_id,
                profile="ml",
                cluster="hoffman2",
                ssh_target="u@h",
                remote_path="/r",
                job_name="ml",
                job_ids=["1"],
                total_tasks=1,
                submitted_at="2026-06-10T00:00:00Z",
                experiment_dir=str(experiment_dir),
                status=status,
            ),
        )


def test_empty_experiment_dir(journal_home, tmp_path: Path) -> None:
    assert dag_frontier(tmp_path) == {"nodes": [], "frontier": [], "summary": {}}


def test_diamond_graph_states_and_frontier(journal_home, tmp_path: Path) -> None:
    """A complete, B in flight, C consumes A (complete), D consumes B+C:
    the frontier is {A, C}; D is blocked transitively by B alone."""
    _seed_node(tmp_path, run_id="A", status="complete")
    _seed_node(tmp_path, run_id="B", status="in_flight")
    _seed_node(tmp_path, run_id="C", parents=["A"], status="complete")
    _seed_node(tmp_path, run_id="D", parents=["B", "C"], status="in_flight")

    view = dag_frontier(tmp_path)

    assert view["frontier"] == ["A", "C"]
    assert view["summary"] == {"complete": ["A", "C"], "in_flight": ["B", "D"]}
    by_id = {n["run_id"]: n for n in view["nodes"]}
    assert by_id["C"]["parent_run_ids"] == ["A"]
    assert by_id["C"]["blocking_ancestors"] == []
    assert by_id["D"]["blocking_ancestors"] == ["B"]
    assert by_id["D"]["node_sha"] is not None
    assert by_id["A"]["node_sha"] is None  # parentless: identity is bare cmd_sha


def test_failed_ancestor_blocks_transitively(journal_home, tmp_path: Path) -> None:
    """grandparent failed → both descendants report it; the walker sees at a
    glance the whole subtree is not worth queueing."""
    _seed_node(tmp_path, run_id="gp", status="failed")
    _seed_node(tmp_path, run_id="mid", parents=["gp"], status="complete")
    _seed_node(tmp_path, run_id="leaf", parents=["mid"], status="in_flight")

    view = dag_frontier(tmp_path)

    by_id = {n["run_id"]: n for n in view["nodes"]}
    assert by_id["mid"]["blocking_ancestors"] == ["gp"]
    assert by_id["leaf"]["blocking_ancestors"] == ["gp"]
    # mid IS complete so it sits on the frontier despite tainted ancestry —
    # the view reports both facts; weighing them is the caller's judgment.
    assert "mid" in view["frontier"]


def test_sidecar_without_journal_is_unknown(journal_home, tmp_path: Path) -> None:
    _seed_node(tmp_path, run_id="ghosted", status=None)
    view = dag_frontier(tmp_path)
    assert view["summary"] == {"unknown": ["ghosted"]}
    assert view["frontier"] == []


def test_dangling_parent_reports_missing(journal_home, tmp_path: Path) -> None:
    """A pruned parent sidecar leaves a dangling edge: the referenced-only
    ancestor is observed as ``missing`` and blocks its descendants."""
    _seed_node(tmp_path, run_id="child", parents=["pruned-away"], status="in_flight")
    view = dag_frontier(tmp_path)
    by_id = {n["run_id"]: n for n in view["nodes"]}
    assert by_id["child"]["blocking_ancestors"] == ["pruned-away"]
    # Referenced-only ids are not nodes — they have no sidecar to report.
    assert list(by_id) == ["child"]


def test_unreadable_sidecar_is_skipped(journal_home, tmp_path: Path) -> None:
    _seed_node(tmp_path, run_id="ok", status="complete")
    runs_dir = tmp_path / ".hpc" / "runs"
    (runs_dir / "corrupt.json").write_text("{not json")
    view = dag_frontier(tmp_path)
    assert [n["run_id"] for n in view["nodes"]] == ["ok"]


def test_handcrafted_cycle_does_not_hang(journal_home, tmp_path: Path) -> None:
    """Cycles are structurally impossible via the submit path; a hand-edited
    sidecar must still not hang the ancestor walk."""
    _seed_node(tmp_path, run_id="X", parents=["Y"], status="in_flight")
    _seed_node(tmp_path, run_id="Y", status="in_flight")
    y_path = tmp_path / ".hpc" / "runs" / "Y.json"
    data = json.loads(y_path.read_text())
    data["parent_run_ids"] = ["X"]  # forge the back-edge
    y_path.write_text(json.dumps(data))

    view = dag_frontier(tmp_path)

    by_id = {n["run_id"]: n for n in view["nodes"]}
    assert by_id["X"]["blocking_ancestors"] == ["Y"]
    assert by_id["Y"]["blocking_ancestors"] == ["X"]
