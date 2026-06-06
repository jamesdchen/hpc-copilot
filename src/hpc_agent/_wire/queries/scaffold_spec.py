"""Pydantic model for the ``scaffold-spec`` query primitive's output (#287).

``scaffold-spec`` emits a populated, schema-valid skeleton for ANOTHER
verb's ``--spec`` input, pulling values from ``load-context`` /
``clusters.yaml`` / ``compute-run-id`` so the agent stops divining the
target schema one ``spec_invalid`` at a time. There is no input model —
the verb takes flags (``--verb`` / ``--cluster`` / ``--run-name``), not a
``--spec``; only the output is a wire contract.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ScaffoldSpecResult(BaseModel):
    """Shape of the ``data`` field on a ``scaffold-spec`` envelope.

    The agent reads ``spec``, overrides the ``unresolved_fields`` (the
    handful of values context could not supply — surfaced as schema-valid
    placeholders), then invokes the target verb with the result. One
    scaffold + one edit + one invoke, instead of N rounds of
    ``spec_invalid`` feedback.
    """

    model_config = ConfigDict(extra="forbid", title="scaffold-spec output data")

    verb: str = Field(
        description="The target verb this skeleton is for (e.g. `resolve-submit-inputs`).",
    )
    spec: dict[str, Any] = Field(
        description=(
            "The populated, schema-valid skeleton. Already validated against the "
            "target verb's input model, so the verb will not reject its shape — "
            "pass it (after filling `unresolved_fields`) to `hpc-agent <verb> --spec`."
        ),
    )
    unresolved_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Dotted paths (e.g. `submit.ssh_target`, `sidecar.executor`) whose "
            "values are PLACEHOLDERS context could not supply. The caller must "
            "fill or override these before invoking the target verb — they are "
            "schema-valid so the skeleton validates, but they are not real."
        ),
    )
    sources: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-field provenance: which context source populated each value "
            "(`clusters.yaml#<cluster>.ssh_target`, `compute-run-id(<run>)`, "
            "`load-context latest_run.*`), or a `placeholder — ...` marker for "
            "the unresolved ones."
        ),
    )
    supported_verbs: list[str] = Field(
        description="The verbs `scaffold-spec` can populate today.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Context-gathering degradations (e.g. clusters.yaml unreadable, or "
            "`.hpc/tasks.py` absent so run_id/cmd_sha came back as placeholders)."
        ),
    )
