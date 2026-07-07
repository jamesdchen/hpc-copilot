"""Notebook assembly, deterministic normalization, and output canonicalization.

Pure helpers shared by the render verb. No hpc-agent core import here beyond the
audit-view dataclasses passed in — jupytext/nbformat are the RENDERER's deps and
stay on this side of the boundary (D-source).

Determinism (pinned by a test): a NON-executed render of identical inputs is
byte-identical. jupytext/nbformat inject moving metadata — a ``jupytext`` block
carrying a version string, random per-cell ``id`` values (nbformat >= 4.5), and
per-cell ``lines_to_next_cell`` hints. :func:`normalize_notebook` erases all of
them: fixed notebook metadata, empty per-cell metadata, and deterministic
sequential cell ids. No timestamps ever enter a non-executed render.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

import nbformat
from nbformat.v4 import new_markdown_cell

if TYPE_CHECKING:
    from collections.abc import Sequence

# The section marker, matched exactly as core does (state/audit_source._MARKER_RE)
# — re-derived here rather than importing the private core symbol, so the plugin
# never reaches into core internals (the audit view supplies the authoritative
# shas; this only needs to find which notebook cell OPENS a section).
_MARKER_RE = re.compile(r"^#\s*hpc-audit-section:\s*(?P<slug>.*)$")

# The recognizable markers a rendered notebook carries. The sign-off marker is
# what ``notebook-ingest-signoffs`` keys on; the sentinel line separates the
# scaffold instructions (above) from the human's typed response area (below).
SIGNOFF_MARKER = "hpc-audit-signoff"
SIGNOFF_MARKER_RE = re.compile(r"<!--\s*hpc-audit-signoff:\s*(?P<slug>\S+)\s*-->")
SIGNOFF_SENTINEL = "<!-- type your sign-off below this line; leave unchanged to skip -->"


def _first_nonblank(source: str) -> str:
    """The first non-blank line of *source* (``""`` when all-blank)."""
    for line in source.splitlines():
        if line.strip():
            return line
    return ""


def cell_section_slug(cell: Any) -> str | None:
    """The section slug a cell OPENS, or ``None`` (a non-marker / preamble cell).

    A section-start cell is one whose first non-blank source line is the
    ``# hpc-audit-section: <slug>`` marker (core's first-non-blank-in-cell rule;
    jupytext keeps the comment as ordinary cell source).
    """
    match = _MARKER_RE.match(_first_nonblank(cell.get("source", "")))
    return match.group("slug").strip() if match else None


def assign_cell_sections(cells: Sequence[Any]) -> list[str | None]:
    """Map each cell to its owning section slug (``None`` = preamble/outside).

    A marker cell starts a section; every following cell belongs to it until the
    next marker. Mirrors core segmentation without re-parsing the source.
    """
    out: list[str | None] = []
    current: str | None = None
    for cell in cells:
        slug = cell_section_slug(cell)
        if slug is not None:
            current = slug
        out.append(current)
    return out


# ── deterministic output canonicalization (the --execute output_sha) ─────────


def _canonical_output(output: Any) -> dict[str, Any]:
    """Reduce one cell output to its deterministic essence.

    Drops everything a re-run varies or a kernel stamps: ``execution_count``,
    output ``metadata`` (timing/transients), and error tracebacks (ANSI + absolute
    paths). Keeps the load-bearing content — a stream's name+text, a result's
    mime-keyed data, an error's ename+evalue — so identical deterministic code
    yields an identical ``output_sha`` across runs and machines.
    """
    otype = output.get("output_type", "")
    if otype == "stream":
        return {
            "output_type": "stream",
            "name": output.get("name", ""),
            "text": output.get("text", ""),
        }
    if otype == "error":
        return {
            "output_type": "error",
            "ename": output.get("ename", ""),
            "evalue": output.get("evalue", ""),
        }
    if otype in ("execute_result", "display_data"):
        return {"output_type": otype, "data": dict(output.get("data", {}))}
    return {"output_type": otype}


def section_output_sha(cells: Sequence[Any]) -> tuple[str, bool]:
    """Return ``(output_sha, error)`` over the code *cells* of one section.

    ``output_sha`` = sha256 over the canonical JSON of every code cell's
    canonicalized outputs, in cell order; ``error`` is True iff any output is an
    ``error`` output (a cell raised). Markdown cells carry no outputs and never
    enter the hash.
    """
    canon: list[dict[str, Any]] = []
    error = False
    for cell in cells:
        if cell.get("cell_type") != "code":
            continue
        for output in cell.get("outputs", []):
            canon.append(_canonical_output(output))
            if output.get("output_type") == "error":
                error = True
    payload = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), error


# ── annotation cells ─────────────────────────────────────────────────────────


def header_cell() -> Any:
    """The leading cell: this notebook is a RENDER, never the source of truth."""
    text = (
        "<!-- hpc-audit-render -->\n"
        "# Audit render — NOT the source of truth\n\n"
        "This notebook is a deterministic RENDER of an audited source `.py` "
        "(jupytext percent format). The `.py` is the source of truth: edits happen "
        "there and are re-rendered — never hand-edit code here. Each audited "
        "section is preceded by an **audit cell** (status / tier / hashes from the "
        "core view). To sign off a section, type your review into its sign-off "
        "cell below the marked line, then run `hpc-agent notebook-ingest-signoffs`."
    )
    return new_markdown_cell(text)


def audit_cell(
    slug: str, *, status: str, tier: str, classification: str, section_sha: str, view_sha: str
) -> Any:
    """The per-section audit cell (status/tier/classification/short hashes)."""
    text = (
        f"<!-- hpc-audit-cell: {slug} -->\n"
        f"**Audit section `{slug}`** — status: `{status}` · tier: `{tier}`\n\n"
        f"- classification: `{classification}`\n"
        f"- section_sha: `{section_sha[:12]}`\n"
        f"- view_sha: `{view_sha[:12]}`"
    )
    return new_markdown_cell(text)


def signoff_cell(slug: str) -> Any:
    """The sign-off scaffold for a human-required section.

    The human types their review AFTER the sentinel line; an unchanged scaffold
    (nothing after the sentinel) is read as "no sign-off" by the ingest verb.
    """
    text = (
        f"<!-- {SIGNOFF_MARKER}: {slug} -->\n"
        f"Sign off section `{slug}` by typing your review below the line. Name the "
        f'section (`{slug}`) and engage the specific change — a bare "ok" is '
        "refused by the sign-off gate.\n\n"
        f"{SIGNOFF_SENTINEL}\n"
    )
    return new_markdown_cell(text)


def extract_typed_signoff(cell_source: str) -> str | None:
    """The human-typed text below the sentinel in a sign-off cell, or ``None``.

    Returns the stripped text AFTER :data:`SIGNOFF_SENTINEL` (the response area).
    ``None`` when the cell is not a sign-off cell, has no sentinel, or the area is
    empty/unchanged — the provenance contract: only typed text counts, the
    scaffold default is not a sign-off.
    """
    if SIGNOFF_MARKER not in cell_source or SIGNOFF_SENTINEL not in cell_source:
        return None
    _, _, tail = cell_source.partition(SIGNOFF_SENTINEL)
    typed = tail.strip()
    return typed or None


# ── deterministic normalization ──────────────────────────────────────────────

_FIXED_METADATA: dict[str, Any] = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python"},
}


def normalize_notebook(nb: Any) -> Any:
    """Strip every moving field so a non-executed render is byte-deterministic.

    Fixed notebook metadata (no ``jupytext`` version block, no ``language_info``
    version), empty per-cell metadata (drops ``lines_to_next_cell`` + nbclient
    execution-timing stamps), and deterministic sequential cell ids
    (``c0``, ``c1``, …). Outputs and execution_count are left untouched — an
    executed render legitimately carries them; only their surrounding metadata is
    normalized.
    """
    nb.metadata = dict(_FIXED_METADATA)
    nb.nbformat = 4
    nb.nbformat_minor = 5
    for index, cell in enumerate(nb.cells):
        cell.metadata = {}
        cell.id = f"c{index}"
    return nb


def write_notebook(nb: Any) -> str:
    """Serialize *nb* to a deterministic ipynb string (trailing newline)."""
    text = str(nbformat.writes(nb, version=nbformat.NO_CONVERT))
    return text if text.endswith("\n") else text + "\n"
