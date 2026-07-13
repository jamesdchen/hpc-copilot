"""Wire models for the ``data-manifest`` primitive (``docs/design/data-manifest.md``).

Spec ``{roots?, output_path?}`` — deliberately bare: ``roots`` defaults to the
experiment's ONE existing input declaration (``audited_source.input_roots``), and
a hardcoded ``data/`` default is REFUSED (core never guesses which directories are
data). ``roots`` are OPAQUE relpath strings — core hashes bytes, never parses a
format.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DataManifestSpec(BaseModel):
    """Input for ``data-manifest`` — mint an identity record for the input data."""

    model_config = ConfigDict(extra="forbid", title="data-manifest input spec")

    roots: list[str] | None = Field(
        default=None,
        description=(
            "Optional OPAQUE relpath roots (files or directories) whose bytes are "
            "hashed into the manifest. When absent, defaults to the experiment's "
            "existing input declaration (interview.json's "
            "audited_source.input_roots) — the ONE 'what are my inputs' "
            "declaration. With neither present the verb REFUSES (spec_invalid): "
            "core never guesses a data/ directory."
        ),
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Optional manifest destination relpath (or absolute). Defaults to "
            ".hpc/data_manifest.json — the copilot-consumed caller record that "
            "sits with interview.json / axes.yaml."
        ),
    )


class DataManifestResult(BaseModel):
    """Result of a ``data-manifest`` mint — counts + the manifest-doc identity."""

    model_config = ConfigDict(extra="forbid", title="data-manifest result")

    manifest_path: str = Field(description="Path the manifest was written to.")
    roots: list[str] = Field(description="The roots the manifest was minted over.")
    manifest_doc_sha: str = Field(
        description=(
            "Canonical-JSON sha256 of the file records — the manifest's identity "
            "AS A DOCUMENT (the journaled mint's 'new known-good' fingerprint). "
            "Distinct from the raw-byte file-content shas inside it."
        ),
    )
    file_count: int = Field(description="Number of files recorded.")
    files: dict[str, Any] = Field(
        description="The {relpath: {sha256, size, built_by?}} records (identities only).",
    )
