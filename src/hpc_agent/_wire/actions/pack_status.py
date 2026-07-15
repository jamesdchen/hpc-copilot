"""Pydantic models for the ``pack-status`` query verb (domain-packs T3/T6).

Wire surface over :mod:`hpc_agent.ops.pack.status_op` — a READ-ONLY digest of
pack state (``docs/design/domain-packs.md``, T6): the current bind, per-slot
receipt currency, an advisory unfillable-requirement report, and dangling-
reference findings. Core reports identity + counts only; it never interprets a
pack value.

**Keyed by pack (the scope-status precedent).** ``pack-status`` reports one
named pack or, when the name is omitted, every opted-in pack — so the result is
a ``{pack -> entry}`` map, exactly as ``scope-status`` returns ``{scope ->
entry}``. Each entry carries the four things T6 names: the current bind (or
null), the per-slot receipt statuses, the unfillable-requirement report, and
the dangling-reference findings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class PackStatusSpec(BaseModel):
    """Which pack(s) to report — one named pack, or every opted-in pack."""

    model_config = ConfigDict(extra="forbid", title="pack-status input spec")

    pack: RunIdStrict | None = Field(
        default=None,
        description="Pack slug to report; omit to report every opted-in pack.",
    )


class PackBind(BaseModel):
    """The current-bind projection for a pack (newest valid ``pack-bind``)."""

    model_config = ConfigDict(extra="forbid", title="pack current-bind")

    pack: str
    version: str
    manifest_sha: str
    bound_at: str = Field(description="Timestamp of the current bind record.")


class PackSlotStatus(BaseModel):
    """One caller-authored slot's receipt currency — mechanical, never interpreted.

    ``status`` is the reduction outcome: ``current`` (a fresh, passed receipt),
    ``failed`` (fresh receipt but ``passed=false``), ``stale`` (a receipt exists
    but content it covered drifted — stale = missing by construction), or
    ``missing`` (no receipt for the slot). ``passed`` is the recorded boolean
    when a receipt exists; ``reason`` is an optional honest note.
    """

    model_config = ConfigDict(extra="forbid", title="pack slot status")

    slot: str
    status: Literal["current", "stale", "missing", "failed"]
    passed: bool | None = None
    reason: str | None = None


class PackUnfillableRequirement(BaseModel):
    """A slot the caller bound to a pack whose manifest ``fills_slots`` omits it.

    ADVISORY only (``fills_slots`` never becomes load-bearing — DP4): a
    requirement always originates with the caller, so this is an early warning
    that the pack does not claim it can fill the slot, never a gate.
    """

    model_config = ConfigDict(extra="forbid", title="pack unfillable requirement")

    slot: str
    pack: str
    reason: str


class PackDanglingReference(BaseModel):
    """One dangling-reference finding for an opted-in pack (the LOUD path).

    An opted-in repo whose manifest is missing/sha-drifted, whose bind names a
    file that no longer resolves, or whose slot binding names a pack with no
    current bind. ``reason`` is the honest mechanical note; ``path`` / ``slot``
    name the offending reference when applicable.
    """

    model_config = ConfigDict(extra="forbid", title="pack dangling reference")

    reason: str
    path: str | None = None
    slot: str | None = None


class PackLineage(BaseModel):
    """The lineage stamp of a PROGRAM pack + its freshness against the bound source.

    Additive/optional (P3 observability, DC10): present only when the pack's
    manifest carries a ``derived_from`` stamp. ``pack``/``seam``/``version``/``sha``
    echo the stamp; ``freshness`` compares the recorded ``sha`` against the
    currently-bound source pack's seam-file sha:

    * ``current`` — the source pack is bound and its seam sha matches the stamp.
    * ``behind`` — the source pack is bound but its seam sha differs (a re-init from
      upstream or a skeleton upgrade moved it; the edge is NOT severed — freshness
      evidence only, DC2).
    * ``source-not-bound`` — the named source pack is not an opted-in current bind,
      so freshness cannot be established.
    """

    model_config = ConfigDict(extra="forbid", title="pack lineage")

    pack: str
    seam: str
    version: str
    sha: str
    freshness: Literal["current", "behind", "source-not-bound"]


class PackStatusEntry(BaseModel):
    """The full status digest for one pack.

    ``bind`` is the current bind or null; ``slots`` the per-slot receipt
    statuses; ``unfillable`` the advisory report; ``dangling`` the loud
    dangling-reference findings.
    """

    model_config = ConfigDict(extra="forbid", title="pack status entry")

    bind: PackBind | None = None
    slots: list[PackSlotStatus] = Field(default_factory=list)
    unfillable: list[PackUnfillableRequirement] = Field(default_factory=list)
    dangling: list[PackDanglingReference] = Field(default_factory=list)
    derived_from: PackLineage | None = Field(
        default=None,
        description=(
            "The PROGRAM-pack lineage stamp + freshness, when the manifest carries a "
            "``derived_from`` (P3 observability). ``None`` for a lineage-root (domain) "
            "pack or a manifest with no stamp — serialized as ``null`` for legacy "
            "packs (an additive optional field; the dispatch serializer does not "
            "exclude_none)."
        ),
    )
    audit_template: str | None = Field(
        default=None,
        description=(
            "Experiment-dir-relative path to this current-bound pack's "
            "``audit_template`` seam file, when it declares one — the AUDIT-FACING "
            "template prepared for building an experiment off the pack (run-#12 "
            "finding 1). Identity/pointer only: the on-ramp COMPOSES the default "
            "template path from this seam and presents a confirm-default rather "
            "than an open path question. ``None`` when the pack declares no "
            "``audit_template`` seam or is not current-bound."
        ),
    )


class PackStatusResult(BaseModel):
    """Pack state keyed by pack slug — one entry per reported pack."""

    model_config = ConfigDict(extra="forbid", title="pack-status output data")

    packs: dict[str, PackStatusEntry] = Field(default_factory=dict)
