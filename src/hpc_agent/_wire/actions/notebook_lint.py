"""Pydantic models for the ``notebook-lint`` primitive (notebook-audit / T4).

Wire surface over :mod:`hpc_agent.ops.notebook.lint` — a read-only ``validate``
verb that runs four structural checks over a jupytext percent-format source
``.py`` (parsed by :mod:`hpc_agent.state.audit_source`) against a template and
caller-declared, opaque path/import roots:

* **structural completeness** — the template's marker slugs must appear in the
  source as an ORDER-PRESERVING SUBSEQUENCE (slugs are opaque — no content
  meaning is read, per the Q1 boundary flags);
* **executes-live** — path-shaped string literals are checked to exist under the
  caller-declared ``input_roots``; a COMPUTED path expression (an f-string / a
  ``+``-concatenation with a separator) cannot be verified and is recorded in
  ``unverifiable_paths`` (an honest gap, never silently skipped);
* **linked_sources** — imports resolving to a file under ``source_roots`` are
  reported as ``{module, file, module_sha}`` (import ORIGIN IDENTITY only —
  never import content/semantics);
* **template_import_shadowed** — a source section that defines or rebinds a
  name the TEMPLATE imports is reported (the template's imports are the
  caller's declared engines; an identical verbatim re-import is clean). The
  shadow list is derived only from the template's own import statements —
  agnostic, no name lists or knobs.

Findings are REPORTED, never raised: the graduation gate refuses, the lint
reports. Only a malformed spec or unparseable source raises ``SpecInvalid``.

The result shape is consumed OPAQUELY downstream: T5's tier computation reads
"zero findings for a section" as one auto-clear precondition, and T9 records
``linked_sources`` at sign-off and drift-checks them at the gate.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: The four lint rules a finding can come from. Kept as a Literal so a caller /
#: T5 can switch on ``rule`` without a magic-string typo going unnoticed.
LintRule = Literal[
    "structural_completeness",
    "executes_live",
    "linked_sources",
    "template_import_shadowed",
]


class NotebookLintFinding(BaseModel):
    """One reported lint finding.

    * ``rule`` — which check produced it.
    * ``section`` — the source (or template) section slug the finding is about,
      or ``None`` for a module-level finding. T5 counts findings per ``section``:
      a section with zero findings is one auto-clear precondition.
    * ``detail`` — human-readable description of the specific violation.
    * ``evidence`` — opaque structured payload (the offending slug, path literal,
      line number, …) for a renderer; never interpreted by core.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-lint finding")

    rule: LintRule
    section: str | None = None
    detail: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class LinkedSource(BaseModel):
    """One import that resolved to a file under a caller ``source_root``.

    Judges import ORIGIN IDENTITY only: ``module`` is the imported dotted name,
    ``file`` is the resolved file's path (relative to the experiment dir when it
    is under it, else absolute), and ``module_sha`` is
    :func:`hpc_agent.state.audit_source.sha256_normalized` over the file text —
    the SAME hashing primitive T9 recomputes to drift-check the link at sign-off.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-lint linked source")

    module: str
    file: str
    module_sha: str


class NotebookLintInput(BaseModel):
    """Spec for ``notebook-lint`` — all paths are caller-declared.

    ``source`` / ``template`` are ``.py`` relpaths under the experiment dir
    (or absolute). ``input_roots`` / ``source_roots`` are OPAQUE caller-declared
    root lists — core never attaches a meaning to a root, it only joins + tests
    existence (data roots) or resolves import origins (import roots).
    """

    model_config = ConfigDict(extra="forbid", title="notebook-lint input spec")

    source: str = Field(min_length=1)
    template: str = Field(min_length=1)
    # Opaque data-path roots the executes-live rule tests literals against.
    input_roots: list[str] = Field(default_factory=list)
    # Opaque import roots the linked-sources rule resolves imports under.
    source_roots: list[str] = Field(default_factory=list)


class NotebookLintResult(BaseModel):
    """The lint report — a shape T5/T9/the skill consume opaquely.

    * ``findings`` — every reported violation (empty = clean).
    * ``unverifiable_paths`` — computed path expressions that could not be
      checked (the honest executes-live gap).
    * ``linked_sources`` — imports resolved under ``source_roots``, with hashes.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-lint output data")

    findings: list[NotebookLintFinding] = Field(default_factory=list)
    unverifiable_paths: list[str] = Field(default_factory=list)
    linked_sources: list[LinkedSource] = Field(default_factory=list)
