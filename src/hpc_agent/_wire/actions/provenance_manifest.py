"""Input spec for the ``provenance-manifest`` primitive (#312).

The manifest itself is *derived* state — recomputed from the run
sidecars on demand — so the input is just the campaign selector; every
provenance fact (``cmd_sha``, ``tasks_py_sha``, ``data_sha``,
``env_hash``, cluster, trial tokens) is read from what the submit path
already recorded, never asserted by the caller.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ProvenanceManifestInput(BaseModel):
    """Wire input for ``provenance-manifest``."""

    model_config = ConfigDict(extra="forbid", title="provenance-manifest input spec")

    campaign_id: str = Field(
        min_length=1,
        description=(
            "Campaign tag whose runs the manifest covers — the same "
            "campaign_id stamped on each run sidecar at submit time "
            "(HPC_CAMPAIGN_ID convention). Path separators are sanitized "
            "for the output filename; an unknown campaign yields a "
            "well-formed empty manifest (run_count=0), not an error."
        ),
    )
