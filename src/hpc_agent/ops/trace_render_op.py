"""``trace-render`` — the four data-trace projections, code-rendered (T5).

A read-only ``query`` primitive (``docs/design/data-trace.md`` Wave 3, T5). Given
a trace scope — DIRECT ``{scope_kind, scope_id, task?}`` or the REFERENCE lookup
``{cmd_sha}`` / ``{profile}`` (latest-by via a sidecar join, Class B) — it reads
ONE task's trace out of the canonical store, joins the run/audit sidecar for a
SELF-DESCRIBING header, and renders the FOUR deterministic views over the
records + the ONE atom-schema registry (:mod:`hpc_agent.state.data_trace`):

* (a) **row waterfall** — stage-by-stage rows in/out + declared drops + the
  conservation arithmetic, with the generic invariant flags beneath;
* (b) **label-chain line** — each tracked label's value chain across stages
  (the units ledger, generalized), with continuity flags beneath;
* (c) **feature lineage** — the ``col_set`` add/drop delta per stage + a
  column→birth-stage map;
* (d) **sketch table** — ``value_sketch`` / ``null_count`` per column per stage.

It is a PURE projection (the ``ops/run_story.py`` / ``ops/story_render.py``
posture): no SSH, no scheduler, no write, no store mutation. Derived state
recomputed from the on-disk records on every call, so it can never drift from a
second source of truth.

THE NEVER-JUDGMENT PIN (data-trace.md §Enforcement — grep-testable): this render
source contains NO verdict vocabulary. Flags render as the records' OWN ``{rule,
detail}`` text; core points, the scientist concludes (the pointing doctrine
applied to data). Trusted-display posture: the returned ``render`` is relayed
VERBATIM. Absence — no trace recorded for a scope — is an honest result carried
on ``present``/``skipped``, never an error.

This file lives at the ``ops/`` *role root* (sibling to ``run_story.py`` /
``story_render.py``) because it reads across subjects — the ``state`` trace store
and the ``state`` run sidecar. The subject-imports lint short-circuits for
role-root files, so the cross-subject reads here are allowed by construction.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.trace_render import (
    TraceFeatureRow,
    TraceFlag,
    TraceRenderResult,
    TraceRenderSpec,
    TraceSketchRow,
    TraceWaterfallRow,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state import data_trace as dt
from hpc_agent.state.runs import find_existing_runs, find_run_by_cmd_sha, read_run_sidecar

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["trace_render"]

#: The fixed value_sketch fields the sketch view surfaces, in render order.
_SKETCH_SCALARS: tuple[str, ...] = ("min", "mean", "std", "max")

#: The self-describing header fields lifted off the run sidecar (identity only —
#: never a metric). Order-preserving so the header renders stably.
_HEADER_SIDECAR_KEYS: tuple[str, ...] = (
    "run_id",
    "cmd_sha",
    "submitted_at",
    "profile",
    "cluster",
    "campaign_id",
)


# ── scope resolution (direct + the Class-B reference lookups) ─────────────────


def _resolve_scope(
    experiment_dir: Path, spec: TraceRenderSpec
) -> tuple[str, str, str, dict[str, Any]]:
    """Resolve the spec to ``(scope_kind, scope_id, resolved_from, sidecar)``.

    DIRECT: the scope is the spec's own ``scope_kind``/``scope_id``; the sidecar
    join is attempted only for a ``run`` scope (an ``audit``/``local`` scope has
    no run sidecar — the header degrades honestly). REFERENCE (Class B): resolve
    the newest run matching ``cmd_sha`` (parameter identity) or the newest run
    carrying the ``profile`` label, then trace under its ``("run", run_id)`` key.
    An unresolved reference returns ``scope_id=""`` (the absence path renders it).
    """
    if spec.cmd_sha is not None:
        run_id = _latest_run_by_cmd_sha(experiment_dir, spec.cmd_sha)
        return "run", run_id or "", "cmd_sha", _safe_sidecar(experiment_dir, run_id)
    if spec.profile is not None:
        run_id = _latest_run_by_profile(experiment_dir, spec.profile)
        return "run", run_id or "", "profile", _safe_sidecar(experiment_dir, run_id)
    # DIRECT — the validator guarantees both are present together.
    scope_kind = spec.scope_kind or ""
    scope_id = spec.scope_id or ""
    sidecar = _safe_sidecar(experiment_dir, scope_id) if scope_kind == "run" else {}
    return scope_kind, scope_id, "spec", sidecar


def _safe_sidecar(experiment_dir: Path, run_id: str | None) -> dict[str, Any]:
    """A run's sidecar dict, or ``{}`` when absent (a missing sidecar is DATA)."""
    if not run_id:
        return {}
    try:
        return read_run_sidecar(experiment_dir, run_id)
    except FileNotFoundError:
        return {}


def _latest_run_by_cmd_sha(experiment_dir: Path, cmd_sha: str) -> str | None:
    """The newest run whose sidecar records *cmd_sha* (the T1/runs dedup identity)."""
    path = find_run_by_cmd_sha(experiment_dir, cmd_sha)
    return path.stem if path is not None else None


def _latest_run_by_profile(experiment_dir: Path, profile: str) -> str | None:
    """The newest run whose sidecar's literal ``profile`` label equals *profile*.

    A mechanical ``latest-by-profile`` (A7 Class B): ``find_existing_runs`` yields
    sidecars newest-first, so the first match is the freshest exemplar. Core stays
    agnostic to WHICH profile is the reference — the caller names it; core joins.
    """
    for path in find_existing_runs(experiment_dir):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("profile") == profile:
            return path.stem
    return None


# ── the four views (PURE functions over records + the atom registry) ──────────


def _atoms(record: dict[str, Any]) -> dict[str, Any]:
    atoms = record.get("atoms")
    return atoms if isinstance(atoms, dict) else {}


def build_waterfall(records: Sequence[dict[str, Any]]) -> list[TraceWaterfallRow]:
    """View (a): the row waterfall — rows in/out, declared drops, conservation.

    ``rows_in`` is the predecessor row_count.rows; a stage with no ``row_count``
    atom breaks the chain (``rows_in`` resets to null at the next counted stage),
    exactly as :func:`data_trace.check_row_conservation` reads it, so the table
    and the conservation flags always agree.
    """
    rows: list[TraceWaterfallRow] = []
    prev_rows: int | None = None
    for rec in records:
        rc = _atoms(rec).get("row_count")
        rows_v = rc.get("rows") if isinstance(rc, dict) else None
        dropped_v = rc.get("dropped") if isinstance(rc, dict) else None
        if not _is_int(rows_v) or not _is_int(dropped_v):
            rows.append(
                TraceWaterfallRow(stage=_stage(rec), seq=_seq(rec), rows_out=None, rows_in=None)
            )
            prev_rows = None
            continue
        rows_out = rows_v
        dropped = dropped_v
        expected = (prev_rows - dropped) if prev_rows is not None else None
        rows.append(
            TraceWaterfallRow(
                stage=_stage(rec),
                seq=_seq(rec),
                rows_in=prev_rows,
                dropped=dropped,
                rows_out=rows_out,
                expected=expected,
            )
        )
        prev_rows = rows_out
    return rows


def build_label_chains(records: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    """View (b): per tracked label, its ``stage=value`` chain across stages.

    The units ledger generalized: for each label name a ``label_chain`` atom
    carries, list its value at every stage that emits it, in emission order. A
    break (a label that vanishes) is surfaced by the continuity flag, never
    fabricated here.
    """
    chains: dict[str, list[str]] = {}
    for rec in records:
        lc = _atoms(rec).get("label_chain")
        if not isinstance(lc, dict):
            continue
        stage = _stage(rec)
        for name, value in lc.items():
            if not isinstance(name, str):
                continue
            chains.setdefault(name, []).append(f"{stage}={_scalar(value)}")
    return chains


def build_feature_lineage(
    records: Sequence[dict[str, Any]],
) -> tuple[list[TraceFeatureRow], dict[str, str]]:
    """View (c): the per-stage col_set add/drop delta + a column→birth-stage map.

    ``added`` / ``dropped`` are sorted set-deltas versus the previous stage that
    carried a ``col_set`` atom; ``births`` records the FIRST stage each column
    appeared. Both are sorted for a byte-stable render.
    """
    lineage: list[TraceFeatureRow] = []
    births: dict[str, str] = {}
    prev_cols: frozenset[str] | None = None
    for rec in records:
        cs = _atoms(rec).get("col_set")
        if not isinstance(cs, dict) or not isinstance(cs.get("columns"), list):
            continue
        cols = frozenset(c for c in cs["columns"] if isinstance(c, str))
        stage = _stage(rec)
        for col in cols:
            births.setdefault(col, stage)
        added = sorted(cols - prev_cols) if prev_cols is not None else sorted(cols)
        dropped = sorted(prev_cols - cols) if prev_cols is not None else []
        lineage.append(TraceFeatureRow(stage=stage, added=added, dropped=dropped))
        prev_cols = cols
    return lineage, dict(sorted(births.items()))


def build_sketch(records: Sequence[dict[str, Any]]) -> list[TraceSketchRow]:
    """View (d): per (stage, column) ``value_sketch`` + ``null_count``.

    One row per column that carries either atom at a stage (columns sorted for a
    stable render). Absent fields stay null — a stage that measured only nulls,
    or only a sketch, discloses exactly what it measured.
    """
    out: list[TraceSketchRow] = []
    for rec in records:
        atoms = _atoms(rec)
        raw_sketch = atoms.get("value_sketch")
        raw_nulls = atoms.get("null_count")
        sketch: dict[str, Any] = raw_sketch if isinstance(raw_sketch, dict) else {}
        nulls: dict[str, Any] = raw_nulls if isinstance(raw_nulls, dict) else {}
        columns = sorted({*sketch, *nulls})
        stage = _stage(rec)
        for col in columns:
            raw_s = sketch.get(col)
            s: dict[str, Any] = raw_s if isinstance(raw_s, dict) else {}
            null_v = nulls.get(col)
            row = TraceSketchRow(
                stage=stage,
                column=col,
                null_count=int(null_v) if _is_int(null_v) else None,
                q05=_num(_quant(s, "q05")),
                q50=_num(_quant(s, "q50")),
                q95=_num(_quant(s, "q95")),
            )
            for field in _SKETCH_SCALARS:
                setattr(row, field, _num(s.get(field)))
            out.append(row)
    return out


def collect_flags(records: Sequence[dict[str, Any]]) -> list[TraceFlag]:
    """All flags: the three generic invariants + every record-carried opaque flag.

    The generic invariants are pure arithmetic over atoms (:func:`data_trace.
    run_invariants`); the record-carried flags are the pack/program checks core
    renders but never interprets. Each is surfaced verbatim in the ``{rule,
    detail, evidence}`` finding shape.
    """
    flags: list[TraceFlag] = [_as_flag(f) for f in dt.run_invariants(records)]
    for rec in records:
        for f in rec.get("flags") or []:
            if isinstance(f, dict) and isinstance(f.get("rule"), str):
                flags.append(_as_flag(f))
    return flags


# ── the markdown render (deterministic; NO verdict vocabulary) ────────────────


def render_views(
    *,
    scope_kind: str,
    scope_id: str,
    task: int,
    resolved_from: str,
    present: bool,
    skipped: str,
    header: dict[str, Any],
    stage_count: int,
    trace_sha: str,
    waterfall: Sequence[TraceWaterfallRow],
    label_chains: dict[str, list[str]],
    feature_lineage: Sequence[TraceFeatureRow],
    feature_births: dict[str, str],
    sketch: Sequence[TraceSketchRow],
    flags: Sequence[TraceFlag],
) -> str:
    """Render the self-describing header + the four views as deterministic markdown.

    Pure string work — same inputs yield byte-identical output on every platform.
    The header arrives cold-reader-first (A6): scope identity, resolution path,
    and the sidecar join. When ``present`` is False the body is the one honest
    absence line; the render never fabricates an empty view as data.
    """
    title = f"# Data trace — {scope_kind}:{scope_id or '(unresolved)'} (task {task})"
    lines: list[str] = [title, ""]
    lines.append(f"- resolved_from: {resolved_from}")
    for key in _HEADER_SIDECAR_KEYS:
        val = header.get(key)
        if val:
            lines.append(f"- {key}: {val}")
    lines.append(f"- stage_count: {stage_count}")
    if trace_sha:
        lines.append(f"- trace_sha: {trace_sha}")
    lines.append("")

    if not present:
        lines.append(skipped or "no trace recorded for this scope")
        return "\n".join(lines).rstrip() + "\n"

    _render_waterfall(lines, waterfall, flags)
    _render_label_chains(lines, label_chains, flags)
    _render_feature_lineage(lines, feature_lineage, feature_births)
    _render_sketch(lines, sketch)
    _render_other_flags(lines, flags)
    return "\n".join(lines).rstrip() + "\n"


def _render_waterfall(
    lines: list[str], waterfall: Sequence[TraceWaterfallRow], flags: Sequence[TraceFlag]
) -> None:
    lines.append("## Row waterfall")
    lines.append("")
    if not waterfall:
        lines.append("(no row_count atoms recorded)")
    else:
        lines.append("| stage | seq | rows_in | dropped | rows_out | expected |")
        lines.append("|---|---|---|---|---|---|")
        for r in waterfall:
            lines.append(
                f"| {r.stage} | {r.seq} | {_cell(r.rows_in)} | {_cell(r.dropped)} "
                f"| {_cell(r.rows_out)} | {_cell(r.expected)} |"
            )
    _append_flag_lines(lines, flags, "row_conservation")
    lines.append("")


def _render_label_chains(
    lines: list[str], label_chains: dict[str, list[str]], flags: Sequence[TraceFlag]
) -> None:
    lines.append("## Label chains")
    lines.append("")
    if not label_chains:
        lines.append("(no label_chain atoms recorded)")
    else:
        for name in sorted(label_chains):
            lines.append(f"- {name}: " + " -> ".join(label_chains[name]))
    _append_flag_lines(lines, flags, "label_chain_break")
    lines.append("")


def _render_feature_lineage(
    lines: list[str],
    feature_lineage: Sequence[TraceFeatureRow],
    feature_births: dict[str, str],
) -> None:
    lines.append("## Feature lineage")
    lines.append("")
    if not feature_lineage:
        lines.append("(no col_set atoms recorded)")
    else:
        lines.append("| stage | added | dropped |")
        lines.append("|---|---|---|")
        for r in feature_lineage:
            added = ", ".join(r.added) or "-"
            dropped = ", ".join(r.dropped) or "-"
            lines.append(f"| {r.stage} | {added} | {dropped} |")
        if feature_births:
            lines.append("")
            for col in sorted(feature_births):
                lines.append(f"- {col} born at {feature_births[col]}")
    lines.append("")


def _render_sketch(lines: list[str], sketch: Sequence[TraceSketchRow]) -> None:
    lines.append("## Sketch")
    lines.append("")
    if not sketch:
        lines.append("(no value_sketch / null_count atoms recorded)")
    else:
        lines.append("| stage | column | null_count | min | mean | std | max | q05 | q50 | q95 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in sketch:
            lines.append(
                f"| {r.stage} | {r.column} | {_cell(r.null_count)} | {_cell(r.min)} "
                f"| {_cell(r.mean)} | {_cell(r.std)} | {_cell(r.max)} | {_cell(r.q05)} "
                f"| {_cell(r.q50)} | {_cell(r.q95)} |"
            )
    lines.append("")


def _render_other_flags(lines: list[str], flags: Sequence[TraceFlag]) -> None:
    """Render the flags NOT already surfaced beside a view (seq + opaque pack flags)."""
    surfaced = {"row_conservation", "label_chain_break"}
    rest = [f for f in flags if f.rule not in surfaced]
    lines.append("## Flags")
    lines.append("")
    if not rest:
        lines.append("(none)")
    else:
        for f in rest:
            lines.append(f"- {f.rule}: {f.detail}")
    lines.append("")


def _append_flag_lines(lines: list[str], flags: Sequence[TraceFlag], rule: str) -> None:
    matching = [f for f in flags if f.rule == rule]
    if matching:
        lines.append("")
        for f in matching:
            lines.append(f"- {f.rule}: {f.detail}")


# ── small pure helpers ────────────────────────────────────────────────────────


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _quant(sketch: Any, key: str) -> Any:
    if isinstance(sketch, dict):
        quant = sketch.get("quantiles")
        if isinstance(quant, dict):
            return quant.get(key)
    return None


def _stage(record: dict[str, Any]) -> str:
    stage = record.get("stage")
    return stage if isinstance(stage, str) and stage else "(unknown)"


def _seq(record: dict[str, Any]) -> int:
    seq = record.get("seq")
    return int(seq) if _is_int(seq) else 0


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def _cell(value: Any) -> str:
    """One table cell: a null value renders as a bare dash (never a fabricated 0)."""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _as_flag(flag: dict[str, Any]) -> TraceFlag:
    return TraceFlag(
        rule=str(flag.get("rule") or ""),
        detail=str(flag.get("detail") or ""),
        evidence=dict(flag.get("evidence") or {}),
    )


def _records_sha(records: Sequence[dict[str, Any]]) -> str:
    """Fingerprint over the records (routes through T1's canonical helper)."""
    return dt.records_sha(list(records))


# ── the primitive ─────────────────────────────────────────────────────────────


@primitive(
    name="trace-render",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Render one task's data trace as FOUR deterministic markdown views — "
            "row waterfall (with conservation flags), label-chain line, feature "
            "lineage (col_set deltas + column births), and the sketch table "
            "(value_sketch / null_count per column per stage) — under a "
            "self-describing run/config header. Read-only, no SSH. Selectors "
            "(exactly one): DIRECT {scope_kind, scope_id, task?}, or the REFERENCE "
            "lookup {cmd_sha} / {profile} (latest-by via a sidecar join). Absence "
            "is honest: 'no trace recorded for this scope' rides present/skipped, "
            "never an error. The render carries NO verdict vocabulary — the trace "
            "SHOWS, the scientist concludes; relay it verbatim."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=TraceRenderSpec,
        schema_ref=SchemaRef(input="trace_render"),
    ),
    agent_facing=True,
)
def trace_render(*, experiment_dir: Path, spec: TraceRenderSpec) -> TraceRenderResult:
    """Read one task's trace, join its sidecar, and render the four views.

    Resolves the scope (direct or the Class-B reference lookup), reads the
    records via the tolerant store read (:func:`data_trace.read_trace`), builds
    the four views + the flag list over the records + the ONE atom registry, and
    renders deterministic markdown. Idempotent by construction: derived state
    recomputed from disk on every call, no store mutation, no write.

    Absence is DATA, never an error: an unresolved reference lookup or a scope
    with no recorded trace yields ``present=False`` + a ``skipped`` disclosure
    and empty views — never a raised exception. Raises :class:`errors.SpecInvalid`
    only for a structurally invalid scope key (surfaced by the store path guard).
    """
    experiment_dir = Path(experiment_dir)
    scope_kind, scope_id, resolved_from, sidecar = _resolve_scope(experiment_dir, spec)

    header = {k: sidecar[k] for k in _HEADER_SIDECAR_KEYS if sidecar.get(k)}

    # An unresolved reference lookup — no run matched — is an honest absence.
    if not scope_id:
        selector = spec.cmd_sha if resolved_from == "cmd_sha" else spec.profile
        skipped = f"no run matches {resolved_from}={selector!r}"
        return _absent_result(
            scope_kind, "", spec.task, resolved_from, header, skipped, spec.markdown
        )

    try:
        records = dt.read_trace(experiment_dir, scope_kind, scope_id, spec.task)
    except errors.SpecInvalid:
        raise
    except Exception as exc:  # noqa: BLE001 — a store read must degrade, never crash the query
        raise errors.SpecInvalid(f"cannot read trace for {scope_kind}:{scope_id}: {exc}") from exc

    if not records:
        skipped = f"no trace recorded for {scope_kind}:{scope_id} task {spec.task}"
        return _absent_result(
            scope_kind, scope_id, spec.task, resolved_from, header, skipped, spec.markdown
        )

    waterfall = build_waterfall(records)
    label_chains = build_label_chains(records)
    feature_lineage, feature_births = build_feature_lineage(records)
    sketch = build_sketch(records)
    flags = collect_flags(records)
    trace_sha = _records_sha(records)

    render = (
        render_views(
            scope_kind=scope_kind,
            scope_id=scope_id,
            task=spec.task,
            resolved_from=resolved_from,
            present=True,
            skipped="",
            header=header,
            stage_count=len(records),
            trace_sha=trace_sha,
            waterfall=waterfall,
            label_chains=label_chains,
            feature_lineage=feature_lineage,
            feature_births=feature_births,
            sketch=sketch,
            flags=flags,
        )
        if spec.markdown
        else ""
    )

    return TraceRenderResult(
        scope_kind=scope_kind,
        scope_id=scope_id,
        task=spec.task,
        resolved_from=resolved_from,
        present=True,
        skipped="",
        stage_count=len(records),
        trace_sha=trace_sha,
        header=header,
        waterfall=waterfall,
        label_chains=label_chains,
        feature_lineage=feature_lineage,
        feature_births=feature_births,
        sketch=sketch,
        flags=flags,
        render=render,
    )


def _absent_result(
    scope_kind: str,
    scope_id: str,
    task: int,
    resolved_from: str,
    header: dict[str, Any],
    skipped: str,
    markdown: bool,
) -> TraceRenderResult:
    """Build the honest absence result — empty views + the ``skipped`` disclosure."""
    render = (
        render_views(
            scope_kind=scope_kind,
            scope_id=scope_id,
            task=task,
            resolved_from=resolved_from,
            present=False,
            skipped=skipped,
            header=header,
            stage_count=0,
            trace_sha="",
            waterfall=[],
            label_chains={},
            feature_lineage=[],
            feature_births={},
            sketch=[],
            flags=[],
        )
        if markdown
        else ""
    )
    return TraceRenderResult(
        scope_kind=scope_kind,
        scope_id=scope_id,
        task=task,
        resolved_from=resolved_from,
        present=False,
        skipped=skipped,
        stage_count=0,
        trace_sha="",
        header=header,
        render=render,
    )
