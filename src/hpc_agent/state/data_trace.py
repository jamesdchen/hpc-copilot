"""The data trace — stage receipts for the pipeline (the audit's runtime twin).

Core substrate for ``docs/design/data-trace.md`` (Wave 1, task T1). An ATOM is
a named, typed, meaning-free measurement of tabular data flow; each atom carries
its COMPARISON SEMANTICS, which is what makes the diff engine discipline-generic.
This module owns the *core* layer only (the design's layer split):

* the record container (``{stage, section?, seq, atoms{}, flags[],
  trace_schema_version, created_at}``) and its shape validation,
* THE ATOM SCHEMA REGISTRY — one definition (:data:`ATOM_REGISTRY`) carrying,
  per atom, the value shape, the comparison semantics, and the OpenLineage
  facet courtesy note (Amendment 13). Render, diff, and the later fingerprint
  interlock all consume this ONE registry (the enforcement-row: one registry),
* the generic invariants as PURE functions (row conservation, label-chain
  continuity, seq monotonicity) — arithmetic over atoms with zero meaning,
* the store (``.hpc/traces/<scope_kind>/<scope_id>/task-<n>.jsonl``, one file
  per task) with a tolerant read, and
* :func:`ingest_trace` — validates every line, moves the transport file into
  the store, and journals ONE ``block="data-trace"`` decision record.

STDLIB-ONLY (the library-boundary guard, ``docs/internals/engineering-
principles.md`` §"Library knowledge in core"): pandas/numpy NEVER import here.
Core validates SHAPES and never touches frames — the pack's pandas-aware
emitter is the implementation that measures frames and emits (the receipts
seam: caller executes, core binds). Canonical serialization routes through
:func:`hpc_agent.state.determinism.canonical_sha` (the one canonicalization
every content sha in the system uses); trace bulk never enters the decision
journal — only the journaled sha does (design §"Storage").
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeGuard

from hpc_agent import errors
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.determinism import canonical_sha

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = [
    "TRACE_SCHEMA_VERSION",
    "DATA_TRACE_BLOCK",
    "TRACE_SCOPE_KINDS",
    "AtomSchema",
    "ATOM_REGISTRY",
    "ATOM_NAMES",
    "atom_schema",
    "comparison_for",
    "make_flag",
    "make_record",
    "validate_record",
    "record_sha",
    "records_sha",
    "check_row_conservation",
    "check_label_chain_continuity",
    "check_seq_monotonicity",
    "run_invariants",
    "trace_store_path",
    "write_trace",
    "read_trace",
    "ingest_trace",
]

#: Bump only on a breaking record-shape change; readers tolerate unknown extra
#: keys (forward-compat), so additive fields do NOT need a bump. Mirrors
#: OpenLineage's self-describing ``_schemaURL`` instinct (A13) — we keep the
#: version field, skip the URL.
TRACE_SCHEMA_VERSION = 1

#: The journal block class for a trace ingestion. Deliberately ABSENT from the
#: notebook-audit ``_BLOCK_ATTESTOR`` and receipt reductions (the relay-due
#: block-class precedent) — a trace ingestion is a journaled *sha of evidence*,
#: never a sign-off / attestation. Pinned by test.
DATA_TRACE_BLOCK = "data-trace"

#: The scope kinds a TRACE STORE key can carry. ``run``/``audit`` are the two
#: identity-bearing scopes; ``local`` is the ad-hoc key ``("local", cmd_sha12)``
#: for a run with neither run_id nor audit_id (A12 recorded-answer G-c). Distinct
#: from the DECISION-JOURNAL scope kinds — the journal has no ``audit``/``local``
#: kind, so :func:`_journal_scope` maps trace→journal at ingestion time.
TRACE_SCOPE_KINDS = frozenset({"run", "audit", "local"})


# --- the atom schema registry (ONE definition — A1 + A13) --------------------
#
# comparison-semantics vocabulary (design "The atom catalog"):
#   exact            — byte/value equality (row_count, order_integrity, digest)
#   set-delta        — added/dropped set difference (col_set)
#   tolerance        — numeric closeness within a declared band (value_sketch,
#                      duration_ms, peak_mb)
#   exact-per-key    — exact, keyed by column name (null_count)
#   equality-chain   — equal along the chain of stages (label_chain)
#   exact-endpoints  — exact on the first/last endpoints only (span)


@dataclass(frozen=True)
class AtomSchema:
    """One atom's core contract: value shape, comparison semantics, facet note.

    ``value_kind`` names the SHAPE :func:`validate_record` checks (core validates
    shapes, never touches frames). ``comparison`` is the diff-engine semantics
    (T6 dispatches on it). ``openlineage_facet`` is the A13 courtesy mapping —
    documentation for an export adapter / cross-tool reader, never a wire format
    (``None`` for the core-original atoms with no lineage-standard equivalent).
    """

    name: str
    value_kind: str
    comparison: str
    openlineage_facet: str | None


# value_kind constants (drive shape validation)
_KIND_ROW_COUNT = "row_count"  # {"rows": int>=0, "dropped": int>=0}
_KIND_NAME_SET = "name_set"  # {"columns": [str, ...]}
_KIND_PER_COLUMN_INT = "per_column_int"  # {col: int>=0} — OL columnMetrics layout
_KIND_PER_COLUMN_SKETCH = "per_column_sketch"  # {col: {min,max,mean,std,quantiles{q05,q50,q95}}}
_KIND_PER_COLUMN_ENDPOINTS = "per_column_endpoints"  # {col: {first, last}}
_KIND_PER_COLUMN_ORDER = "per_column_order"  # {col: {monotonic: bool, dups: int, gaps: int}}
_KIND_LABELS = "labels"  # {label_name: <opaque>} — the units ledger, generalized
_KIND_DIGEST = "digest"  # str (content sha, lowercase hex-ish opaque)
_KIND_SCALAR_COST = "scalar_cost"  # number >= 0

#: THE ATOM SCHEMA REGISTRY — the equality-pinned closed set. One definition:
#: render (T5), diff (T6), and the fingerprint interlock all consume it.
ATOM_REGISTRY: dict[str, AtomSchema] = {
    "row_count": AtomSchema(
        "row_count",
        _KIND_ROW_COUNT,
        "exact",
        "OutputStatisticsOutputDatasetFacet.rowCount",
    ),
    "col_set": AtomSchema(
        "col_set",
        _KIND_NAME_SET,
        "set-delta",
        "SchemaDatasetFacet.fields[].name",
    ),
    "null_count": AtomSchema(
        "null_count",
        _KIND_PER_COLUMN_INT,
        "exact-per-key",
        "DataQualityMetricsInputDatasetFacet.columnMetrics.<col>.nullCount",
    ),
    "value_sketch": AtomSchema(
        "value_sketch",
        _KIND_PER_COLUMN_SKETCH,
        "tolerance",
        "DataQualityMetricsInputDatasetFacet.columnMetrics.<col>.{min,max,sum,count,quantiles}",
    ),
    "span": AtomSchema(
        "span",
        _KIND_PER_COLUMN_ENDPOINTS,
        "exact-endpoints",
        None,
    ),
    "order_integrity": AtomSchema(
        "order_integrity",
        _KIND_PER_COLUMN_ORDER,
        "exact",
        None,
    ),
    "label_chain": AtomSchema(
        "label_chain",
        _KIND_LABELS,
        "equality-chain",
        None,
    ),
    "digest": AtomSchema(
        "digest",
        _KIND_DIGEST,
        "exact",
        None,
    ),
    "duration_ms": AtomSchema(
        "duration_ms",
        _KIND_SCALAR_COST,
        "tolerance",
        None,
    ),
    "peak_mb": AtomSchema(
        "peak_mb",
        _KIND_SCALAR_COST,
        "tolerance",
        None,
    ),
}

#: The closed set of atom names — the registry keys. Consumers pin equality
#: against this (the one-registry enforcement row).
ATOM_NAMES = frozenset(ATOM_REGISTRY)

#: The fixed quantile keys (A8/A13 recorded-answer: FIXED, not declared —
#: q05/q50/q95, mirroring OL's ``quantiles``-as-object-keyed-by-fraction).
QUANTILE_KEYS = ("q05", "q50", "q95")


def atom_schema(name: str) -> AtomSchema:
    """Return the :class:`AtomSchema` for *name*; raise on an unknown atom.

    The registry access API T2–T6 consume — one lookup so a typo or a
    pack-invented atom fails loudly instead of silently rendering/diffing.
    """
    try:
        return ATOM_REGISTRY[name]
    except KeyError:
        raise errors.SpecInvalid(
            f"unknown atom {name!r}; the closed set is {sorted(ATOM_NAMES)}"
        ) from None


def comparison_for(name: str) -> str:
    """Return the comparison-semantics token for atom *name* (T6's dispatch key)."""
    return atom_schema(name).comparison


# --- the flag shape (A12 G-c: the notebook-lint finding, reused system-wide) --


def make_flag(rule: str, detail: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build one flag in the canonical finding shape ``{rule, detail, evidence{}}``.

    One flag vocabulary system-wide (the notebook-lint finding shape, A12) — core
    renders flags but NEVER interprets them (pack/program invariants are opaque
    to core; the generic invariants below emit this same shape).
    """
    if not rule:
        raise errors.SpecInvalid("flag rule must be a non-empty string")
    return {"rule": rule, "detail": detail, "evidence": dict(evidence) if evidence else {}}


def _flag_errors(flag: Any, where: str) -> list[str]:
    """Shape-validate one flag; return a list of error strings (possibly empty)."""
    if not isinstance(flag, dict):
        return [f"{where}: flag must be an object"]
    errs: list[str] = []
    rule = flag.get("rule")
    if not isinstance(rule, str) or not rule:
        errs.append(f"{where}: flag.rule must be a non-empty string")
    if not isinstance(flag.get("detail"), str):
        errs.append(f"{where}: flag.detail must be a string")
    if not isinstance(flag.get("evidence"), dict):
        errs.append(f"{where}: flag.evidence must be an object")
    return errs


# --- per-atom shape validation (core validates shapes, never touches frames) --


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _atom_value_errors(name: str, value: Any) -> list[str]:  # noqa: C901 (flat dispatch)
    """Validate one atom VALUE against its registry shape; return error strings."""
    kind = atom_schema(name).value_kind
    w = f"atom {name!r}"
    if kind == _KIND_ROW_COUNT:
        if not isinstance(value, dict):
            return [f"{w}: value must be an object {{rows, dropped}}"]
        errs = []
        rows_v = value.get("rows")
        dropped_v = value.get("dropped")
        if not _is_int(rows_v) or rows_v < 0:
            errs.append(f"{w}: rows must be a non-negative int")
        if not _is_int(dropped_v) or dropped_v < 0:
            errs.append(f"{w}: dropped must be a non-negative int")
        return errs
    if kind == _KIND_NAME_SET:
        if not isinstance(value, dict) or not isinstance(value.get("columns"), list):
            return [f"{w}: value must be {{columns: [names]}}"]
        if not all(isinstance(c, str) for c in value["columns"]):
            return [f"{w}: columns must all be strings"]
        return []
    if kind == _KIND_PER_COLUMN_INT:
        if not isinstance(value, dict):
            return [f"{w}: value must be an object keyed by column"]
        return [
            f"{w}: column {c!r} count must be a non-negative int"
            for c, v in value.items()
            if not _is_int(v) or v < 0
        ]
    if kind == _KIND_PER_COLUMN_SKETCH:
        if not isinstance(value, dict):
            return [f"{w}: value must be an object keyed by column"]
        errs = []
        for col, sketch in value.items():
            errs.extend(_sketch_errors(f"{w}[{col!r}]", sketch))
        return errs
    if kind == _KIND_PER_COLUMN_ENDPOINTS:
        if not isinstance(value, dict):
            return [f"{w}: value must be an object keyed by column"]
        return [
            f"{w}: column {c!r} must be {{first, last}}"
            for c, v in value.items()
            if not isinstance(v, dict) or "first" not in v or "last" not in v
        ]
    if kind == _KIND_PER_COLUMN_ORDER:
        if not isinstance(value, dict):
            return [f"{w}: value must be an object keyed by column"]
        errs = []
        for col, v in value.items():
            if not isinstance(v, dict):
                errs.append(f"{w}: column {col!r} must be {{monotonic, dups, gaps}}")
                continue
            if not isinstance(v.get("monotonic"), bool):
                errs.append(f"{w}: column {col!r} monotonic must be a bool")
            if not _is_int(v.get("dups")) or not _is_int(v.get("gaps")):
                errs.append(f"{w}: column {col!r} dups/gaps must be ints")
        return errs
    if kind == _KIND_LABELS:
        if not isinstance(value, dict):
            return [f"{w}: value must be an object {{label_name: value}}"]
        if not all(isinstance(k, str) for k in value):
            return [f"{w}: label names must be strings"]
        return []
    if kind == _KIND_DIGEST:
        if not isinstance(value, str) or not value:
            return [f"{w}: digest must be a non-empty string"]
        return []
    if kind == _KIND_SCALAR_COST:
        if not _is_number(value) or value < 0:
            return [f"{w}: value must be a non-negative number"]
        return []
    return [f"{w}: no validator for value_kind {kind!r}"]  # pragma: no cover (closed set)


def _sketch_errors(where: str, sketch: Any) -> list[str]:
    """Validate a value_sketch bundle: min/max/mean/std numbers + fixed quantiles."""
    if not isinstance(sketch, dict):
        return [f"{where}: sketch must be an object"]
    errs = [
        f"{where}: {field} must be a number"
        for field in ("min", "max", "mean", "std")
        if not _is_number(sketch.get(field))
    ]
    quant = sketch.get("quantiles")
    if not isinstance(quant, dict):
        errs.append(f"{where}: quantiles must be an object keyed {QUANTILE_KEYS}")
    else:
        errs.extend(
            f"{where}: quantile {q!r} must be a number"
            for q in QUANTILE_KEYS
            if not _is_number(quant.get(q))
        )
        extra = set(quant) - set(QUANTILE_KEYS)
        if extra:
            errs.append(
                f"{where}: quantiles keys are FIXED to {QUANTILE_KEYS}; extra {sorted(extra)}"
            )
    return errs


# --- the record container ----------------------------------------------------


def make_record(
    stage: str,
    seq: int,
    atoms: dict[str, Any],
    *,
    section: str | None = None,
    flags: list[dict[str, Any]] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build one validated trace record dict.

    ``stage`` is the fine-grained emit; ``section`` optionally names the audit
    slug housing it (one section : many stages; both opaque to core). ``created_at``
    auto-stamps UTC ISO-8601 when omitted. Raises :class:`errors.SpecInvalid` on
    any shape violation (unknown atom, malformed value, bad flag).
    """
    record: dict[str, Any] = {
        "stage": stage,
        "seq": seq,
        "atoms": dict(atoms),
        "flags": [dict(f) for f in (flags or [])],
        "trace_schema_version": TRACE_SCHEMA_VERSION,
        "created_at": created_at or utcnow_iso(),
    }
    if section is not None:
        record["section"] = section
    errs = validate_record(record)
    if errs:
        raise errors.SpecInvalid("invalid trace record: " + "; ".join(errs))
    return record


def validate_record(record: Any) -> list[str]:
    """Return a list of shape errors for *record* (empty = valid). PURE.

    Validates the container shape, that every atom name is in the closed
    registry set, each atom value matches its registry shape, and each flag is
    the ``{rule, detail, evidence}`` finding shape.
    """
    if not isinstance(record, dict):
        return ["record must be an object"]
    errs: list[str] = []
    if not isinstance(record.get("stage"), str) or not record.get("stage"):
        errs.append("stage must be a non-empty string")
    if not _is_int(record.get("seq")) or record.get("seq", -1) < 0:
        errs.append("seq must be a non-negative int")
    section = record.get("section")
    if section is not None and not isinstance(section, str):
        errs.append("section must be a string when present")
    if record.get("trace_schema_version") != TRACE_SCHEMA_VERSION:
        errs.append(f"trace_schema_version must be {TRACE_SCHEMA_VERSION}")
    if not isinstance(record.get("created_at"), str) or not record.get("created_at"):
        errs.append("created_at must be a non-empty ISO-8601 string")
    atoms = record.get("atoms")
    if not isinstance(atoms, dict):
        errs.append("atoms must be an object")
    else:
        for name, value in atoms.items():
            if name not in ATOM_NAMES:
                errs.append(f"unknown atom {name!r}; closed set is {sorted(ATOM_NAMES)}")
                continue
            errs.extend(_atom_value_errors(name, value))
    flags = record.get("flags")
    if not isinstance(flags, list):
        errs.append("flags must be a list")
    else:
        for i, flag in enumerate(flags):
            errs.extend(_flag_errors(flag, f"flags[{i}]"))
    return errs


# --- canonical serialization (routes through the ONE canonical helper) --------


def record_sha(record: dict[str, Any]) -> str:
    """Canonical SHA-256 over one record (``state.determinism.canonical_sha``)."""
    return canonical_sha(record)


def records_sha(records: Sequence[dict[str, Any]]) -> str:
    """Canonical SHA-256 over an ordered list of records — the ``trace_sha``.

    The identity a trace joins the trust chain by (design §"Identity = journaled
    sha"): tamper or regeneration breaks this sha. Routes through the one
    canonicalization every content sha in the system uses.
    """
    return canonical_sha(list(records))


# --- the generic invariants (PURE arithmetic over atoms, zero meaning) --------


def check_row_conservation(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag any stage where ``rows_out != rows_in - dropped`` (design §invariants).

    ``rows_in`` = the predecessor stage's ``row_count.rows``; ``dropped`` /
    ``rows_out`` = this stage's ``row_count.{dropped, rows}``. Records are read
    in the given (emission) order; stages lacking a ``row_count`` atom are
    skipped for the pair they'd span (a partial trace is not a violation). Pure.
    """
    flags: list[dict[str, Any]] = []
    prev_rows: int | None = None
    prev_stage: str | None = None
    for rec in records:
        rc = _row_count(rec)
        if rc is None:
            prev_rows = None
            prev_stage = None
            continue
        rows, dropped = rc
        if prev_rows is not None:
            expected = prev_rows - dropped
            if rows != expected:
                flags.append(
                    make_flag(
                        "row_conservation",
                        (
                            f"rows_out={rows} != rows_in({prev_rows}) - dropped({dropped})"
                            f" = {expected}"
                        ),
                        {
                            "stage": rec.get("stage"),
                            "prev_stage": prev_stage,
                            "rows_in": prev_rows,
                            "dropped": dropped,
                            "rows_out": rows,
                            "expected": expected,
                        },
                    )
                )
        prev_rows = rows
        prev_stage = rec.get("stage")
    return flags


def check_label_chain_continuity(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag a tracked label that VANISHES before the trace ends (a broken chain).

    Core knows only "a label the caller tracks through stages and wants unbroken"
    (the units ledger, generalized) — never "units". Once a label name first
    appears at stage *s*, every later record must carry it; a later record that
    omits it is a break (the #1 historical mirage class reads as a broken link).
    Pure; emits the one finding shape.
    """
    flags: list[dict[str, Any]] = []
    ordered = list(records)
    first_seen: dict[str, int] = {}
    for idx, rec in enumerate(ordered):
        for label in _labels(rec):
            first_seen.setdefault(label, idx)
    for label, start in first_seen.items():
        for idx in range(start + 1, len(ordered)):
            rec = ordered[idx]
            if label not in _labels(rec):
                flags.append(
                    make_flag(
                        "label_chain_break",
                        f"tracked label {label!r} absent at stage {rec.get('stage')!r}",
                        {
                            "label": label,
                            "stage": rec.get("stage"),
                            "seq": rec.get("seq"),
                            "introduced_at_index": start,
                        },
                    )
                )
    return flags


def check_seq_monotonicity(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag any record whose ``seq`` does not strictly increase in emission order.

    A duplicate or out-of-order ``seq`` means two stage-exit records collide
    (ambiguity) — the emitter's sequencing broke. Pure.
    """
    flags: list[dict[str, Any]] = []
    prev: int | None = None
    for rec in records:
        seq = rec.get("seq")
        if not _is_int(seq):
            continue
        if prev is not None and seq <= prev:
            flags.append(
                make_flag(
                    "seq_monotonicity",
                    f"seq {seq} does not exceed the prior seq {prev}",
                    {"stage": rec.get("stage"), "seq": seq, "prev_seq": prev},
                )
            )
        prev = seq
    return flags


def run_invariants(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run all three generic invariants and return the concatenated flags."""
    return [
        *check_seq_monotonicity(records),
        *check_row_conservation(records),
        *check_label_chain_continuity(records),
    ]


def _row_count(record: dict[str, Any]) -> tuple[int, int] | None:
    """Extract ``(rows, dropped)`` from a record's row_count atom, or ``None``."""
    atoms = record.get("atoms")
    if not isinstance(atoms, dict):
        return None
    rc = atoms.get("row_count")
    if not isinstance(rc, dict):
        return None
    rows, dropped = rc.get("rows"), rc.get("dropped")
    if _is_int(rows) and _is_int(dropped):
        return rows, dropped
    return None


def _labels(record: dict[str, Any]) -> frozenset[str]:
    """The set of label names carried by a record's label_chain atom."""
    atoms = record.get("atoms")
    if not isinstance(atoms, dict):
        return frozenset()
    lc = atoms.get("label_chain")
    if not isinstance(lc, dict):
        return frozenset()
    return frozenset(k for k in lc if isinstance(k, str))


# --- the store (one canonical, local, append-only store) ---------------------


def _validate_store_scope(scope_kind: str, scope_id: str) -> None:
    if scope_kind not in TRACE_SCOPE_KINDS:
        raise errors.SpecInvalid(
            f"scope_kind must be one of {sorted(TRACE_SCOPE_KINDS)}; got {scope_kind!r}"
        )
    if not scope_id:
        raise errors.SpecInvalid("scope_id must be a non-empty string")
    if "/" in scope_id or "\\" in scope_id or scope_id in (".", ".."):
        raise errors.SpecInvalid(f"scope_id must be filesystem-safe; got {scope_id!r}")


def _validate_task(task: int) -> None:
    if not _is_int(task) or task < 0:
        raise errors.SpecInvalid(f"task must be a non-negative int; got {task!r}")


def trace_store_path(experiment_dir: Path, scope_kind: str, scope_id: str, task: int) -> Path:
    """Return ``.hpc/traces/<scope_kind>/<scope_id>/task-<n>.jsonl`` (one per task).

    Point-lookup layout because five of seven consumers are point lookups; LOCAL
    placement because every consumer runs locally against the experiment. Single-
    task local runs use ``task-0`` (A-recorded answer; no collision with the
    audit-prelude scope, which keys by ``audit`` scope_kind). Does not create the
    file; the parent dir is created by the append seam.
    """
    from hpc_agent._kernel.contract.layout import RepoLayout

    _validate_store_scope(scope_kind, scope_id)
    _validate_task(task)
    return RepoLayout(experiment_dir).hpc / "traces" / scope_kind / scope_id / f"task-{task}.jsonl"


def write_trace(
    experiment_dir: Path,
    scope_kind: str,
    scope_id: str,
    task: int,
    records: Sequence[dict[str, Any]],
) -> Path:
    """Validate then append every record to the task's store file. Returns the path.

    Raises :class:`errors.SpecInvalid` on the FIRST invalid record (the store is
    the trust chain — an invalid record never enters it). Append-only via the
    canonical JSONL seam (flock + fsync).
    """
    for i, rec in enumerate(records):
        errs = validate_record(rec)
        if errs:
            raise errors.SpecInvalid(f"record {i} invalid: " + "; ".join(errs))
    path = trace_store_path(experiment_dir, scope_kind, scope_id, task)
    for rec in records:
        append_jsonl_line(path, rec)
    return path


def read_trace(
    experiment_dir: Path, scope_kind: str, scope_id: str, task: int
) -> list[dict[str, Any]]:
    """Return the task's records in append order; tolerant read.

    Returns ``[]`` when the file does not exist. Blank and individually-corrupt
    lines are skipped rather than failing the whole read — one bad line must
    never strand the rest of a trace (the decision-journal read convention).
    """
    import json

    path = trace_store_path(experiment_dir, scope_kind, scope_id, task)
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return records
    except (OSError, UnicodeDecodeError):
        return records
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


# --- ingestion (transport → store, ONE journaled sha) ------------------------


def _journal_scope(scope_kind: str, scope_id: str) -> tuple[str, str]:
    """Map a TRACE scope to a DECISION-JOURNAL ``(scope_kind, scope_id)`` pair.

    The journal has no ``audit``/``local`` kind: an audit-context trace journals
    under ``notebook`` with ``scope_id = audit_id`` (the recorded answer — notebook
    scope id = audit_id); an ad-hoc ``local`` trace journals under ``scope`` with
    the ``cmd_sha12`` tag; a ``run`` trace journals under ``run``.
    """
    if scope_kind == "run":
        return "run", scope_id
    if scope_kind == "audit":
        return "notebook", scope_id
    return "scope", scope_id  # local


def ingest_trace(
    experiment_dir: Path,
    scope_kind: str,
    scope_id: str,
    task: int,
    transport_path: Path,
) -> dict[str, Any]:
    """Ingest a transport ``_trace.jsonl`` into the store; journal ONE sha record.

    Validates EVERY line of *transport_path* (raises on the first invalid one —
    an invalid trace never enters the trust chain), appends the records to the
    task's store file, deletes the disposable transport copy, computes
    ``trace_sha = canonical_sha(records)``, and journals ONE ``append_decision``
    record (``block="data-trace"``, ``resolved={scope, id, task, trace_sha,
    stage_count}``) on the mapped run/audit scope. Trace BULK never enters the
    journal — only this sha does. The block is ABSENT from ``_BLOCK_ATTESTOR`` /
    receipt reductions (pinned by test), so an ingestion can never enter a
    sign-off reduction. Returns a summary dict.

    Raises :class:`errors.SpecInvalid` on a bad scope/task or any invalid line,
    :class:`FileNotFoundError` if *transport_path* is absent.
    """
    import json

    _validate_store_scope(scope_kind, scope_id)
    _validate_task(task)

    text = transport_path.read_text(encoding="utf-8")
    records: list[dict[str, Any]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise errors.SpecInvalid(
                f"{transport_path}: line {lineno} is not valid JSON ({exc})"
            ) from exc
        errs = validate_record(obj)
        if errs:
            raise errors.SpecInvalid(f"{transport_path}: line {lineno} invalid: " + "; ".join(errs))
        records.append(obj)

    store_path = trace_store_path(experiment_dir, scope_kind, scope_id, task)
    for rec in records:
        append_jsonl_line(store_path, rec)

    # Transport copies are disposable after ingestion (design §"Emission =
    # transport"): the move completes by removing the in-flight packet.
    with contextlib.suppress(OSError):
        transport_path.unlink()

    trace_sha = records_sha(records)
    stage_count = len(records)

    journal_kind, journal_id = _journal_scope(scope_kind, scope_id)
    append_decision(
        experiment_dir,
        scope_kind=journal_kind,
        scope_id=journal_id,
        block=DATA_TRACE_BLOCK,
        response="ingested",
        resolved={
            "scope": scope_kind,
            "id": scope_id,
            "task": task,
            "trace_sha": trace_sha,
            "stage_count": stage_count,
        },
    )

    return {
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "task": task,
        "trace_sha": trace_sha,
        "stage_count": stage_count,
        "store_path": store_path,
        "journal_scope_kind": journal_kind,
        "journal_scope_id": journal_id,
    }
