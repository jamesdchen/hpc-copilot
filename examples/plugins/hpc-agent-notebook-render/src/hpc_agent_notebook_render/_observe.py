"""T-R — the runner's BETWEEN-CELL observation loop (A10 / A12 / A14).

THE OBSERVER IS THE RUNNER. The sanctioned execution lane (this plugin) executes
the audited source CELL BY CELL and, between cells, looks up the DECLARED
OBSERVABLES — the A14 observation plan, read from the SIGNED audit configuration —
in the exec namespace, and MEASURES each present one. Measurement is the pack's
job (its frame-aware impls are injected as a ``Measurer`` callable); the fallback
is core's stdlib measurer. Each measurement becomes a runner-tier T1 record
(``source="runner"``), tagged with the current ``# hpc-audit-section:`` slug (the
A9 Q3 mapping), appended to the T2 transport file, and — at execution end —
ingested into the audit scope (``traces/audit/<audit_id>/``).

Ungameable by construction: the observer is the PROCESS, not the code. A draft
cannot skip a cell boundary, and hiding data in an UNDECLARED name yields a
visibly-absent observable (a disclosure, never a silence). No frame knowledge
lives here — core stays frame-blind (DP2/DP3); the pack injects the measurer.

The observation lane is a LIGHT in-process exec runner (no jupyter kernel — the
only way to reach a namespace as a Python dict). It runs ONLY when the audit
config declares ``observables``; absent, the loop never runs and execution is
byte-identical (D7). ``observe_cell`` is the pure between-cell seam every test
drives with a stub namespace dict.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jupytext

from hpc_agent.execution.mapreduce.data_trace_contract import (
    TRACE_SOURCE_FIELD,
    TRACE_SOURCE_RUNNER,
    TRACE_TRANSPORT_FILENAME,
)
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.state.data_trace import ingest_trace, make_record, stdlib_measure

from . import _annotate

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "Measurer",
    "measure_observable",
    "observe_cell",
    "run_observation",
    "observe_source",
]

#: A measurement implementation the runner INVOKES on a declared observable:
#: ``measure(obj) -> {atom_name: value} | None``. Satisfied by core's
#: :func:`hpc_agent.state.data_trace.stdlib_measure` and by the pack's
#: frame-aware impls (injected caller-side — core never imports them).
Measurer = Callable[[Any], "dict[str, Any] | None"]


def measure_observable(obj: Any, *, measurer: Measurer | None = None) -> dict[str, Any] | None:
    """Measure one observed object to atoms — injected *measurer* wins, else stdlib.

    The pack's injected *measurer* is tried first; a non-``None`` return wins (a
    pack impl measures a real frame into ``col_set`` / ``null_count`` / ... that
    the stdlib fallback cannot). ``None`` from the pack impl (or no impl) falls
    through to core's frame-blind :func:`stdlib_measure`. ``None`` overall means
    "nothing measurable here" — the caller skips this observable silently.
    """
    if measurer is not None:
        atoms = measurer(obj)
        if atoms is not None:
            return atoms
    return stdlib_measure(obj)


def observe_cell(
    namespace: dict[str, Any],
    observables: Sequence[str],
    *,
    section: str | None,
    seq: int,
    measurer: Measurer | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """The pure between-cell seam: measure every DECLARED-AND-PRESENT observable.

    For each name in *observables* PRESENT in *namespace*, measure it (via the
    injected *measurer*, else the stdlib fallback) and build ONE runner-tier T1
    record (``source="runner"``) tagged with the current audit *section*. A
    declared-but-ABSENT name is skipped silently (the disclosure is its absence
    downstream); an object nothing can measure is skipped too. ``seq`` advances by
    one per emitted record (monotone across cells). Returns ``(records, next_seq)``.
    """
    records: list[dict[str, Any]] = []
    for name in observables:
        if name not in namespace:
            continue
        atoms = measure_observable(namespace[name], measurer=measurer)
        if atoms is None:
            continue
        record = make_record(stage=name, seq=seq, atoms=atoms, section=section)
        # The runner stamps its OWN trust tier (A10): it is the trust-bearing
        # observer, so the record is receipt-grade ``runner`` by construction.
        record[TRACE_SOURCE_FIELD] = TRACE_SOURCE_RUNNER
        records.append(record)
        seq += 1
    return records, seq


def run_observation(
    cells: Sequence[tuple[str | None, str]],
    observables: Sequence[str],
    *,
    measurer: Measurer | None = None,
    namespace: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute *cells* in-process cell-by-cell, observing between each.

    *cells* is an ordered sequence of ``(section_slug, code_source)`` (markdown /
    preamble cells carry ``section=None``). Each cell's source is ``exec``'d into a
    single shared *namespace*; after each, :func:`observe_cell` measures the
    declared observables now present. A cell that RAISES stops the run (later
    cells depend on it; their observables read as absent — a disclosure), but the
    records collected up to the failure are returned. Pass *namespace* to seed /
    inspect the exec dict (tests drive a stub namespace this way).
    """
    ns = namespace if namespace is not None else {}
    records: list[dict[str, Any]] = []
    seq = 0
    for section, code in cells:
        try:
            exec(compile(code, "<audit-cell>", "exec"), ns)  # noqa: S102 — the sanctioned lane
        except Exception:  # noqa: BLE001 — a raising cell ends observation, never the render
            break
        cell_records, seq = observe_cell(
            ns, observables, section=section, seq=seq, measurer=measurer
        )
        records.extend(cell_records)
    return records


def _percent_code_cells(source_text: str) -> list[tuple[str | None, str]]:
    """Parse percent *source_text* into ``(section_slug, code_source)`` code cells.

    Reuses the render annotator's section segmentation (the ``# hpc-audit-section:``
    marker → the owning slug) so the observation records' ``section`` agrees with
    every other view by construction. Only CODE cells carry executable source.
    """
    nb = jupytext.reads(source_text, fmt="py:percent")
    slugs = _annotate.assign_cell_sections(nb.cells)
    out: list[tuple[str | None, str]] = []
    for cell, slug in zip(nb.cells, slugs, strict=True):
        if cell.get("cell_type") == "code":
            out.append((slug, cell.get("source", "")))
    return out


def observe_source(
    experiment_dir: Path,
    *,
    audit_id: str,
    source_text: str,
    observables: Sequence[str],
    measurer: Measurer | None = None,
) -> dict[str, Any] | None:
    """The full T-R pass: run observation, emit to transport, ingest to audit scope.

    Parses *source_text*'s percent cells, runs the between-cell observation loop,
    appends the runner-tier records to a T2 transport file, then ingests them into
    the audit scope (``traces/audit/<audit_id>/task-0.jsonl``, journaled under the
    notebook scope — T1's audit→notebook mapping). Returns the ingest summary, or
    ``None`` when nothing was observed (no observables, or none present/measurable)
    so no empty trace is journaled.
    """
    if not observables:
        return None
    records = run_observation(_percent_code_cells(source_text), observables, measurer=measurer)
    if not records:
        return None

    transport_dir = experiment_dir / ".hpc" / "traces" / "_transport" / audit_id
    transport_dir.mkdir(parents=True, exist_ok=True)
    transport_path = transport_dir / TRACE_TRANSPORT_FILENAME
    for record in records:
        append_jsonl_line(transport_path, record)

    return ingest_trace(experiment_dir, "audit", audit_id, 0, transport_path)
