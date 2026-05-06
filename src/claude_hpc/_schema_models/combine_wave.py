"""Pydantic model for the ``combine-wave`` mutator's output."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ._shared import RunIdLoose


class CombineWaveResult(BaseModel):
    """Shape of the ``data`` field on a successful ``aggregate --run-id <id> --wave <N>`` envelope.

    Note: ``combined: false`` is a SUCCESSFUL envelope (the call
    recorded the failure to the journal); inspect ``data.combined``
    to branch.
    """

    model_config = ConfigDict(extra="forbid", title="aggregate (combine-wave) output data")

    run_id: RunIdLoose
    wave: int = Field(ge=0)
    combined: bool = Field(
        description=(
            "True when the cluster-side combiner exited 0 and the "
            "wave landed in combined_waves. False when the combiner "
            "exited non-zero and the wave landed in failed_waves; "
            "inspect stderr_tail."
        ),
    )
    output_dir: str = Field(
        description="Absolute path on the cluster where the wave's combined partial was written.",
    )
    stdout_tail: str | None = None
    stderr_tail: str | None = None
