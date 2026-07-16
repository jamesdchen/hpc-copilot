"""Cell-ownership map for a two-parent remainder migration [SPEC §3 Step B.4, §5, LIVE-2].

When ``migrate-remainder`` splits a run's task cells across two clusters — the
*done* cells stay under the source run, the *undone* cells move to a derived run
— the eventual multi-parent harvest (``ops/migrate/harvest.py``) must NOT
blind-pool the two mirrors. In the ``qdel`` race window the same logical cell can
briefly exist under *both* run_ids, and the value-keyed reducer
(``execution/mapreduce/reduce/metrics.py:134``, weighted-mean by ``n_samples``,
no task-id keying and no cardinality gate) would happily average the over-count,
double-summing ``n`` (``metrics.py:101-102``).

The fix is an **ownership map** computed **mechanically from the census** (no LLM,
the framework's founding numeric-loop constraint): every *undone* cell is owned by
the derived run, every *done* cell by the source run, and the union covers the
whole task range **exactly once**. The harvest selects, per cell, exactly the
owner's result dir before it reduces — so a race-duplicated cell resolves to its
owner and its ``n`` is counted once.

Persistence is a **migrate-scoped artifact** (``.hpc/migrate/<derived_run_id>/
ownership.json``), NOT a run-sidecar write: the state-writer seam is parked /
forbidden for this wave (SPEC §9). Folding the map into the sidecar is a follow-on
once the state-writer wave lands — disclosed in the derive result, never silent.

The stored cell key is the **source-global task id** (the stable cross-parent cell
identity); a companion ``derived_local_index`` maps each undone cell back to the
derived run's re-indexed ``0..len(undone)-1`` local task index, because the derived
run re-materializes its ``items`` from ``0`` and its result dirs render under its
own run_id at that local index. The harvest needs both to locate the owner's dir.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpc_agent import errors

# Bump when the on-disk ownership.json shape changes; the harvest reader pins it.
OWNERSHIP_SCHEMA_VERSION = 1

# The migrate-scoped artifact filename under .hpc/migrate/<derived_run_id>/.
OWNERSHIP_FILENAME = "ownership.json"


@dataclass(frozen=True)
class OwnershipMap:
    """The exactly-once cell → owning-run map for a two-parent harvest.

    ``owner`` is the SPEC's ``{cell_key → owning_run_id}`` contract, keyed by the
    source-global task id. ``derived_local_index`` re-indexes each undone cell to
    the derived run's local ``0..len(undone)-1`` task index (done cells keep their
    source-global index and are absent from this map — the source run owns them at
    the id the census already knows).
    """

    total: int
    source_run_id: str
    derived_run_id: str
    owner: dict[int, str]
    derived_local_index: dict[int, int]

    def owner_of(self, cell_id: int) -> str:
        """Return the run_id that owns *cell_id*.

        Raises :class:`errors.SpecInvalid` for a cell outside the covered range —
        the map is exactly-once by construction, so an unknown cell is a caller
        bug worth failing loud on rather than a silent miss (the harvest would
        otherwise drop a real cell and under-count ``total``).
        """
        try:
            return self.owner[cell_id]
        except KeyError:
            raise errors.SpecInvalid(
                f"ownership map has no owner for cell {cell_id!r} "
                f"(covered range is 0..{self.total - 1}); the map is exactly-once "
                "by construction — an unknown cell means the census and the map "
                "disagree, which must never be masked."
            ) from None

    def to_json_obj(self) -> dict[str, Any]:
        """Serializable form; JSON object keys are strings, so cell ids stringify."""
        return {
            "schema": OWNERSHIP_SCHEMA_VERSION,
            "total": self.total,
            "source_run_id": self.source_run_id,
            "derived_run_id": self.derived_run_id,
            # str keys: JSON has no int keys, and the harvest reader casts back.
            "owner": {str(cid): rid for cid, rid in sorted(self.owner.items())},
            "derived_local_index": {
                str(cid): idx for cid, idx in sorted(self.derived_local_index.items())
            },
            # Disclosed provenance: this is a migrate-scoped artifact, not a
            # sidecar write — the state-writer seam is parked this wave (SPEC §9).
            "folds_into_sidecar": (
                "migrate-scoped artifact for v1; folds into the run sidecar once "
                "the state-writer wave lands (SPEC §6 DECLINED, §9)"
            ),
        }

    def digest(self) -> dict[str, Any]:
        """A brief for the migration brief — counts, not the full per-cell map."""
        source_n = sum(1 for rid in self.owner.values() if rid == self.source_run_id)
        derived_n = sum(1 for rid in self.owner.values() if rid == self.derived_run_id)
        return {
            "total": self.total,
            "source_run_id": self.source_run_id,
            "source_cells": source_n,
            "derived_run_id": self.derived_run_id,
            "derived_cells": derived_n,
            "exactly_once": source_n + derived_n == self.total,
        }


def compute_ownership_map(
    *,
    total: int,
    undone_ids: list[int],
    done_ids: list[int],
    source_run_id: str,
    derived_run_id: str,
) -> OwnershipMap:
    """Build the exactly-once cell → owning-run map, MECHANICALLY [SPEC §3.B.4, LIVE-2].

    Every id in *undone_ids* is owned by *derived_run_id*, every id in *done_ids*
    by *source_run_id*. Guards that CAN fire (the engineering-principles rule):

    - *total* < 1, or any id outside ``range(total)`` → REFUSE (a census that
      names a cell the source never had is corrupt).
    - a cell in BOTH sets, or a cell in NEITHER → REFUSE: the union must partition
      ``range(total)`` exactly once, which is the whole point — the reducer has no
      cardinality gate of its own (SPEC §5), so a non-partition here would surface
      only as a silently double- or under-counted ``n`` downstream.

    The derived run re-indexes its undone cells from ``0`` in ascending
    source-global-id order (the same order ``derive`` materializes ``items``), so
    ``derived_local_index[source_id] = position-in-sorted-undone``.
    """
    if total < 1:
        raise errors.SpecInvalid(f"ownership map needs total >= 1, got {total}")

    undone_set = set(undone_ids)
    done_set = set(done_ids)

    if len(undone_set) != len(undone_ids):
        raise errors.SpecInvalid("undone_ids contains duplicates — census is not a set")
    if len(done_set) != len(done_ids):
        raise errors.SpecInvalid("done_ids contains duplicates — census is not a set")

    full = set(range(total))
    for cid in undone_set | done_set:
        if cid not in full:
            raise errors.SpecInvalid(
                f"cell id {cid!r} is outside the source task range 0..{total - 1}; "
                "the census names a cell the source never had — refusing to build "
                "an ownership map over a corrupt census."
            )

    both = undone_set & done_set
    if both:
        raise errors.SpecInvalid(
            f"cells {sorted(both)!r} are in BOTH the done and undone sets — the "
            "ownership partition must be disjoint (a cell has exactly one owner); "
            "the census disagrees with itself."
        )

    covered = undone_set | done_set
    missing = full - covered
    if missing:
        raise errors.SpecInvalid(
            f"cells {sorted(missing)!r} are in NEITHER the done nor undone set; the "
            f"union must cover the whole range 0..{total - 1} exactly once (SPEC §5) "
            "— refusing a census that leaves cells unaccounted for."
        )

    owner: dict[int, str] = {}
    for cid in done_set:
        owner[cid] = source_run_id
    for cid in undone_set:
        owner[cid] = derived_run_id

    derived_local_index = {cid: j for j, cid in enumerate(sorted(undone_set))}

    return OwnershipMap(
        total=total,
        source_run_id=source_run_id,
        derived_run_id=derived_run_id,
        owner=owner,
        derived_local_index=derived_local_index,
    )


def ownership_artifact_path(experiment_dir: Path, derived_run_id: str) -> Path:
    """``.hpc/migrate/<derived_run_id>/ownership.json`` — the migrate-scoped path.

    Does NOT create the file; mirrors the ``RepoLayout`` file-path convention
    (methods returning a *file* path don't create it).
    """
    return Path(experiment_dir).resolve() / ".hpc" / "migrate" / derived_run_id / OWNERSHIP_FILENAME


def persist_ownership_map(experiment_dir: Path, ownership: OwnershipMap) -> Path:
    """Write the ownership map to its migrate-scoped artifact path; return it.

    NOT a sidecar write — the state-writer seam is parked/forbidden this wave
    (SPEC §9). The parent directory is created idempotently.
    """
    path = ownership_artifact_path(experiment_dir, ownership.derived_run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(ownership.to_json_obj(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_ownership_map(experiment_dir: Path, derived_run_id: str) -> OwnershipMap:
    """Read back a persisted ownership map (the harvest's data-contract entry point).

    Raises :class:`FileNotFoundError` when the artifact is absent — the harvest
    must never treat a missing ownership map as "no overlap to resolve".
    """
    path = ownership_artifact_path(experiment_dir, derived_run_id)
    if not path.is_file():
        raise FileNotFoundError(f"ownership artifact not found: {path}")
    obj = json.loads(path.read_text(encoding="utf-8"))
    schema = obj.get("schema")
    if schema != OWNERSHIP_SCHEMA_VERSION:
        raise errors.SpecInvalid(
            f"ownership artifact {path} has schema {schema!r}, expected "
            f"{OWNERSHIP_SCHEMA_VERSION}; refusing to read a shape this reader "
            "does not understand."
        )
    return OwnershipMap(
        total=int(obj["total"]),
        source_run_id=str(obj["source_run_id"]),
        derived_run_id=str(obj["derived_run_id"]),
        owner={int(cid): str(rid) for cid, rid in obj["owner"].items()},
        derived_local_index={int(cid): int(idx) for cid, idx in obj["derived_local_index"].items()},
    )
