"""Select an SGE parallel environment for a multi-rank (MPI) job (#293).

``inspect-cluster`` enumerates a cluster's ``parallel_environments`` (PR1) —
SGE PEs (``source="pe"``), SLURM partitions, PBS queues — each tagged
``kind: mpi|smp|other``. An SGE MPI submit must name a PE for the
``qsub -pe <pe_name> <ranks>`` flag; this picks that name from the cluster's
own enumeration so the caller doesn't have to hard-code it.

Pure + deterministic: it only reads the ``parallel_environments`` list and the
rank count, so it is trivially testable and reused by ``resolve-resources``.
SLURM/PBS need no ``-pe`` name (they size from ``--ntasks`` / ``select=``), so
their ``source != "pe"`` entries are ignored — for those families the function
correctly returns ``None`` (no PE to pick).
"""

from __future__ import annotations

from typing import Any

__all__ = ["recommend_pe"]


def _pe_slots(pe: dict[str, Any]) -> int | None:
    """The PE's total slot capacity from ``raw.slots``, or ``None`` if unknown."""
    raw = pe.get("raw") or {}
    slots = raw.get("slots")
    if slots is None:
        return None
    try:
        return int(slots)
    except (TypeError, ValueError):
        return None


def recommend_pe(parallel_environments: list[dict[str, Any]], ranks: int) -> tuple[str | None, str]:
    """Pick an SGE ``kind="mpi"`` parallel environment that can hold *ranks*.

    Returns ``(pe_name, rationale)``. ``pe_name`` is ``None`` when no MPI PE is
    available (or none has the slot capacity for *ranks*); the rationale string
    records why so the caller can surface an actionable message.

    Selection rule — among the MPI PEs whose slot capacity is sufficient
    (``raw.slots >= ranks``, or unknown capacity which is assumed usable), pick
    the **tightest fit** (smallest sufficient ``slots``). That keeps a small job
    off the cluster's largest PE, and is deterministic (slots then name as
    tiebreaks) so the same cluster + rank count always resolves the same PE.
    """
    mpi_pes = [
        pe for pe in parallel_environments if pe.get("source") == "pe" and pe.get("kind") == "mpi"
    ]
    if not mpi_pes:
        return None, "no_mpi_pe"

    fitting = []
    for pe in mpi_pes:
        slots = _pe_slots(pe)
        if slots is None or slots >= ranks:
            fitting.append(pe)
    if not fitting:
        known = [s for s in (_pe_slots(pe) for pe in mpi_pes) if s is not None]
        largest = max(known) if known else None
        return None, f"no_pe_fits_ranks:ranks={ranks},largest_slots={largest}"

    def _sort_key(pe: dict[str, Any]) -> tuple[int, int, str]:
        slots = _pe_slots(pe)
        name = str(pe.get("name", ""))
        # Known-capacity PEs first, smallest sufficient slots, then name.
        if slots is None:
            return (1, 0, name)
        return (0, slots, name)

    chosen = min(fitting, key=_sort_key)
    return str(chosen["name"]), (
        f"tightest_fit:pe={chosen['name']},slots={_pe_slots(chosen)},"
        f"ranks={ranks},mpi_candidates={len(mpi_pes)}"
    )
