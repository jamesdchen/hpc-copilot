"""Pydantic models for the ``notebook-scaffold-template`` primitive.

Wire surface over :mod:`hpc_agent.ops.notebook.scaffold_template_op` — a
content-free scaffold generator for notebook-audit templates. Given an ordered
list of section slugs and an output path, the verb writes a jupytext
percent-format ``.py`` whose ``# %%`` cells each open with a
``# hpc-audit-section: <slug>`` marker (the marker grammar comes from
:mod:`hpc_agent.state.audit_source` — one definition for both read and write
sides) followed by a one-line placeholder comment. Cell bodies are
CALLER-OWNED: the scaffold carries format machinery only, never content (the
audit-template analog of ``build-template``'s shape-level scaffolding).

Slugs are OPAQUE identifiers — core validates their filesystem-safe shape and
uniqueness, never their meaning (the Q1 substrate-not-semantics boundary).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class NotebookScaffoldTemplateSpec(BaseModel):
    """Spec for ``notebook-scaffold-template``.

    * ``slugs`` — the ordered section inventory the scaffold declares, one
      ``# %%`` marker cell per slug. Must be non-empty and duplicate-free
      (a duplicate would fail the section parse anyway — refused EARLY with
      the offending slug named).
    * ``output_path`` — where the scaffold ``.py`` is written (relative paths
      resolve under the experiment dir). An EXISTING file is refused — there
      is no force flag; the caller deletes first.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-scaffold-template input spec")

    slugs: list[str]
    output_path: str = Field(min_length=1)


class NotebookScaffoldTemplateResult(BaseModel):
    """The scaffold receipt.

    * ``output_path`` — the resolved path the scaffold was written to.
    * ``slugs`` — the section slugs as VERIFIED by the round-trip parse of the
      written file (equal to the requested slugs by construction — the verb
      refuses and deletes the file otherwise).
    * ``module_sha`` — :func:`hpc_agent.state.audit_source.sha256_normalized`
      over the written module, from the same round-trip parse (the fingerprint
      a later audit of the untouched scaffold reproduces).
    """

    model_config = ConfigDict(extra="forbid", title="notebook-scaffold-template output data")

    output_path: str
    slugs: list[str]
    module_sha: str
