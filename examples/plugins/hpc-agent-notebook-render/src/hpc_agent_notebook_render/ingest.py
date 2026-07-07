"""``notebook-ingest-signoffs`` — the SECOND-CONFORMING-HARNESS ceiling.

Role (b) of the export. A human typing into a rendered sign-off cell is
out-of-band from the LLM, so this verb makes the full audit loop work with NO
Claude Code anywhere: render -> human types in Jupyter -> ingest -> full-strength
tier. For each typed sign-off it (1) writes the raw text through the documented
utterance-log write API (``state/utterances.py::append_utterance`` — honoring
no-scaffold + fail-open) and (2) appends the sign-off via the CORE append-decision
path (``scope_kind='notebook'``, ``block='notebook-sign-off'``), recomputing
``section_sha`` / ``view_sha`` from the CURRENT source + template so the T8 gate
enforces recompute + authorship. A gate refusal for one section is reported
per-section, never fatal to the batch.
"""

from __future__ import annotations

import re
from pathlib import Path

import nbformat

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.cli._dispatch import CliShape
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.ops.notebook.audit_view import build_audit_view
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.utterances import append_utterance

from . import _annotate
from ._models import (
    IngestedSignoff,
    NotebookIngestSignoffsResult,
    NotebookIngestSignoffsSpec,
    RefusedSignoff,
)

__all__ = ["notebook_ingest_signoffs"]

_PRIMITIVE = "notebook-ingest-signoffs"

# A typed sign-off that OPENS with a harness-injection tag is refused: it is not
# human-typed but harness/agent-influenced (the write-API provenance clause). The
# same tag set as core's _kernel/hooks/utterance_capture._HARNESS_INJECTION_RE,
# re-derived here so the plugin never imports a private core hook symbol.
_HARNESS_INJECTION_RE = re.compile(
    r"^\s*<(?:task-notification|system-reminder|local-command-caveat|"
    r"command-name|command-message|local-command-stdout)\b"
)


def _read_rel(experiment_dir: Path, relpath: str, *, what: str) -> str:
    path = Path(relpath)
    if not path.is_absolute():
        path = experiment_dir / path
    if not path.is_file():
        raise errors.SpecInvalid(f"notebook-ingest-signoffs {what} file not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise errors.SpecInvalid(
            f"notebook-ingest-signoffs {what} file unreadable: {path} ({exc})"
        ) from exc


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "<journal home>/<repo_hash>/utterances.jsonl"),
        SideEffect("file_write", "<experiment>/.hpc/notebooks/<audit_id>.decisions.jsonl"),
    ],
    error_codes=[errors.SpecInvalid],
    # Append-only sign-off records + utterance-log writes; not byte-idempotent.
    idempotent=False,
    cli=CliShape(
        help=(
            "Ingest human sign-offs typed into a rendered notebook's sign-off cells "
            "(the second conforming harness). For each typed sign-off: write the raw "
            "text to the out-of-band utterance log (no-scaffold + fail-open) and "
            "append a notebook-sign-off via the core append-decision path, "
            "recomputing section_sha/view_sha from the CURRENT source so the T8 gate "
            "enforces recompute + authorship. Unchanged scaffolds are skipped; a "
            "gate refusal is reported per-section, never fatal. Ships no JSON schema: "
            "the Pydantic spec model validates at the seam."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=NotebookIngestSignoffsSpec,
    ),
    agent_facing=True,
)
def notebook_ingest_signoffs(
    *, experiment_dir: Path, spec: NotebookIngestSignoffsSpec
) -> NotebookIngestSignoffsResult:
    """Ingest typed sign-offs from *spec.notebook_path* into the notebook journal.

    Raises :class:`errors.SpecInvalid` only on an unresolvable source / template /
    notebook (broken setup); a per-section gate refusal is REPORTED, not raised.
    """
    experiment_dir = Path(experiment_dir)
    source_text = _read_rel(experiment_dir, spec.source, what="source")
    template_text = _read_rel(experiment_dir, spec.template, what="template")
    nb_text = _read_rel(experiment_dir, spec.notebook_path, what="notebook")

    source = parse_percent_source(source_text)
    template = parse_percent_source(template_text)
    view = build_audit_view(source, template, ())
    sha_by_slug = {sect.slug: sect.section_sha for sect in source.sections}
    view_by_slug = {sv.slug: sv.view_sha for sv in view.sections}

    try:
        notebook = nbformat.reads(nb_text, as_version=4)
    except Exception as exc:  # noqa: BLE001 — nbformat raises varied errors
        raise errors.SpecInvalid(
            f"notebook-ingest-signoffs: {spec.notebook_path} is not a readable .ipynb ({exc})"
        ) from exc

    ingested: list[IngestedSignoff] = []
    refused: list[RefusedSignoff] = []
    skipped_empty: list[str] = []
    utterance_written = False

    for cell in notebook.get("cells", []):
        cell_source = cell.get("source", "")
        if isinstance(cell_source, list):
            cell_source = "".join(cell_source)
        marker = _annotate.SIGNOFF_MARKER_RE.search(cell_source)
        if marker is None:
            continue
        slug = marker.group("slug")
        typed = _annotate.extract_typed_signoff(cell_source)
        if typed is None:
            skipped_empty.append(slug)
            continue
        if _HARNESS_INJECTION_RE.match(typed):
            refused.append(RefusedSignoff(section=slug, reason="harness-injection-text"))
            continue

        # (1) Out-of-band utterance-log write (full-strength authorship channel).
        # Honors no-scaffold: a missing namespace returns None (degraded tier,
        # reported honestly via utterance_log below).
        if append_utterance(experiment_dir, typed) is not None:
            utterance_written = True

        # (2) The sign-off, through the CORE append-decision path. section_sha /
        # view_sha recomputed from the CURRENT source; source/template ride resolved
        # so the T8 gate can recompute even with no interview.json.
        section_sha = sha_by_slug.get(slug)
        view_sha = view_by_slug.get(slug)
        if section_sha is None or view_sha is None:
            refused.append(RefusedSignoff(section=slug, reason="section-not-in-current-source"))
            continue
        resolved = {
            "audit_id": spec.audit_id,
            "section": slug,
            "section_sha": section_sha,
            "view_sha": view_sha,
            "source": spec.source,
            "template": spec.template,
        }
        try:
            append_decision(
                experiment_dir=experiment_dir,
                spec=AppendDecisionInput(
                    scope_kind="notebook",
                    scope_id=spec.audit_id,
                    block="notebook-sign-off",
                    response=typed,
                    resolved=resolved,
                ),
            )
        except errors.SpecInvalid as exc:
            refused.append(RefusedSignoff(section=slug, reason=str(exc)))
            continue
        ingested.append(IngestedSignoff(section=slug, section_sha=section_sha, view_sha=view_sha))

    return NotebookIngestSignoffsResult(
        audit_id=spec.audit_id,
        ingested=ingested,
        refused=refused,
        skipped_empty=skipped_empty,
        utterance_log="written" if utterance_written else "absent-namespace",
    )
