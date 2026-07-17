"""Pydantic models for the ``export-bundle`` action — the publication bundle.

Wire surface over the PUBLICATION-time composition layer that sits atop the
``export-dossier`` sealing layer (``docs/design/publication-bundle.md``). Where
``export-dossier`` copies a run's concrete on-disk **source stores** into one
integrity-sealed ``.zip``, ``export-bundle`` COMPOSES that same sealed evidence
— via the one gather ``export_dossier`` already defines — and adds the signed
provenance manifest, a cite-check audit of the manuscript, and a top-level
``VERIFY`` manifest classifying each reproducibility link, all under one seal.
It is a SIBLING of ``export-dossier`` (the ``export-attestations`` precedent),
never an extension: the dossier's run-scoped contract is untouched.

Boundary posture (see ``docs/internals/engineering-principles.md`` Q1,
"substrate, not semantics"): the bundle describes itself by PROVENANCE +
COUNTING + a per-link MECHANICAL/DISCLOSED/ABSENT classification over opaque +
framework-derived records — never by what any metric MEANS. That is why these
models carry no store-name / member-name vocabulary and no domain-semantics
field: the closed BUNDLE-MEMBER vocabulary lives in the ops module, not on the
wire (the ``export-dossier`` posture, where ``DOSSIER_SOURCES`` stays an
ops-layer contract and the ``verify_manifest`` rides as a bare ``dict``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExportBundleSpec(BaseModel):
    """Inputs to ``export-bundle`` — one seed, an optional manuscript.

    The **seed** is exactly one of ``run_id`` / ``campaign_id`` /
    ``aggregate_path`` (the ``extract-recipe`` / ``cite-check`` seed contract,
    reused verbatim — validated in the op, not by the schema, so the exactly-one
    rule reports one clear error). The **manuscript** is optionally one of
    ``manuscript_text`` / ``manuscript_path`` (the ``cite-check`` input
    contract): ABSENT is legal (disclose-not-gate) — the bundle still seals the
    dossier evidence + signed manifest and records a ``cite-check-skipped``
    disclosure.
    """

    model_config = ConfigDict(extra="forbid", title="export-bundle input spec")

    run_id: str | None = Field(
        default=None,
        description=(
            "Seed the bundle from this run's sealed evidence + table. Excludes the other two seeds."
        ),
    )
    campaign_id: str | None = Field(
        default=None,
        description="Seed the bundle from this campaign. Excludes the other two seeds.",
    )
    aggregate_path: str | None = Field(
        default=None,
        description=(
            "Seed the bundle from a sealed reduced-metrics artifact. A pack *.csv "
            "is an OPAQUE citation (never parsed). Excludes the other two seeds."
        ),
    )
    manuscript_text: str | None = Field(
        default=None,
        description=(
            "The manuscript verbatim whose numeric claims are audited against the "
            "sealed table. Optional; excludes manuscript_path."
        ),
    )
    manuscript_path: str | None = Field(
        default=None,
        description=(
            "Path to a .tex / .md / .txt manuscript, read tolerantly. Optional; "
            "excludes manuscript_text."
        ),
    )
    include_lineage: bool = Field(
        default=False,
        description=(
            "When true, widen the dossier gather to the primary run's whole "
            "supersession lineage (the run plus every run it superseded, to the "
            "lineage root). The resolved set is reported in run_ids."
        ),
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Destination path for the bundle .zip. Omit to let the verb derive a "
            "conventional path (<experiment>/_dossier/<seed>.bundle.zip); the "
            "resolved location is echoed back as bundle_path."
        ),
    )


class ExportBundleResult(BaseModel):
    """The assembled publication bundle's provenance + honest verify verdict.

    Every field describes the bundle by PROVENANCE (where it landed, which seed
    + runs it covers, how many members, the seal) or by the CODE-emitted honest
    verdict + disclosure ledger — never by the meaning of any sealed member. The
    ``verify_manifest`` rides as a bare ``dict`` so the closed BUNDLE-MEMBER
    vocabulary and the per-link classification stay an ops-layer contract, out of
    the boundary schema (the ``export-dossier`` ``manifest`` posture).
    """

    model_config = ConfigDict(extra="forbid", title="export-bundle output data")

    # Resolved path the .zip was written to (the derived default or the caller's
    # output_path, whichever applied).
    bundle_path: str
    seed_kind: Literal["run", "campaign", "aggregate"] = Field(
        description="Which seed reference the bundle was composed from.",
    )
    seed_ref: str = Field(description="The seed's identity / path verbatim.")
    # The run whose stores seeded the dossier gather (the run itself for a run
    # seed; the head contributing run for a campaign / aggregate seed).
    primary_run_id: str | None = Field(
        default=None,
        description="The run whose sealed stores seeded the dossier gather.",
    )
    run_ids: list[str] = Field(
        default_factory=list,
        description=(
            "The run ids whose dossier stores were sealed, in lineage order "
            "(newest first) when include_lineage is set; otherwise the primary run."
        ),
    )
    # The ONE top-level seal over the path-sorted member entries — reused from
    # the dossier's signable digest (manifest_signature).
    bundle_sha256: str
    # Total number of sealed members in the bundle (every dossier store entry +
    # the added members).
    member_count: int = Field(ge=0)
    # Whether a manuscript was supplied (False → the cite-check report member is
    # disclose-skipped, and the transcription link is classified ABSENT).
    manuscript_present: bool
    # The CODE-emitted honest verdict (a fixed template filled by the per-link
    # classification, never LLM-composed) — relayed VERBATIM. It never stamps
    # "reproducible": it is a proof-of-what-is-mechanical + a ledger-of-what-is-
    # disclosed, never a reproducibility certificate.
    verdict: str
    # The union of every disclosed gap across the whole chain (dossier absent
    # stores + recipe gaps + cite-check's uncitable/skip + the env/data/provenance
    # disclosures). Each item names its origin + a disclosed detail; reported,
    # never fatal.
    disclosures: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "The union-of-disclosures ledger: every disclosed gap across the "
            "dossier, the recipe, the cite-check, and the env/data/provenance "
            "legs. Disclosed, never a failure."
        ),
    )
    # The full self-attesting VERIFY manifest (carried as a bare dict so the
    # BUNDLE-MEMBER vocabulary + per-link classification stay ops-owned, out of
    # the wire schema). A stranger recomputes bundle_sha256 from its path-sorted
    # entries offline.
    verify_manifest: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The top-level VERIFY manifest: bundle_schema_version, the path-sorted "
            "member entries, the per-link MECHANICAL/DISCLOSED/ABSENT "
            "classification, the disclosure ledger, the code-emitted verdict, the "
            "offline-verify recipe, and bundle_sha256. Self-attesting."
        ),
    )
