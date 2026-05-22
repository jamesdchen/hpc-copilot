"""Pydantic model for the ``export-package`` scaffold's input.

``export-package`` builds the experiment's ``src/`` package from its
notebooks. It is convention-driven — notebooks under
``notebooks/{pipeline,executors,scripts}/`` export; the output path and
the exporter are both derived, so the input carries almost nothing.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ExportPackageInput(BaseModel):
    """Options for the notebook → ``src/`` build."""

    model_config = ConfigDict(extra="forbid", title="export-package input")

    force: bool = Field(
        default=False,
        description=(
            "Ignore the content-hash build cache (.hpc/.build-cache.json) "
            "and re-export every notebook. Default reuses unchanged "
            "notebooks' prior output."
        ),
    )
    notebooks_dir: str = Field(
        default="notebooks",
        min_length=1,
        description=(
            "Directory (relative to experiment_dir) holding the "
            "{pipeline,executors,scripts}/ notebook subtrees. Convention "
            "default 'notebooks'."
        ),
    )
