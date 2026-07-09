"""``trace-diff`` — overlay two traces; localize the FIRST divergence.

Projection 5 of ``docs/design/data-trace.md``: two trace keys → a per-STAGE,
per-ATOM comparison, with the earliest diverging ``(stage, atom)`` highlighted
(canary-vs-local, arm-vs-arm, today-vs-last-known-good). Read-only,
``agent_facing`` query — a pure local read of the trace store (T1's
``read_trace``), never SSH.

**One semantics definition.** Every comparison dispatches through T1's
``comparison_for(atom)`` (``state/data_trace.py``) — the render, the fingerprint
interlock, and this diff share the ONE registry. There is deliberately NO local
``{atom: semantics}`` table here (the route-through pin): this module maps a
SEMANTICS TOKEN to a comparator, never an atom name to a semantics.

**Differences are facts, never verdicts** (the token pin, design §"Projections"):
``row_count rows 246059 → 218905``, never "wrong". The render carries no verdict
vocabulary (grep-pinned); a divergence is disclosed, a conclusion is the
human's. **Absence is honest**: a key the store never held is disclosed
(``present: false``), never fabricated as a match.

**Tolerance is caller-owned** (the no-invented-epsilon rule): the tolerance-class
atoms (``value_sketch``, ``duration_ms``, ``peak_mb``) compare under the caller's
``TraceTolerance``; absent (or all-absent) → EXACT. Mirrors how
``verify-reproduction`` takes a ReproTolerance. Every other atom is always exact.

**Stage alignment** is by ``(seq, stage)``: matched stages compare their atoms;
an unmatched stage (present on one side only) is a NAMED STRUCTURAL divergence.
First-divergence is the smallest-seq position that parts, structural or atomic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.trace_diff import (
    FirstDivergence,
    TraceDiffResult,
    TraceDiffSpec,
    TraceEndpoint,
    TraceKey,
    TraceTolerance,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state.data_trace import (
    ATOM_REGISTRY,
    QUANTILE_KEYS,
    TRACE_SCHEMA_VERSION,
    comparison_for,
    make_flag,
    read_trace,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

__all__ = ["trace_diff", "render_trace_diff"]


# --- tolerance resolution (mirrors verify-reproduction's _resolve_key_tol) -----


def _is_number(value: Any) -> bool:
    """True for a real numeric value — ``bool`` excluded (compares by equality)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _resolve_tol(
    tolerance: TraceTolerance | None, key: str
) -> tuple[float | None, float | None, bool]:
    """Resolve ``(abs_tol, rel_tol, supplied)`` for one tolerance key.

    A ``per_key`` entry FULLY replaces the default for that key (even an
    all-absent entry → exact for that key). ``supplied`` is False when both
    resolved bounds are absent — that key is compared EXACTLY (no invented
    epsilon).
    """
    if tolerance is None:
        return None, None, False
    override = tolerance.per_key.get(key)
    if override is not None:
        abs_tol, rel_tol = override.abs_tol, override.rel_tol
    else:
        abs_tol, rel_tol = tolerance.default_abs_tol, tolerance.default_rel_tol
    return abs_tol, rel_tol, (abs_tol is not None or rel_tol is not None)


def _within_tol(a: float, b: float, abs_tol: float | None, rel_tol: float | None) -> bool:
    """True when ``a`` and ``b`` are within the supplied band (either bound satisfies)."""
    abs_diff = abs(a - b)
    if abs_tol is not None and abs_diff <= abs_tol:
        return True
    denom = max(abs(a), abs(b))
    rel_diff = abs_diff / denom if denom else 0.0
    return bool(rel_tol is not None and rel_diff <= rel_tol)


def _num_differs(a: Any, b: Any, key: str, tolerance: TraceTolerance | None) -> bool:
    """A tolerance-class numeric field diverges? Exact when no tolerance supplied."""
    if not (_is_number(a) and _is_number(b)):
        return bool(a != b)
    abs_tol, rel_tol, supplied = _resolve_tol(tolerance, key)
    if not supplied:
        return float(a) != float(b)
    return not _within_tol(float(a), float(b), abs_tol, rel_tol)


# --- per-semantics comparators (a SEMANTICS token → a comparator; never an
#     atom → semantics table — the route-through pin) --------------------------


def _cmp_exact(atom: str, a: Any, b: Any) -> list[str]:
    """Byte/value equality. Renders row_count / order_integrity / digest factually."""
    if atom == "row_count" and isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        if a.get("rows") != b.get("rows"):
            out.append(f"row_count rows {a.get('rows')} → {b.get('rows')}")
        if a.get("dropped") != b.get("dropped"):
            out.append(f"row_count dropped {a.get('dropped')} → {b.get('dropped')}")
        return out
    if atom == "order_integrity" and isinstance(a, dict) and isinstance(b, dict):
        out = []
        for col in sorted(set(a) | set(b)):
            av, bv = a.get(col), b.get(col)
            if av == bv:
                continue
            if col not in a:
                out.append(f"order_integrity[{col}] present on B only")
            elif col not in b:
                out.append(f"order_integrity[{col}] present on A only")
            elif isinstance(av, dict) and isinstance(bv, dict):
                for field in ("monotonic", "dups", "gaps"):
                    if av.get(field) != bv.get(field):
                        out.append(
                            f"order_integrity[{col}].{field} {av.get(field)} → {bv.get(field)}"
                        )
            else:
                out.append(f"order_integrity[{col}] {av} → {bv}")
        return out
    if atom == "digest":
        return [] if a == b else [f"digest {_short(a)} → {_short(b)}"]
    return [] if a == b else [f"{atom} {a} → {b}"]


def _cmp_set_delta(a: Any, b: Any) -> list[str]:
    """Set difference over col_set: added on B, dropped from A."""
    sa = set(a.get("columns", [])) if isinstance(a, dict) else set()
    sb = set(b.get("columns", [])) if isinstance(b, dict) else set()
    added, dropped = sorted(sb - sa), sorted(sa - sb)
    if not added and not dropped:
        return []
    return [f"col_set added {added} dropped {dropped}"]


def _cmp_per_key(atom: str, a: Any, b: Any) -> list[str]:
    """Exact per column key (null_count): changed / one-side-only columns."""
    a = a if isinstance(a, dict) else {}
    b = b if isinstance(b, dict) else {}
    out: list[str] = []
    for col in sorted(set(a) | set(b)):
        if col in a and col in b:
            if a[col] != b[col]:
                out.append(f"{atom}[{col}] {a[col]} → {b[col]}")
        elif col in a:
            out.append(f"{atom}[{col}] present on A only (={a[col]})")
        else:
            out.append(f"{atom}[{col}] present on B only (={b[col]})")
    return out


def _cmp_tolerance(atom: str, a: Any, b: Any, tolerance: TraceTolerance | None) -> list[str]:
    """Tolerance-class atoms: value_sketch per column/field, scalar cost. Caller-owned."""
    if atom in ("duration_ms", "peak_mb"):
        if _num_differs(a, b, atom, tolerance):
            return [f"{atom} {a} → {b}"]
        return []
    # value_sketch: {col: {min,max,mean,std,quantiles{q05,q50,q95}}}
    a = a if isinstance(a, dict) else {}
    b = b if isinstance(b, dict) else {}
    out: list[str] = []
    for col in sorted(set(a) | set(b)):
        if col not in a:
            out.append(f"value_sketch[{col}] present on B only")
            continue
        if col not in b:
            out.append(f"value_sketch[{col}] present on A only")
            continue
        key = f"value_sketch:{col}"
        sa, sb = a[col], b[col]
        if not (isinstance(sa, dict) and isinstance(sb, dict)):
            if sa != sb:
                out.append(f"value_sketch[{col}] {sa} → {sb}")
            continue
        for field in ("min", "max", "mean", "std"):
            if _num_differs(sa.get(field), sb.get(field), key, tolerance):
                out.append(f"value_sketch[{col}].{field} {sa.get(field)} → {sb.get(field)}")
        qa = _quantiles(sa)
        qb = _quantiles(sb)
        for q in QUANTILE_KEYS:
            if _num_differs(qa.get(q), qb.get(q), key, tolerance):
                out.append(f"value_sketch[{col}].{q} {qa.get(q)} → {qb.get(q)}")
    return out


def _cmp_chain(a: Any, b: Any) -> list[str]:
    """Equality-chain (label_chain): the tracked labels compared at this stage.

    The cross-stage continuity is T1's invariant; here we compare the two
    stages' label bundles — a differing / one-side-only tracked label is the #1
    historical mirage class (a units/target inversion) read as a divergence.
    """
    a = a if isinstance(a, dict) else {}
    b = b if isinstance(b, dict) else {}
    out: list[str] = []
    for label in sorted(set(a) | set(b)):
        if label in a and label in b:
            if a[label] != b[label]:
                out.append(f"label_chain[{label}] {a[label]!r} → {b[label]!r}")
        elif label in a:
            out.append(f"label_chain[{label}] present on A only (={a[label]!r})")
        else:
            out.append(f"label_chain[{label}] present on B only (={b[label]!r})")
    return out


def _cmp_endpoints(a: Any, b: Any) -> list[str]:
    """Exact-endpoints (span): first/last of an ordered column, per column."""
    a = a if isinstance(a, dict) else {}
    b = b if isinstance(b, dict) else {}
    out: list[str] = []
    for col in sorted(set(a) | set(b)):
        if col not in a:
            out.append(f"span[{col}] present on B only")
            continue
        if col not in b:
            out.append(f"span[{col}] present on A only")
            continue
        ea, eb = a[col], b[col]
        ea = ea if isinstance(ea, dict) else {}
        eb = eb if isinstance(eb, dict) else {}
        for end in ("first", "last"):
            if ea.get(end) != eb.get(end):
                out.append(f"span[{col}].{end} {ea.get(end)!r} → {eb.get(end)!r}")
    return out


def _quantiles(sketch: Any) -> dict[str, Any]:
    """The quantiles sub-object of a value_sketch bundle, or ``{}`` when absent."""
    if isinstance(sketch, dict):
        q = sketch.get("quantiles")
        if isinstance(q, dict):
            return q
    return {}


def _short(value: Any) -> str:
    """Truncate a digest to its recognisable head (facts, not the whole sha)."""
    text = str(value)
    return text[:12] if len(text) > 12 else text


def _compare_atom(atom: str, a: Any, b: Any, tolerance: TraceTolerance | None) -> list[str]:
    """Dispatch ONE atom's comparison via T1's semantics token (route-through)."""
    sem = comparison_for(atom)  # the ONE semantics definition (T1)
    if sem == "exact":
        return _cmp_exact(atom, a, b)
    if sem == "set-delta":
        return _cmp_set_delta(a, b)
    if sem == "exact-per-key":
        return _cmp_per_key(atom, a, b)
    if sem == "tolerance":
        return _cmp_tolerance(atom, a, b, tolerance)
    if sem == "equality-chain":
        return _cmp_chain(a, b)
    if sem == "exact-endpoints":
        return _cmp_endpoints(a, b)
    # A registry that grows a semantics token without a comparator here is a
    # framework bug — refuse loudly rather than silently render a match.
    raise errors.SpecInvalid(  # pragma: no cover (closed set)
        f"trace-diff: no comparator for semantics {sem!r} (atom {atom!r})"
    )


# --- stage alignment + the divergence walk -----------------------------------


def _stage_key(record: Mapping[str, Any]) -> tuple[int, str]:
    """The (seq, stage) alignment key; seq is monotonic per trace (T1 invariant)."""
    seq = record.get("seq")
    seq_i = seq if isinstance(seq, int) and not isinstance(seq, bool) else -1
    return (seq_i, str(record.get("stage")))


def _atoms(record: Mapping[str, Any]) -> dict[str, Any]:
    atoms = record.get("atoms")
    return atoms if isinstance(atoms, dict) else {}


def _stage_divergences(
    rec_a: Mapping[str, Any], rec_b: Mapping[str, Any], tolerance: TraceTolerance | None
) -> list[dict[str, Any]]:
    """Per-atom divergences for one MATCHED stage, in registry (deterministic) order."""
    atoms_a, atoms_b = _atoms(rec_a), _atoms(rec_b)
    out: list[dict[str, Any]] = []
    for atom in ATOM_REGISTRY:  # registry order → byte-stable
        in_a, in_b = atom in atoms_a, atom in atoms_b
        if not (in_a or in_b):
            continue
        if in_a and in_b:
            for detail in _compare_atom(atom, atoms_a[atom], atoms_b[atom], tolerance):
                out.append({"atom": atom, "kind": comparison_for(atom), "detail": detail})
        elif in_a:
            out.append(
                {"atom": atom, "kind": "atom_presence", "detail": f"atom {atom} present on A only"}
            )
        else:
            out.append(
                {"atom": atom, "kind": "atom_presence", "detail": f"atom {atom} present on B only"}
            )
    return out


def _diff_traces(
    records_a: Sequence[dict[str, Any]],
    records_b: Sequence[dict[str, Any]],
    tolerance: TraceTolerance | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], FirstDivergence | None]:
    """Align by (seq, stage) and walk seq-order → (stages, structural, first_divergence)."""
    a_by_key = {_stage_key(r): r for r in records_a}
    b_by_key = {_stage_key(r): r for r in records_b}
    all_keys = sorted(set(a_by_key) | set(b_by_key))  # (seq, stage) → smallest-seq first

    stages: list[dict[str, Any]] = []
    structural: list[dict[str, Any]] = []
    first: FirstDivergence | None = None

    for seq, stage in all_keys:
        in_a, in_b = (seq, stage) in a_by_key, (seq, stage) in b_by_key
        if in_a and in_b:
            divs = _stage_divergences(a_by_key[(seq, stage)], b_by_key[(seq, stage)], tolerance)
            stages.append({"stage": stage, "seq": seq, "side": "both", "divergences": divs})
            if first is None and divs:
                d0 = divs[0]
                first = FirstDivergence(
                    stage=stage, seq=seq, atom=d0["atom"], kind=d0["kind"], detail=d0["detail"]
                )
        else:
            side = "a_only" if in_a else "b_only"
            where = "A" if in_a else "B"
            detail = f"stage {stage!r} (seq {seq}) present on {where} only"
            stages.append({"stage": stage, "seq": seq, "side": side, "divergences": []})
            structural.append(
                make_flag(
                    "stage_unmatched",
                    detail,
                    {"stage": stage, "seq": seq, "side": side},
                )
            )
            if first is None:
                first = FirstDivergence(
                    stage=stage, seq=seq, atom=None, kind="structural", detail=detail
                )
    return stages, structural, first


# --- the render (self-describing, byte-stable, NO verdict vocabulary) ---------


def render_trace_diff(result: TraceDiffResult) -> str:
    """Deterministic markdown overlay of two traces — facts only, no verdicts.

    Self-describing header (both keys + presence), first-divergence lead, then
    one line per stage. This is the trace-diff-owned markdown helper; a later
    unification with T5 ``trace-render``'s helper is noted in the module doc.
    """
    lines: list[str] = ["# trace-diff: A vs B", ""]
    for side, ep in (("A", result.a), ("B", result.b)):
        loc = f"`{ep.scope_kind}/{ep.scope_id}` task-{ep.task}"
        if ep.present:
            lines.append(f"- {side}: {loc} — {ep.stage_count} stages")
        else:
            lines.append(f"- {side}: {loc} — absent (no trace in the store)")
    lines.append("")

    if result.clean:
        lines.append("first divergence: none — the two traces are identical")
    elif result.first_divergence is not None:
        fd = result.first_divergence
        atom = f" · atom `{fd.atom}`" if fd.atom else " · structural"
        lines.append(f"first divergence: stage `{fd.stage}` (seq {fd.seq}){atom}")
        lines.append(f"    {fd.detail}")
    lines.append("")

    lines.append("stages:")
    for st in result.stages:
        head = f"- seq {st['seq']} `{st['stage']}`"
        if st["side"] != "both":
            where = "A" if st["side"] == "a_only" else "B"
            lines.append(f"{head} — present on {where} only")
        elif not st["divergences"]:
            lines.append(f"{head} — identical")
        else:
            lines.append(f"{head}")
            for d in st["divergences"]:
                lines.append(f"    {d['detail']}")
    return "\n".join(lines) + "\n"


# --- the primitive -----------------------------------------------------------


@primitive(
    name="trace-diff",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    agent_facing=True,
    cli=CliShape(
        help=(
            "Overlay TWO traces from the local store and report, per stage and "
            "per atom, where their measurements diverge — dispatched through the "
            "ONE semantics registry (exact / set-delta / tolerance / "
            "exact-per-key / equality-chain / exact-endpoints). Highlights the "
            "FIRST diverging (stage, atom). Read-only, no SSH. Differences are "
            "FACTS (`row_count rows 100 → 90`), never verdicts; absence is "
            "disclosed. Tolerance is caller-owned (absent → exact)."
        ),
        spec_arg=True,
        spec_model=TraceDiffSpec,
        experiment_dir_arg=True,
        requires_ssh=False,
        schema_ref=SchemaRef(input="trace_diff"),
    ),
)
def trace_diff(experiment_dir: Path, *, spec: TraceDiffSpec) -> TraceDiffResult:
    """Overlay two store traces; localize + render the first divergence.

    Reads each side's task records from the local trace store (``read_trace``,
    tolerant — a missing key yields ``present: false``, disclosed never
    fabricated), aligns stages by ``(seq, stage)``, compares every atom through
    T1's ``comparison_for`` semantics, and highlights the earliest diverging
    ``(stage, atom)``. Always succeeds (exit-0): a divergence is the feature
    working, a FACT for the human to conclude on, never an error.
    """
    a_records = read_trace(experiment_dir, spec.a.scope_kind, spec.a.scope_id, spec.a.task)
    b_records = read_trace(experiment_dir, spec.b.scope_kind, spec.b.scope_id, spec.b.task)

    stages, structural, first = _diff_traces(a_records, b_records, spec.tolerance)
    clean = not structural and all(not s["divergences"] for s in stages)
    aligned = not structural

    endpoint_a = _endpoint(spec.a, a_records)
    endpoint_b = _endpoint(spec.b, b_records)

    result = TraceDiffResult(
        trace_schema_version=TRACE_SCHEMA_VERSION,
        a=endpoint_a,
        b=endpoint_b,
        clean=clean,
        aligned=aligned,
        tolerance_applied=(spec.tolerance.model_dump(mode="json") if spec.tolerance else None),
        first_divergence=first,
        stages=stages,
        structural=structural,
        render="",
    )
    result.render = render_trace_diff(result)
    return result


def _endpoint(key: TraceKey, records: Sequence[dict[str, Any]]) -> TraceEndpoint:
    """Echo one side's key + what the store held (presence disclosed)."""
    return TraceEndpoint(
        scope_kind=key.scope_kind,
        scope_id=key.scope_id,
        task=key.task,
        present=bool(records),
        stage_count=len(records),
    )
