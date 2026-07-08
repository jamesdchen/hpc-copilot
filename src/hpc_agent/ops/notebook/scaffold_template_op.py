"""``notebook-scaffold-template`` — content-free audit-template scaffolding.

The audit-template analog of ``build-template``'s shape-level scaffolding
(notebook-audit substrate, ``docs/design/notebook-audit.md``). A ``mutate``
verb: given an ordered list of section slugs and an output path, it writes a
jupytext percent-format ``.py`` containing a short format-only module
docstring plus one ``# %%`` cell per slug — each cell's first non-blank line
is its ``# hpc-audit-section: <slug>`` marker, followed by a one-line
placeholder comment. Cell BODIES are caller-owned; the verb emits format
machinery only, never content (slugs stay opaque — the Q1
substrate-not-semantics boundary).

**One definition (the load-bearing constraint).** The marker line and the cell
delimiter come from :mod:`hpc_agent.state.audit_source` — the ONE reader of the
percent-format grammar (:func:`format_section_marker` /
:data:`CELL_DELIMITER`); this writer never re-spells either as a fresh literal,
so it can never emit a file the parser would not recognize.

**Round-trip verification.** After writing, the verb re-reads its own output
and parses it with the SAME :func:`parse_percent_source` every audit consumer
uses; the parsed slugs must equal the requested slugs exactly. On any mismatch
(or parse failure) the partial file is DELETED and the verb refuses — a
scaffold that does not survive its own parse must never be left on disk.

Refusals (all :class:`hpc_agent.errors.SpecInvalid`, the offending slug named
where one exists): an empty slug list, a duplicate slug (would fail the
section parse anyway — refused EARLY with a clear message), a malformed slug
(surfaced by the marker grammar itself), and an EXISTING output file (no force
flag in v1 — the caller deletes first; never silently clobbered).

This file lives inside the ``notebook`` subject, reaching only the ``state.*``
substrate and its own wire models — the subject-imports lint is satisfied by
construction.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.notebook_scaffold_template import (
    NotebookScaffoldTemplateResult,
    NotebookScaffoldTemplateSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state.audit_source import (
    CELL_DELIMITER,
    format_section_marker,
    parse_percent_source,
)

__all__ = ["notebook_scaffold_template"]

_PRIMITIVE = "notebook-scaffold-template"

#: The one-line placeholder each scaffolded cell carries under its marker.
#: Format-only prose: it says WHO owns the body, never what the body means.
_PLACEHOLDER_COMMENT = "# (caller-owned section body - replace this placeholder)"

#: The generated module docstring. Format-only prose: what the file is, that
#: the markers are the section inventory, that cell bodies are caller-owned.
#: No line may sit at column 0 starting with the cell delimiter or the marker
#: token — the parser is line-based and docstring-illiterate by design.
_MODULE_DOCSTRING = '''\
"""Audit-template scaffold (jupytext percent-format .py).

Structure only. Each cell below opens with a section marker comment; the
markers are this file's section inventory. Every cell body is caller-owned:
replace each placeholder comment with that section's content, keeping the
marker as the first non-blank line of its cell.
"""\
'''


def _resolve_output_path(experiment_dir: Path, output_path: str) -> Path:
    """Resolve the caller's output path (relative → under the experiment dir)."""
    path = Path(output_path)
    if not path.is_absolute():
        path = experiment_dir / path
    return path


def _render_scaffold(slugs: list[str]) -> str:
    """Render the percent-format scaffold text for *slugs*.

    One ``# %%`` cell per slug: the marker line (via the ONE grammar's
    :func:`format_section_marker`, which raises SpecInvalid naming a malformed
    slug) then the placeholder comment. The docstring is the implicit leading
    cell — preamble to the parser, covered by ``module_sha``.
    """
    blocks = [_MODULE_DOCSTRING]
    for slug in slugs:
        marker = format_section_marker(slug)  # SpecInvalid names a bad slug
        blocks.append(f"{CELL_DELIMITER}\n{marker}\n{_PLACEHOLDER_COMMENT}")
    return "\n\n".join(blocks) + "\n"


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect(
            "file_write",
            "<experiment>/<output_path> (new file; an existing one is refused)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    # Not idempotent: the verb refuses an existing output file (no force flag),
    # so an immediate retry of a SUCCEEDED call is itself refused — honest, not
    # retry-equivalent.
    idempotent=False,
    cli=CliShape(
        help=(
            "Scaffold a content-free notebook-audit template: write a jupytext "
            "percent-format .py with one # %% cell per requested section slug, "
            "each opening with its hpc-audit-section marker (the ONE marker "
            "grammar from state/audit_source.py) plus a one-line placeholder "
            "comment. Cell bodies are caller-owned; the verb emits format "
            "machinery only. Round-trip verified: the written file is re-parsed "
            "and must yield exactly the requested slugs, else it is deleted and "
            "the call refused. Refuses empty/duplicate/malformed slugs and an "
            "existing output file (no force flag - delete it first). Pure local "
            "write, no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=NotebookScaffoldTemplateSpec,
        schema_ref=SchemaRef(input="notebook_scaffold_template"),
    ),
    agent_facing=True,
)
def notebook_scaffold_template(
    *, experiment_dir: Path, spec: NotebookScaffoldTemplateSpec
) -> NotebookScaffoldTemplateResult:
    """Write a content-free percent-format audit-template scaffold.

    Renders one marker cell per ``spec.slugs`` entry (marker syntax from the
    ONE grammar in :mod:`hpc_agent.state.audit_source`), writes it to
    ``spec.output_path``, then round-trips the written file through
    :func:`parse_percent_source` — the parsed slugs must equal the requested
    slugs, else the partial file is deleted and the call refused.

    Raises
    ------
    :class:`hpc_agent.errors.SpecInvalid`
        Empty ``slugs``; a duplicate or malformed slug (the offending slug is
        named); an output file that already exists (no force flag in v1); or
        a written scaffold that fails its own round-trip parse (deleted before
        the raise — never left on disk).
    """
    experiment_dir = Path(experiment_dir)

    if not spec.slugs:
        raise errors.SpecInvalid(
            f"{_PRIMITIVE} requires at least one section slug (slugs is empty)"
        )
    seen: set[str] = set()
    for slug in spec.slugs:
        if slug in seen:
            raise errors.SpecInvalid(
                f"{_PRIMITIVE} duplicate section slug {slug!r}: each "
                "hpc-audit-section slug must be unique (a duplicate would fail "
                "the section parse)"
            )
        seen.add(slug)

    output = _resolve_output_path(experiment_dir, spec.output_path)
    if output.exists():
        raise errors.SpecInvalid(
            f"{_PRIMITIVE} output file already exists: {output} "
            "(no force flag in v1 - delete it first)"
        )

    # Render BEFORE touching the filesystem: a malformed slug refuses here
    # (format_section_marker names it) and no partial file is ever created.
    content = _render_scaffold(spec.slugs)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")

    # ROUND-TRIP VERIFICATION: re-read the bytes on disk (not the in-memory
    # string) and parse through the ONE grammar. A scaffold the parser does not
    # read back as exactly the requested slugs is deleted, never left partial.
    try:
        parsed = parse_percent_source(output.read_text(encoding="utf-8"))
    except errors.SpecInvalid as exc:
        output.unlink(missing_ok=True)
        raise errors.SpecInvalid(
            f"{_PRIMITIVE} wrote a scaffold that failed its own round-trip "
            f"parse (file deleted): {exc}"
        ) from exc
    if list(parsed.slugs) != list(spec.slugs):
        output.unlink(missing_ok=True)
        raise errors.SpecInvalid(
            f"{_PRIMITIVE} round-trip verification failed (file deleted): "
            f"requested slugs {list(spec.slugs)!r}, parsed back "
            f"{list(parsed.slugs)!r}"
        )

    return NotebookScaffoldTemplateResult(
        output_path=str(output),
        slugs=list(parsed.slugs),
        module_sha=parsed.module_sha,
    )
