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
import importlib.metadata
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


# ── output canonicalization (the --execute output_sha) ───────────────────────
#
# The canonical-form DEFINITION — "what in an output is transient vs meaningful"
# — is outsourced to nbdime (Jupyter's own notebook diff tool), the maintained
# encoding of that distinction. We do NOT re-derive it by hand; we strip exactly
# the output-level fields nbdime classifies as TRANSIENT and hash the remainder
# verbatim. The sha-attestation shape (sha256 over canonical JSON) stays ours.
#
# The nbdime symbols that DEFINE the transient set (nbdime 4.x):
#   • ``nbdime.merging.notebooks`` — ``strategies.transients`` lists
#     ``/cells/*/outputs/*/execution_count`` (and clears execution_count) as the
#     output-level transient whose deletion is always safe.
#   • ``nbdime.diffing.notebooks.set_notebook_diff_targets`` — with
#     ``metadata=False``/``details=False`` it marks ``/cells/*/outputs/*/metadata``
#     and the output ``execution_count`` as ignored (transient) diff targets.
#   • ``nbdime.diffing.notebooks.compare_output_strict`` /
#     ``compare_output_approximate`` — "Deliberately skipping metadata and
#     execution count", the pairwise output identity nbdime actually diffs on.
# Together these define the output-level transient fields as exactly
# ``{execution_count, metadata}``. Everything else nbdime treats as MEANINGFUL —
# a stream's name+text, a result's mime data, an error's ename/evalue AND its
# traceback — so those enter the hash verbatim (nbdime normalizes tracebacks
# only at compare-time via ``compare_tracebacks``; that normalization has no
# public canonical-string surface, so an erroring section's sha is stable
# across re-runs on one machine but is NOT guaranteed cross-machine — a
# deliberate consequence of adopting nbdime's "traceback is meaningful"
# classification over the prior hand-rolled traceback-stripping).
CANONICALIZER = "nbdime"

# The output-level fields nbdime marks transient (see the symbol list above).
# Import-checked against nbdime's public diffing surface at module load so this
# constant cannot silently drift from nbdime's own definition.
_NBDIME_TRANSIENT_OUTPUT_FIELDS: frozenset[str] = frozenset({"execution_count", "metadata"})

# Fail loudly at import if the nbdime symbols this canonical form is derived from
# are gone (a rename/removal in a future major) — so the plugin never silently
# hashes against a definition nbdime no longer holds. The ``<5`` pin plus this
# guard is how the canonicalizer identity stays honest.
import nbdime.diffing.notebooks as _nbd  # noqa: E402

for _sym in ("set_notebook_diff_targets", "compare_output_strict", "compare_output_approximate"):
    if not hasattr(_nbd, _sym):  # pragma: no cover - tripped only on an nbdime break
        raise ImportError(
            f"nbdime.diffing.notebooks.{_sym} is absent — this plugin derives its "
            "output canonical form from nbdime's transient-field definition and "
            "cannot trust an nbdime whose diffing surface has changed."
        )
del _nbd


def canonicalizer_version() -> str:
    """The installed nbdime version (``importlib.metadata``) — the receipt's identity.

    Recorded alongside every ``output_sha`` so a shift in nbdime's transient
    definition across a version reads as an explicit canonicalizer change, never
    as silent receipt drift.
    """
    return importlib.metadata.version("nbdime")


def _canonical_output(output: Any) -> dict[str, Any]:
    """Reduce one cell output to nbdime's meaningful essence.

    Strips exactly the output-level fields nbdime classifies as transient
    (:data:`_NBDIME_TRANSIENT_OUTPUT_FIELDS` = ``execution_count`` + output
    ``metadata``) and keeps every remaining field verbatim. So identical
    deterministic code yields an identical ``output_sha`` across re-runs.
    """
    return {k: v for k, v in output.items() if k not in _NBDIME_TRANSIENT_OUTPUT_FIELDS}


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


def stamp_canonicalizer(nb: Any) -> dict[str, str]:
    """Record the canonicalizer identity into the executed notebook's metadata.

    Core's ``notebook-record-receipt`` entry model is ``extra="forbid"`` (only
    ``output_sha`` + ``error``), so the ``{canonicalizer, canonicalizer_version}``
    identity that binds each ``output_sha`` cannot ride on the receipt entry. It
    is recorded instead on the render RESULT and here, in the notebook's own
    ``metadata.hpc_audit_canonicalizer`` — the two surfaces the plugin owns. Only
    an EXECUTED render carries it (a non-executed render computes no ``output_sha``
    and stays byte-deterministic). Returns the identity for the result model.
    """
    identity = {"canonicalizer": CANONICALIZER, "canonicalizer_version": canonicalizer_version()}
    nb.metadata["hpc_audit_canonicalizer"] = dict(identity)
    return identity


def write_notebook(nb: Any) -> str:
    """Serialize *nb* to a deterministic ipynb string (trailing newline)."""
    text = str(nbformat.writes(nb, version=nbformat.NO_CONVERT))
    return text if text.endswith("\n") else text + "\n"
