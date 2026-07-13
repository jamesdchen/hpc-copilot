"""Pydantic models for the ``export-attestations`` action.

Wire surface over the in-toto/DSSE PORTABILITY layer that sits atop the
``export-dossier`` sealing layer. Where ``export-dossier`` copies a run's
concrete on-disk **source stores** into one integrity-sealed ``.zip``,
``export-attestations`` projects that SAME sealed evidence — via the one
gather ``export_dossier`` already defines — into a stream of in-toto
Statements wrapped in (unsigned, v1) DSSE envelopes, so ecosystem tooling can
verify the bundle WITHOUT hpc-agent.

Boundary posture (see ``docs/internals/engineering-principles.md``, Q1
"substrate, not semantics"): an exported Statement is typed by the SOURCE
STORE its bytes came from (``predicateType`` per :data:`DOSSIER_SOURCES`
noun) and by NOTHING else. The subject digest is copied VERBATIM from the
dossier manifest; the predicate embeds the store's RAW BYTES. Core never
parses the content it attests — the moment it reads a field out of a record
it is interpreting the trail it seals. That is why these models carry no
store-name vocabulary and no domain-semantics field: the closed store-noun
set (and its predicateType map) live in the ops module, not on the wire.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class ExportAttestationsSpec(BaseModel):
    """Inputs to ``export-attestations``.

    ``run_id`` names the run whose sealed dossier evidence is projected into
    in-toto Statements (``RunIdStrict`` — the same filesystem-safe slug every
    run_id field carries). ``include_lineage`` widens the projection from the
    single run to its whole supersession chain, exactly as ``export-dossier``
    does (the resolved set and its order are reported in
    :class:`ExportAttestationsResult.run_ids`). ``output_path`` overrides the
    derived ``<experiment>/_dossier/<run_id>.attestations.jsonl`` landing path.
    """

    model_config = ConfigDict(extra="forbid", title="export-attestations input spec")

    run_id: RunIdStrict
    # Where to write the DSSE-envelope JSONL. ``None`` lets the verb derive a
    # conventional path under the experiment's ``_dossier/`` tree — a derived
    # default, not an agent-authored one.
    output_path: str | None = Field(
        default=None,
        description=(
            "Destination path for the attestations bundle (one DSSE envelope "
            "per line, JSONL). Omit to let the verb derive a conventional path; "
            "the resolved location is echoed back as output_path."
        ),
    )
    include_lineage: bool = Field(
        default=False,
        description=(
            "When true, project the run's whole supersession lineage (the run "
            "plus every run it superseded, to the lineage root) instead of the "
            "single run. The projected set and its lineage order are reported "
            "in run_ids."
        ),
    )


class ExportAttestationsResult(BaseModel):
    """The emitted attestations bundle's provenance and integrity fingerprint.

    Every field describes the bundle by PROVENANCE (where it landed, which
    runs it covers, how many Statements, the dossier signature it ties back
    to, which stores were absent), never by the meaning of any Statement — a
    Statement is typed by its source store's predicateType, not by a
    caller-owned role.
    """

    model_config = ConfigDict(extra="forbid", title="export-attestations output data")

    # Resolved path the DSSE-envelope JSONL was written to (the derived default
    # or the caller's output_path, whichever applied).
    output_path: str
    # The run ids projected, in lineage order (newest→root) when
    # include_lineage is set; a single-element list otherwise.
    run_ids: list[str] = Field(
        description=(
            "The run ids projected, in lineage order (newest first, root last) "
            "when include_lineage is set; otherwise the single requested run."
        ),
    )
    # One Statement (one DSSE envelope line) per sealed store entry — the count
    # equals the dossier's entry_count for the same run set.
    statement_count: int = Field(ge=0)
    # The dossier signature the Statements were projected from — identical to
    # export-dossier's bundle_sha256 for the same run set and on-disk state, so
    # a consumer can tie the attestations back to the exact sealed bundle.
    bundle_sha256: str
    # Stores the dossier gather expected but did not find (carried through from
    # the delegated ``export_dossier`` signature). Reported, never fatal — a
    # bundle with gaps is still written; those stores simply produce no
    # Statement.
    gaps: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Expected-but-absent stores (carried through from the dossier "
            "gather), each a record naming the missing source store and its "
            "run. Reported, not fatal — an absent store yields no Statement."
        ),
    )
