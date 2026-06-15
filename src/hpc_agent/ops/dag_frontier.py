"""``dag-frontier`` primitive — read-only view of the recorded run graph.

The observation instrument for caller-side topology walking
(``docs/design/dag-kernel.md`` step 5): the lineage graph already exists
on disk — every parented sidecar carries ``parent_run_ids`` — so this
verb reconstructs it from ``.hpc/runs/`` and reports, per recorded node,
its observed lifecycle state and which ancestors are not yet
terminal-success. One call answers the walker's standing question:
*which runs can serve as parents for the next submits?*

This is the ∀-nodes lift of ``validate-parents-ready`` (which answers
the same question for ONE prospective child's declared parents), and the
two share ``observe_run_state`` so they can never disagree. Deliberately
NOT a walker: it computes the frontier and stops — deciding which child
to submit, with what concurrency, stays the caller's (the earn-it rule
for the graph-runner composite is unchanged).

Pure local: sidecar reads + journal ``load_run``. No SSH, no scheduler.
The recorded graph is acyclic by construction (a parent's sidecar must
exist before a child can compose its identity), but the ancestor walk
still carries a visited-set so a hand-edited sidecar cannot hang it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliShape
from hpc_agent.ops.validate.parents_ready import observe_run_state

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["dag_frontier"]

#: Lineage states a node may report. ``missing``/``unknown`` come from
#: ``observe_run_state`` (no sidecar / no journal record respectively).
_TERMINAL_SUCCESS = "complete"


def _blocking_ancestors(
    run_id: str,
    edges: dict[str, list[str]],
    states: dict[str, str],
) -> list[str]:
    """Transitive ancestors of *run_id* whose state is not terminal-success.

    Ancestors absent from the recorded node set (pruned sidecars, or
    edits) are observed lazily via *states* — the caller pre-populates it
    for recorded nodes and this walk extends it for referenced-only ids.
    Informational: the authoritative per-child gate checks DIRECT parents
    only (``validate-parents-ready``); the transitive view exists so a
    walker can see at a glance which subtrees are not worth queueing yet.
    """
    blocked: list[str] = []
    seen: set[str] = {run_id}
    stack = list(edges.get(run_id, []))
    while stack:
        ancestor = stack.pop()
        if ancestor in seen:
            continue
        seen.add(ancestor)
        if states.get(ancestor) != _TERMINAL_SUCCESS:
            blocked.append(ancestor)
        stack.extend(edges.get(ancestor, []))
    return sorted(blocked)


@primitive(
    name="dag-frontier",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Read-only view of the recorded run graph: per-node lifecycle state "
            "+ parent edges (from sidecar parent_run_ids), the frontier of "
            "complete runs eligible to serve as parents, and per-node "
            "blocking ancestors. Pure local read; the walker's observation "
            "instrument — it computes, never submits."
        ),
        experiment_dir_arg=True,
    ),
    agent_facing=True,
)
def dag_frontier(experiment_dir: Path) -> dict[str, Any]:
    """Reconstruct the lineage graph from sidecars and report its state.

    Returns::

        {
          "nodes": [
            {"run_id", "parent_run_ids", "state", "node_sha",
             "blocking_ancestors"},   # sorted by run_id
            ...
          ],
          "frontier": [run_id, ...],  # state == complete: eligible parents
          "summary": {state: [run_id, ...], ...},  # every observed state
        }

    ``state`` is ``observe_run_state``'s vocabulary: a journal status
    (``complete`` / ``in_flight`` / ``failed`` / ``abandoned``), or
    ``unknown`` (sidecar without a journal record). ``blocking_ancestors``
    is the transitive not-yet-complete ancestry — informational; the
    authoritative pre-submit gate is ``validate-parents-ready`` over a
    child's direct parents.
    """
    import json

    from hpc_agent import errors
    from hpc_agent.state.runs import find_existing_runs, read_run_sidecar

    edges: dict[str, list[str]] = {}
    node_shas: dict[str, str | None] = {}
    for path in find_existing_runs(experiment_dir):
        run_id = path.stem
        try:
            sidecar = read_run_sidecar(experiment_dir, run_id)
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError, errors.HpcError):
            # Same tolerance as the campaign history walk: one unreadable
            # sidecar must not blank the whole graph view.
            continue
        edges[run_id] = [str(p) for p in (sidecar.get("parent_run_ids") or [])]
        node_shas[run_id] = sidecar.get("node_sha")

    # Observe every recorded node, then lazily extend to referenced-only
    # ancestors (a pruned parent sidecar leaves a dangling edge).
    states: dict[str, str] = {run_id: observe_run_state(experiment_dir, run_id) for run_id in edges}
    for parents in edges.values():
        for parent_id in parents:
            if parent_id not in states:
                states[parent_id] = observe_run_state(experiment_dir, parent_id)

    nodes = [
        {
            "run_id": run_id,
            "parent_run_ids": list(edges[run_id]),
            "state": states[run_id],
            "node_sha": node_shas[run_id],
            "blocking_ancestors": _blocking_ancestors(run_id, edges, states),
        }
        for run_id in sorted(edges)
    ]
    summary: dict[str, list[str]] = {}
    for run_id in sorted(edges):
        summary.setdefault(states[run_id], []).append(run_id)
    return {
        "nodes": nodes,
        "frontier": summary.get(_TERMINAL_SUCCESS, []),
        "summary": summary,
    }
