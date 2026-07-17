"""Pydantic models for the ``extract-recipe`` query verb.

``extract-recipe`` is the artifact → minimal-run-set → runnable-recipe walk
(clean-reproduction extraction, proposal #1). Given a citable artifact reference
(a ``run_id``, a ``campaign_id``, or a path to a reduced-metrics artifact), it
walks BACK to the MINIMAL contributing run-set — canary siblings, superseded
lineage members, and dead-end runs mechanically EXCLUDED (each exclusion
disclosed + counted) — and emits one deterministic recipe: each contributing
run's full provenance fingerprint (including ``hpc_agent_version``, the wheel), a
recipe-specific signature over ONLY the minimal set, the runnable re-derivation
steps, the receipts chain, and every gap it cannot bridge DISCLOSED (never
papered over).

Boundary posture (the :mod:`hpc_agent._wire.queries.run_story` /
:mod:`hpc_agent._wire.queries.trace` posture — flat, no domain vocabulary in
field names): the recipe is IDENTITY (which runs, at which shas) + ORDERING (the
re-derivation steps) + COUNTING (exclusion counts, receipt presence) over opaque
records. It never names a metric, never picks a "best" run, never concludes. The
heterogeneous per-run / per-exclusion / per-step payloads stay
``list[dict[str, Any]]`` (the ``trace`` node/edge precedent): a consumer
dispatches on the payload's own keys, and over-constraining them in the wire
schema would only invite a domain-semantics field name.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExtractRecipeInput(BaseModel):
    """Inputs to ``extract-recipe`` — exactly one seed reference.

    Exactly one of ``run_id`` / ``campaign_id`` / ``aggregate_path`` names the
    citable artifact to walk back from. ``run_id`` / ``campaign_id`` are
    identities core owns; ``aggregate_path`` is a path to a reduced-metrics
    artifact — a ``metrics_aggregate.json`` is read for its Task-1
    ``contributing_run_ids`` provenance, while a pack ``*.csv`` is accepted only
    as an OPAQUE citation whose provenance is its containing run's (its content
    is NEVER parsed — the dossier no-parse boundary, R2).
    """

    model_config = ConfigDict(extra="forbid", title="extract-recipe input spec")

    run_id: str | None = Field(
        default=None,
        description=(
            "Walk back from this run's reduced table (its "
            "_aggregated/<run_id>/metrics_aggregate.json contributing set, or its "
            "lineage when no table was persisted). Excludes the other two seeds."
        ),
    )
    campaign_id: str | None = Field(
        default=None,
        description=(
            "Walk back from this campaign — the campaign's runs minus canary / "
            "superseded / dead-end members. Excludes the other two seeds."
        ),
    )
    aggregate_path: str | None = Field(
        default=None,
        description=(
            "Path to a reduced-metrics artifact. A metrics_aggregate.json is read "
            "for its contributing_run_ids provenance; a pack *.csv is an OPAQUE "
            "citation (never parsed) whose provenance is its containing run's. "
            "Excludes the other two seeds."
        ),
    )


class ExtractRecipeResult(BaseModel):
    """The derived clean-reproduction recipe — minimal set, fingerprints, gaps.

    ``recipe_signature`` is a deterministic digest over ONLY the minimal run-set's
    fingerprints (a table-specific attestation, not a whole-campaign one): a
    reviewer re-derives the same recipe and re-hashes to confirm the minimal set
    has not drifted. Every excluded run rides ``excluded`` with a countable
    reason; every gap the walk cannot bridge rides ``gaps`` — disclosed, never
    papered.
    """

    model_config = ConfigDict(extra="forbid", title="extract-recipe output data")

    recipe_schema_version: int = Field(
        ge=1,
        description="Bumped when the emitted recipe shape changes incompatibly.",
    )
    seed_kind: Literal["run", "campaign", "aggregate"] = Field(
        description="Which seed reference the recipe was walked back from.",
    )
    seed_ref: str = Field(
        description="The seed's identity / path verbatim.",
    )
    artifact_opaque: bool = Field(
        default=False,
        description=(
            "True when the cited artifact was accepted as an OPAQUE citation "
            "(a pack *.csv whose content is never parsed) — its provenance is "
            "its containing run's, disclosed as a gap (R2)."
        ),
    )
    minimal_run_ids: list[str] = Field(
        default_factory=list,
        description="The minimal contributing run-set, after all exclusions.",
    )
    runs: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "One fingerprint per minimal run: {run_id, cmd_sha, tasks_py_sha, "
            "data_sha, data_manifest_sha, env_hash, env_lock_sha, "
            "hpc_agent_version, cluster, profile} — identity fields only, no "
            "metric value. The two signed legs (hpc_agent_version, env_lock_sha) "
            "each carry a <field>_source disclosing signed-manifest vs sidecar."
        ),
    )
    excluded: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "One entry per mechanically-excluded run: {run_id, reason} where "
            "reason is 'canary' / 'superseded' / 'dead-end'. Every exclusion is a "
            "disclosed, countable fact."
        ),
    )
    recipe_signature: str = Field(
        description="Deterministic 64-hex digest over ONLY the minimal set's fingerprints.",
    )
    rederivation_steps: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "The runnable re-derivation steps as structured hints: a reproduce-run "
            "+ submit-s2 pair per contributing run, then the aggregate invocation. "
            "Emitted as a runnable artifact, not prose; extract-recipe NEVER "
            "executes them."
        ),
    )
    receipts: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "The receipts chain, walked per run: {run_id, harvest_receipt (bool), "
            "reproduction_receipt (bool), greenlights (count)} — presence / counts "
            "only, never a verdict."
        ),
    )
    gaps: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Every receipts-chain gap the walk could not bridge, disclosed: "
            "{code, detail} for the G4 breaks (table→run-set absent, pack-csv "
            "opaque, operator-bypass / journal-provenance-absent)."
        ),
    )
    markdown: str = Field(
        default="",
        description="The code-rendered recipe (empty when not requested).",
    )
