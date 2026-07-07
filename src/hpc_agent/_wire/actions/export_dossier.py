"""Pydantic models for the ``export-dossier`` action.

Wire surface over the dossier bundler — the verb that gathers a run's
persisted state into a single portable archive. The bundle is assembled by
walking the run's **source stores** (the journal, the sidecar, the scope
ledgers, the harvested results — whatever concrete on-disk store the ops
layer enumerates) and copying each store's entries verbatim.

Boundary posture (see ``docs/internals/engineering-principles.md``): an
entry in the bundle is typed by the SOURCE STORE it came from, NEVER by what
it means. The framework knows "this line is a decision-journal record" or
"this file is a run sidecar"; it never knows — and these models never
encode — that a record is a "greenlight", a "holdout result", or any other
caller-owned semantics. The closed set of source-store names lives in the
ops module that does the bundling, not on the wire; that is why ``manifest``
is carried as a bare ``dict`` here rather than a typed model — pinning the
store-name vocabulary in the wire schema would leak an ops-layer contract
into the boundary and freeze it against the schema.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class ExportDossierSpec(BaseModel):
    """Inputs to ``export-dossier``.

    ``run_id`` names the run whose stores are bundled (``RunIdStrict`` — the
    same filesystem-safe slug every run_id field carries). ``include_lineage``
    widens the bundle from the single run to its whole supersession chain (the
    run plus every run it superseded, back to the lineage root); the resolved
    set and its order are reported in :class:`ExportDossierResult.run_ids`.
    """

    model_config = ConfigDict(extra="forbid", title="export-dossier input spec")

    run_id: RunIdStrict
    # Where to write the archive. ``None`` lets the verb derive a conventional
    # path under the experiment's ``.hpc/`` tree — a derived default, not an
    # agent-authored one.
    output_path: str | None = Field(
        default=None,
        description=(
            "Destination path for the bundle archive. Omit to let the verb "
            "derive a conventional path; the resolved location is echoed back "
            "as archive_path."
        ),
    )
    include_lineage: bool = Field(
        default=False,
        description=(
            "When true, bundle the run's whole supersession lineage (the run "
            "plus every run it superseded, to the lineage root) instead of the "
            "single run. The bundled set and its lineage order are reported in "
            "run_ids."
        ),
    )


class ExportDossierResult(BaseModel):
    """The assembled dossier bundle's manifest and integrity fingerprint.

    Every field describes the bundle by PROVENANCE (which stores, how many
    entries, what identities), never by the meaning of any entry — an entry is
    typed by its source store, not by a caller-owned role.
    """

    model_config = ConfigDict(extra="forbid", title="export-dossier output data")

    # Resolved path the archive was written to (the derived default or the
    # caller's output_path, whichever applied).
    archive_path: str
    # The run ids bundled, in lineage order (newest→root) when include_lineage
    # is set; a single-element list otherwise. Same order as the lineage chain.
    run_ids: list[str] = Field(
        description=(
            "The run ids bundled, in lineage order (newest first, root last) "
            "when include_lineage is set; otherwise the single requested run."
        ),
    )
    # SHA-256 over the archive bytes — the integrity fingerprint a consumer
    # re-checks after transport.
    bundle_sha256: str
    # Total number of entries copied into the bundle across every source store.
    entry_count: int = Field(ge=0)
    # Stores or entries the bundler expected but did not find (a run in the
    # lineage with no journal record, an absent sidecar, …). Each item is a
    # free-shape record naming the missing store and the run it belonged to;
    # gaps are REPORTED, never silently dropped, but they are not fatal.
    gaps: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Expected-but-absent stores/entries, each a record naming the "
            "missing source store and its run. Reported, not fatal — a bundle "
            "with gaps is still written."
        ),
    )
    # The bundle manifest: a mapping the ops layer fills, keyed by SOURCE-STORE
    # name (the closed set of store names is owned by the ops module, not the
    # wire). Kept a bare dict so the store-name vocabulary stays out of the
    # boundary schema — entries are typed by store, never by meaning.
    manifest: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The bundle manifest keyed by source-store name (vocabulary owned "
            "by the ops bundler, not the wire). Describes what each store "
            "contributed by provenance, never by an entry's meaning."
        ),
    )
