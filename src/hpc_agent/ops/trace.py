"""Execution trace — join the journal, sidecars, and provenance into one DAG.

A read-only ``query`` primitive. Given a ``campaign_id`` (or a single
``run_id``), assemble the campaign's runs, each run's provenance fingerprint
and wave structure, and the lineage edges between runs (``parent_run_ids``)
into one *derived*, replayable DAG — the "explain exactly what produced this
result, and in what order" surface. It is the read-side complement to the
OpenTelemetry sink (:mod:`hpc_agent._kernel.extension.telemetry`): OTel
streams the trace live; ``trace`` reconstructs it after the fact for
replay / audit / agent consumption.

Pure local filesystem read — the per-run journal records under
``~/.claude/hpc/<repo>/``, the per-run sidecars under ``.hpc/runs/``, and the
signable provenance manifest. No SSH, no scheduler. Derived state, like
``provenance-manifest``: recomputed from disk on every call, never a second
source of truth that can drift.

This file lives at the ``ops/`` *role root* (sibling to the subjects, like
``provenance_manifest.py`` and the workflows) because it reads across subjects
— ``state``, the campaign sidecar history, and the provenance manifest. The
subject-imports lint short-circuits for role-root files, so the cross-subject
reads here are allowed by construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.trace import TraceResult
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.execution.mapreduce.reduce.history import find_sidecars_by_campaign
from hpc_agent.ops.provenance_manifest import (
    build_provenance_manifest,
    manifest_signature,
    project_run_provenance,
)
from hpc_agent.state.index import find_runs_by_campaign
from hpc_agent.state.journal import load_run
from hpc_agent.state.runs import read_run_sidecar

if TYPE_CHECKING:
    from hpc_agent.state.run_record import RunRecord

__all__ = ["trace"]

# The closed set of output formats, mirrored on TraceResult.format. Typed as a
# Literal (not bare str) so it satisfies the TraceResult field without a cast.
TraceFormat = Literal["dag", "flat", "dot"]

# Bump when the emitted node/edge shape changes in a way a consumer would need
# to branch on. Mirrored in TraceResult.trace_schema_version.
TRACE_SCHEMA_VERSION: int = 1


def _coerce_format(value: str) -> TraceFormat:
    """Narrow an arbitrary CLI string to a valid :data:`TraceFormat`.

    `value in (...)` does not narrow the type for the checker, so an explicit
    branch is what lets ``format=fmt`` type-check against the Literal field.
    Any unknown value falls back to the ``dag`` default.
    """
    if value == "flat":
        return "flat"
    if value == "dot":
        return "dot"
    return "dag"


def _safe_sidecar(experiment_dir: Path, run_id: str) -> dict[str, Any]:
    """Return a run's sidecar dict, or ``{}`` when none exists.

    ``read_run_sidecar`` raises ``FileNotFoundError`` for a missing sidecar; a
    trace must tolerate a run that has a journal record but no sidecar (or vice
    versa), so the absence is data, not an error.
    """
    try:
        return read_run_sidecar(experiment_dir, run_id)
    except FileNotFoundError:
        return {}


def _run_node(run_id: str, sidecar: dict[str, Any], record: RunRecord | None) -> dict[str, Any]:
    """Build the ``run`` node — provenance fingerprint + lifecycle + timing.

    The journal record is the authority for live lifecycle state (status,
    stage, combined/failed waves, job ids); the sidecar is the authority for
    the immutable submit-time provenance (cmd/code/data/env shas). Either may
    be absent, so each field falls back to the other source and finally to
    ``None``.
    """
    return {
        "id": f"run:{run_id}",
        "kind": "run",
        "run_id": run_id,
        "status": record.status if record else None,
        "stage": record.stage if record else None,
        "cluster": (record.cluster if record else None) or sidecar.get("cluster"),
        "profile": (record.profile if record else None) or sidecar.get("profile"),
        "submitted_at": (record.submitted_at if record else None) or sidecar.get("submitted_at"),
        "total_tasks": record.total_tasks if record else sidecar.get("task_count"),
        "job_ids": list(record.job_ids) if record else list(sidecar.get("job_ids") or []),
        "combined_waves": list(record.combined_waves) if record else [],
        "failed_waves": list(record.failed_waves) if record else [],
        "provenance": project_run_provenance(sidecar),
    }


def _wave_nodes_and_edges(
    run_id: str, sidecar: dict[str, Any], record: RunRecord | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build one ``wave`` node per wave in the run's wave_map, plus ``contains`` edges.

    A wave's ``state`` is read from the journal record's combined/failed wave
    lists (the live verdict); a wave in neither is still ``in_flight``.
    """
    wave_map: dict[str, Any] = sidecar.get("wave_map") or {}
    combined = set(record.combined_waves) if record else set()
    failed = set(record.failed_waves) if record else set()
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for w_str, task_ids in sorted(wave_map.items(), key=lambda kv: int(kv[0])):
        wave = int(w_str)
        state = "combined" if wave in combined else "failed" if wave in failed else "in_flight"
        wave_id = f"wave:{run_id}:{wave}"
        ids = list(task_ids)
        nodes.append(
            {
                "id": wave_id,
                "kind": "wave",
                "run_id": run_id,
                "wave": wave,
                "state": state,
                "task_count": len(ids),
                "task_ids": ids,
            }
        )
        edges.append({"from": f"run:{run_id}", "to": wave_id, "rel": "contains"})
    return nodes, edges


def _lineage_edges(run_id: str, sidecar: dict[str, Any]) -> list[dict[str, Any]]:
    """``derived-from`` edges from *run_id* to each of its parent runs."""
    return [
        {"from": f"run:{run_id}", "to": f"run:{parent}", "rel": "derived-from"}
        for parent in (sidecar.get("parent_run_ids") or [])
    ]


# --- Graphviz DOT rendering (--format dot) -----------------------------------

_NODE_SHAPE = {"campaign": "box", "run": "ellipse", "wave": "note"}
# Fill colour by run lifecycle status / wave verdict — green = done, red =
# failed, grey = still in flight, white = unknown. Shared so a run and the wave
# it contains read the same.
_DONE = "#cdebc5"
_FAIL = "#f4c7c3"
_LIVE = "#d9d9d9"
_RUN_FILL = {"complete": _DONE, "failed": _FAIL, "error": _FAIL, "timeout": _FAIL}
_WAVE_FILL = {"combined": _DONE, "failed": _FAIL, "in_flight": _LIVE}
_EDGE_STYLE = {"member": "solid", "derived-from": "dashed", "contains": "dotted"}


def _dot_escape(text: str) -> str:
    r"""Escape a string for a double-quoted DOT id/label (``\`` and ``"``)."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _dot_node_attrs(node: dict[str, Any]) -> tuple[str, str]:
    """Return ``(label, fillcolor)`` for one node, keyed by ``kind``."""
    kind = node["kind"]
    if kind == "campaign":
        return f"campaign\\n{node['campaign_id']}\\n{node['run_count']} runs", "#cfe2f3"
    if kind == "run":
        status = str(node.get("status") or "?")
        # The trailing slug of the run id is the human-recognisable part.
        short = str(node["run_id"]).rsplit("-", 1)[-1]
        return f"run {short}\\n{status}", _RUN_FILL.get(status, "#ffffff")
    state = str(node.get("state") or "")
    return f"wave {node['wave']}\\n{state} ({node['task_count']})", _WAVE_FILL.get(state, "#ffffff")


def _render_dot(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    """Render *nodes* / *edges* as a Graphviz DOT digraph.

    Node shape encodes kind (box=campaign, ellipse=run, note=wave) and fill
    encodes lifecycle state; edge line-style encodes the relation
    (solid=member, dashed=derived-from, dotted=contains). The string is stable
    for a given DAG so a caller can diff two renders.
    """
    lines = [
        "digraph hpc_trace {",
        "  rankdir=LR;",
        '  node [fontname="monospace", style=filled];',
    ]
    for node in nodes:
        label, fill = _dot_node_attrs(node)
        shape = _NODE_SHAPE.get(str(node["kind"]), "ellipse")
        node_id = _dot_escape(str(node["id"]))
        lines.append(f'  "{node_id}" [label="{label}", shape={shape}, fillcolor="{fill}"];')
    for edge in edges:
        style = _EDGE_STYLE.get(str(edge["rel"]), "solid")
        src = _dot_escape(str(edge["from"]))
        dst = _dot_escape(str(edge["to"]))
        lines.append(f'  "{src}" -> "{dst}" [label="{edge["rel"]}", style={style}];')
    lines.append("}")
    return "\n".join(lines)


def _trace_campaign(experiment_dir: Path, campaign_id: str, fmt: TraceFormat) -> TraceResult:
    """Assemble the DAG for every run tagged with *campaign_id*."""
    records = find_runs_by_campaign(experiment_dir, campaign_id)
    sidecars: dict[str, dict[str, Any]] = {
        sc.get("run_id", ""): sc
        for sc in find_sidecars_by_campaign(experiment_dir, campaign_id)
        if sc.get("run_id")
    }
    record_by_id = {r.run_id: r for r in records}

    # Run universe = the union of journal records and sidecars, journal-order
    # first (oldest-first) then any sidecar-only runs. A run can have one
    # without the other (journal pruned, or sidecar written before the journal
    # record landed), and the trace should surface either.
    run_ids: list[str] = list(record_by_id)
    run_ids += [rid for rid in sidecars if rid not in record_by_id]

    root = f"campaign:{campaign_id}"
    nodes: list[dict[str, Any]] = [
        {"id": root, "kind": "campaign", "campaign_id": campaign_id, "run_count": len(run_ids)}
    ]
    edges: list[dict[str, Any]] = []
    for run_id in run_ids:
        record = record_by_id.get(run_id)
        sidecar = sidecars.get(run_id, {})
        nodes.append(_run_node(run_id, sidecar, record))
        if fmt != "flat":
            edges.append({"from": root, "to": f"run:{run_id}", "rel": "member"})
            edges.extend(_lineage_edges(run_id, sidecar))
            wave_nodes, wave_edges = _wave_nodes_and_edges(run_id, sidecar, record)
            nodes.extend(wave_nodes)
            edges.extend(wave_edges)

    # The signature ties this trace to the canonical signable provenance
    # artifact: a reader can `hpc-agent provenance-manifest` the same campaign
    # and confirm the signatures match.
    signature = manifest_signature(build_provenance_manifest(experiment_dir, campaign_id))
    return TraceResult(
        trace_schema_version=TRACE_SCHEMA_VERSION,
        scope="campaign",
        format=fmt,
        campaign_id=campaign_id,
        root=root,
        signature=signature,
        node_count=len(nodes),
        nodes=nodes,
        edges=edges,
    )


def _trace_run(experiment_dir: Path, run_id: str, fmt: TraceFormat) -> TraceResult:
    """Assemble the DAG for one run plus its transitive lineage ancestors."""
    seed_record = load_run(experiment_dir, run_id)
    seed_sidecar = _safe_sidecar(experiment_dir, run_id)
    if seed_record is None and not seed_sidecar:
        raise errors.SpecInvalid(f"no journal record or sidecar found for run_id {run_id!r}")

    # Walk parent_run_ids breadth-first so a resubmit chain (A→B→C) is captured
    # as the lineage that produced this run. `seen` guards against a cycle in a
    # malformed sidecar set.
    seen: set[str] = set()
    order: list[str] = []
    queue: list[str] = [run_id]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        order.append(current)
        sidecar = seed_sidecar if current == run_id else _safe_sidecar(experiment_dir, current)
        for parent in sidecar.get("parent_run_ids") or []:
            if parent not in seen:
                queue.append(parent)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for current in order:
        record = seed_record if current == run_id else load_run(experiment_dir, current)
        sidecar = seed_sidecar if current == run_id else _safe_sidecar(experiment_dir, current)
        nodes.append(_run_node(current, sidecar, record))
        if fmt != "flat":
            edges.extend(_lineage_edges(current, sidecar))
            wave_nodes, wave_edges = _wave_nodes_and_edges(current, sidecar, record)
            nodes.extend(wave_nodes)
            edges.extend(wave_edges)

    return TraceResult(
        trace_schema_version=TRACE_SCHEMA_VERSION,
        scope="run",
        format=fmt,
        campaign_id=None,
        root=f"run:{run_id}",
        signature=None,
        node_count=len(nodes),
        nodes=nodes,
        edges=edges,
    )


@primitive(
    name="trace",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help=(
            "Assemble a derived execution DAG for a campaign (--campaign-id) or "
            "a single run's lineage (--run-id) by joining the per-run journal "
            "records, the per-run sidecars, and the signable provenance "
            "manifest. Read-only, no SSH. --format dag (default) emits run + "
            "wave nodes and member/derived-from/contains edges; --format flat "
            "emits the run list only; --format dot adds a Graphviz `dot` string."
        ),
        experiment_dir_arg=True,
        args=(
            CliArg(
                "--campaign-id",
                type=str,
                help="Trace every run tagged with this campaign_id. Excludes --run-id.",
            ),
            CliArg(
                "--run-id",
                type=str,
                help="Trace this run plus its transitive lineage. Excludes --campaign-id.",
            ),
            CliArg(
                "--format",
                type=str,
                choices=("dag", "flat", "dot"),
                default="dag",
                dest="trace_format",
                help="`dag` (default): nodes + edges. `flat`: run nodes only. `dot`: + Graphviz.",
            ),
        ),
    ),
    agent_facing=True,
)
def trace(
    *,
    experiment_dir: Path,
    campaign_id: str | None = None,
    run_id: str | None = None,
    trace_format: str = "dag",
) -> dict[str, Any]:
    """Return a derived execution DAG for a campaign or a single run's lineage.

    Exactly one of *campaign_id* / *run_id* must be supplied. Campaign scope
    walks every run tagged with the campaign and attaches the canonical
    provenance ``signature``; run scope walks the run's ``parent_run_ids``
    transitively (the resubmit lineage) and carries no signature (a signature
    attests a whole campaign, not a lineage slice).

    Idempotent by construction: the DAG is derived state, recomputed from the
    journal records + sidecars on every call, so replaying after more submits
    simply reflects the runs now on disk.
    """
    cid = (campaign_id or "").strip()
    rid = (run_id or "").strip()
    if bool(cid) == bool(rid):
        raise errors.SpecInvalid("trace requires exactly one of --campaign-id or --run-id")
    fmt = _coerce_format(trace_format)
    experiment_dir = Path(experiment_dir)
    result = (
        _trace_campaign(experiment_dir, cid, fmt) if cid else _trace_run(experiment_dir, rid, fmt)
    )
    # `dot` carries the same graph as `dag` plus a rendered Graphviz string, so
    # a consumer can pull `data.dot` and pipe straight to `dot -Tsvg`.
    if fmt == "dot":
        result.dot = _render_dot(result.nodes, result.edges)
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped
