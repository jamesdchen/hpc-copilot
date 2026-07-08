"""Pydantic models for the ``notebook-draft-context`` query verb.

Wire surface over :mod:`hpc_agent.ops.notebook.draft_context_op` — the drafting
projection that mechanizes run #10's hand-written "drafting brief"
(``docs/design/draft-context.md``). Given a template ``.py`` and the same
caller-declared, OPAQUE roots the audit already uses, it emits ONE deterministic
artifact the drafting agent reads INSTEAD of N discovery greps:

1. **template sections** — the template's slugs + cell prose, verbatim;
2. **resolved engines** — for each name the template imports, the resolving file
   under ``source_roots`` (the SAME resolution ``notebook-lint`` uses — one
   definition), the symbol's ``path:lineno``, its signature (``ast.unparse`` of
   the def's args), first docstring line, and ``module_sha``. All AST; no import
   is ever executed;
3. **name-match call sites** — ``Call`` nodes whose name matches an engine,
   across ``source_roots``, as ``path:lineno`` (+count, capped, cap DISCLOSED —
   the no-silent-caps rule). Labeled honestly "name-match" (AST identity, not
   type resolution);
4. **inventory** — files + ``sha12`` + size under ``input_roots`` and each
   ``inventory_roots`` entry (roots OPAQUE — core never knows what a "config" is).
   When a ``.hpc/data_manifest.json`` is present, an entry CITES its recorded sha
   rather than re-hashing.

Altitude boundary: the projection LISTS, never NOMINATES. It attaches no meaning
to a root, ranks no section, and names no "baseline" config — that is
program-binding / pack knowledge. The ``markdown`` render is the TRUSTED-DISPLAY
class: the LLM relays / points at it, never re-summarizes.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class NotebookDraftContextSpec(BaseModel):
    """Inputs to ``notebook-draft-context`` — all roots caller-declared, OPAQUE.

    ``source_roots`` / ``input_roots`` default from the audit's RECORDED
    configuration when ``audit_id`` is given (the one-declaration reuse rule):
    an explicit list overrides, ``null`` falls back to the recorded config, and
    an audit with no recorded config yields empty roots. ``inventory_roots`` is
    draft-context-specific (extra opaque roots to list) and never defaulted.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-draft-context input spec")

    template: str = Field(
        min_length=1,
        description=(
            "Experiment-relative path to the template .py (jupytext percent "
            "format). Its slugs + cell prose are echoed verbatim and its imports "
            "are the declared engines the projection resolves."
        ),
    )
    source_roots: list[str] | None = Field(
        default=None,
        description=(
            "OPAQUE import roots the engines resolve under. null => default from "
            "the audit's recorded config (when audit_id is given), else empty."
        ),
    )
    input_roots: list[str] | None = Field(
        default=None,
        description=(
            "OPAQUE data roots whose files are listed in the inventory. null => "
            "default from the audit's recorded config (when audit_id is given), "
            "else empty."
        ),
    )
    inventory_roots: list[str] = Field(
        default_factory=list,
        description=(
            "Additional OPAQUE roots to list in the inventory (e.g. a configs "
            "dir). Never defaulted from the audit — draft-context-specific."
        ),
    )
    audit_id: str | None = Field(
        default=None,
        description=(
            "Optional notebook audit id. When given, absent source_roots / "
            "input_roots default from that audit's recorded configuration "
            "(interview.json audited_source, else the journaled record)."
        ),
    )


class TemplateSection(BaseModel):
    """One template section echoed verbatim: its slug + raw cell prose."""

    model_config = ConfigDict(extra="forbid", title="draft-context template section")

    slug: str
    source: str


class ResolvedEngine(BaseModel):
    """One name the template imports, resolved to its defining file (or not).

    ``resolved`` is false for an import that does not resolve under any
    ``source_root`` (stdlib / site-packages / external) — listed honestly, never
    silently dropped. When resolved to a ``from M import symbol`` origin, the
    ``symbol_lineno`` / ``signature`` / ``doc`` locate the def/class in ``file``;
    a plain ``import M`` has no single symbol (those stay null, ``doc`` carries
    the module docstring's first line). ``module_sha`` is
    :func:`hpc_agent.state.audit_source.sha256_normalized` of the file — the same
    hash ``notebook-lint`` reports.
    """

    model_config = ConfigDict(extra="forbid", title="draft-context resolved engine")

    name: str
    module: str
    symbol: str | None = None
    resolved: bool = False
    file: str | None = None
    symbol_lineno: int | None = None
    signature: str | None = None
    doc: str | None = None
    module_sha: str | None = None


class EngineCallSites(BaseModel):
    """Name-match ``Call`` sites for one engine across ``source_roots``.

    ``sites`` are ``path:lineno`` strings (AST identity — a call whose function
    name matches, NOT type resolution). ``count`` is the total found; ``sites``
    is capped at ``cap`` and ``truncated`` discloses when more existed (the
    no-silent-caps rule).
    """

    model_config = ConfigDict(extra="forbid", title="draft-context engine call sites")

    name: str
    sites: list[str] = Field(default_factory=list)
    count: int = 0
    cap: int
    truncated: bool = False


class InventoryEntry(BaseModel):
    """One inventoried file: its experiment-relative path, sha12, and size.

    ``cited`` is true when the sha/size came from ``.hpc/data_manifest.json``
    (the reuse seam) rather than from re-hashing the file here.
    """

    model_config = ConfigDict(extra="forbid", title="draft-context inventory entry")

    relpath: str
    sha12: str
    size: int
    cited: bool = False


class InventoryListing(BaseModel):
    """The files found under one declared root.

    ``kind`` records which declaration the root came from (``input`` /
    ``inventory``) — opaque provenance, never a semantic label. ``manifest_cited``
    is true when at least one entry was cited from the data manifest.
    """

    model_config = ConfigDict(extra="forbid", title="draft-context inventory listing")

    root: str
    kind: str
    entries: list[InventoryEntry] = Field(default_factory=list)
    manifest_cited: bool = False


class NotebookDraftContextResult(BaseModel):
    """The drafting projection — structured result plus its trusted-display render.

    ``markdown`` is the code-authored, deterministic render the drafting agent
    reads and the skill relays VERBATIM (same inputs => byte-identical bytes).
    Every other field is the structured half the same render is built from.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-draft-context output data")

    template_sections: list[TemplateSection] = Field(default_factory=list)
    resolved_engines: list[ResolvedEngine] = Field(default_factory=list)
    call_sites: list[EngineCallSites] = Field(default_factory=list)
    inventory: list[InventoryListing] = Field(default_factory=list)
    source_roots: list[str] = Field(default_factory=list)
    input_roots: list[str] = Field(default_factory=list)
    markdown: str = ""
