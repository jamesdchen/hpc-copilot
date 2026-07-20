"""The CANONICAL audit view — one server-computed definition, three call sites.

The full-view-recompute upgrade (user-approved 2026-07-07, "solve it properly"):
the T8 sign-off gate must not merely validate a resolved ``view_sha`` as PRESENT —
it must RECOMPUTE it and refuse a mismatch. Recomputing ``view_sha`` needs three
ingredients: the source + template ``.py`` on disk, the LINT findings, and the
JOURNALED render receipts. Post-T10 the receipts are journaled; the lint is cheap
local static analysis; the only thing that was ever missing is the audit's
CONFIGURATION — the ``input_roots`` / ``source_roots`` / ``attention_order`` the
audit was run with. Once that config is persisted on ``interview.json``'s
``audited_source`` block (the `_AuditedSource` grew those fields), the view is
fully recomputable server-side.

This module is that ONE definition. :func:`build_canonical_view` parses the
source + template from disk, runs the ``notebook-lint`` rules in-process with the
RECORDED roots (the ``notebook-auto-clear`` un-fakeability precedent — never trust
caller findings), reads the JOURNALED render receipts (fresh entries only), and
builds the D-attention view with the recorded ``attention_order``. Everything is
server-computed; zero caller-supplied findings or receipts enter. The gate, the
``notebook-audit-view`` / ``notebook-auto-clear`` verbs, and the render plugin all
route through THIS function, so their view shas agree by construction (pinned by
``inspect.getsource`` in the enforcement map).

Lives inside the ``notebook`` subject (beside the lint it recomputes and the view
builder it wraps), reaching only same-subject ``ops.notebook.*`` and the
``state.*`` substrate — the subject-imports lint is satisfied by construction. The
``decision`` subject reaches it through the top-level ``ops/notebook_view.py``
facade (the ``field_ownership`` precedent).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hpc_agent._wire.actions.notebook_lint import NotebookLintInput
from hpc_agent.ops.notebook.audit_view import AuditView, build_audit_view
from hpc_agent.ops.notebook.lint import notebook_lint
from hpc_agent.state import notebook_audit
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.data_trace import read_trace
from hpc_agent.state.interview_doc import iter_interview_docs

__all__ = [
    "AuditConfig",
    "read_interview_audited_source",
    "read_recorded_config",
    "build_canonical_view",
]


@dataclass(frozen=True)
class AuditConfig:
    """The CANONICAL audit configuration — the per-invocation ephemera that was
    never persisted before the full-view-recompute upgrade.

    * ``input_roots`` — the opaque data-path roots the executes-live lint tests
      path literals against.
    * ``source_roots`` — the opaque import roots the linked-sources lint resolves
      imports under.
    * ``output_roots`` — the opaque WRITE-target roots: a path literal under one
      is a declared output, exempt from the executes-live not-exists flag (the
      run-#10 output-literal noise fix).
    * ``attention_order`` — the presented section ordering (``None`` = source
      order). It feeds the MODULE roll-up view_sha only; per-section view shas are
      unaffected.
    * ``observables`` — the OBSERVATION PLAN (A14, G-a ruled): the opaque
      declared-observable names the sanctioned runner (the notebook-render
      plugin's between-cell loop, T-R) looks up in the exec namespace and measures
      into runner-tier trace records. ``None`` = no observation plan (the loop is
      OFF; execution is byte-identical — D7). Read here only; core never observes.

    Equality is by value (two configs with equal roots + order are equal), which
    the ``notebook-audit-view`` verb uses to decide whether a produced view is
    CANONICAL (matches the recorded config) or a PREVIEW (an override).
    """

    input_roots: list[str] = field(default_factory=list)
    source_roots: list[str] = field(default_factory=list)
    attention_order: list[str] | None = None
    output_roots: list[str] = field(default_factory=list)
    observables: list[str] | None = None


def read_interview_audited_source(experiment_dir: Path, audit_id: str | None) -> dict | None:
    """The interview.json ``audited_source`` block matching *audit_id*, or ``None``.

    Mirrors the gate's / graduation gate's posture: the canonical location is the
    campaign-dir root, ``.hpc/interview.json`` accepted defensively; a corrupt /
    non-object file reads as absent. Matched by ``audit_id`` when one is given so a
    stray block never supplies another audit's config; ``audit_id=None`` takes the
    first block found (the graduation-gate convention). Public so the
    ``notebook-record-config`` verb can refuse when the opt-in path already owns
    the config (one source of truth — never a second reader of the file format).
    """
    for doc in iter_interview_docs(experiment_dir):
        block = doc.get("audited_source")
        if isinstance(block, dict) and (audit_id is None or block.get("audit_id") == audit_id):
            return block
    return None


def _coerce_roots(value: object) -> list[str]:
    """A persisted roots field → ``list[str]`` (absent / None / malformed → [])."""
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def read_recorded_config(experiment_dir: Path, audit_id: str | None) -> AuditConfig:
    """The RECORDED audit configuration, from either seat that can hold one.

    Precedence (the run-#10 standalone-audit fix):

    1. interview.json's ``audited_source`` block matching *audit_id* WINS when
       present — the opt-in path owns the config (even a block predating the
       config fields wins, yielding the conservative defaults: byte-compatible
       with every pre-upgrade record).
    2. Else the JOURNALED ``notebook-audit-config`` record the standalone
       ``notebook-record-config`` verb appends
       (:func:`hpc_agent.state.notebook_audit.read_audit_config`).
    3. Else the conservative defaults — empty roots, source-order presentation —
       exactly the posture the gate used before any config was persisted.

    Pure local read, no SSH. ``audit_id=None`` (the graduation-gate convention)
    reads the interview seat only — a journal record is per-``audit_id`` by
    construction, so there is nothing to fall back to.
    """
    block = read_interview_audited_source(experiment_dir, audit_id)
    if block is not None:
        return _config_from_record(block)
    if audit_id is not None:
        journaled = notebook_audit.read_audit_config(experiment_dir, audit_id)
        if journaled is not None:
            return _config_from_record(journaled)
    return AuditConfig()


def _config_from_record(block: dict) -> AuditConfig:
    """Coerce a persisted config mapping (interview block / journal record) to
    an :class:`AuditConfig` — absent / malformed fields → conservative defaults."""
    order = block.get("attention_order")
    observables = block.get("observables")
    return AuditConfig(
        input_roots=_coerce_roots(block.get("input_roots")),
        source_roots=_coerce_roots(block.get("source_roots")),
        attention_order=[str(s) for s in order] if isinstance(order, list) else None,
        output_roots=_coerce_roots(block.get("output_roots")),
        observables=[str(s) for s in observables] if isinstance(observables, list) else None,
    )


def _read_py(experiment_dir: Path, relpath: str) -> str:
    """Read a source/template ``.py`` (relative → experiment_dir); raise on missing.

    The one loud failure a recompute makes: a view that cannot be rebuilt from the
    ``.py`` on disk is refused, never silently skipped (the T8 unresolvable-source
    posture). ``notebook-lint`` re-reads the same files independently.
    """
    from hpc_agent import errors

    path = Path(relpath)
    if not path.is_absolute():
        path = experiment_dir / path
    if not path.is_file():
        raise errors.SpecInvalid(f"canonical audit view: .py not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise errors.SpecInvalid(
            f"canonical audit view: .py could not be read: {path} ({exc})"
        ) from exc


def build_canonical_view(
    experiment_dir: Path,
    *,
    audit_id: str,
    source_relpath: str,
    template_relpath: str,
    cfg: AuditConfig,
) -> AuditView:
    """Build the CANONICAL :class:`AuditView` — everything server-computed.

    The one definition every sign-off view_sha is recomputed against:

    1. parse the source + template ``.py`` from disk (loud on a missing file);
    2. RECOMPUTE the lint findings in-process by calling the ``notebook-lint``
       primitive with the RECORDED roots (never a caller-supplied finding — the
       auto-clear un-fakeability precedent);
    3. read the JOURNALED render receipts and feed only the entries still FRESH at
       the current section sha (a drifted receipt greens nothing);
    4. build the D-attention view with the recorded ``attention_order``.

    Returns the :class:`AuditView`; writes nothing (the render-store write stays in
    the ``notebook-audit-view`` verb). Pure of caller trust — a caller who moved a
    data path, or whose findings disagree, gets a different view_sha and the gate
    refuses.
    """
    source = parse_percent_source(_read_py(experiment_dir, source_relpath))
    template = parse_percent_source(_read_py(experiment_dir, template_relpath))

    lint_result = notebook_lint(
        experiment_dir=experiment_dir,
        spec=NotebookLintInput(
            source=source_relpath,
            template=template_relpath,
            input_roots=cfg.input_roots,
            source_roots=cfg.source_roots,
            output_roots=cfg.output_roots,
        ),
    )
    findings = [f.model_dump() for f in lint_result.findings]

    current_shas = {sect.slug: sect.section_sha for sect in source.sections}
    journaled = notebook_audit.read_render_receipts(
        experiment_dir, audit_id, current_shas=current_shas
    )
    receipt = {slug: entry for slug, entry in journaled.items() if entry["fresh"]}

    # Preview-wiring (R1/R2): the journaled SAMPLED preview receipts — the DISTINCT,
    # WEAKER evidence basis the view DISCLOSES (presentation-only: never a tier /
    # trust input, never in view_sha — R3). Passed UNFILTERED (unlike the full
    # receipts above) so a preview the section outlived is still disclosed
    # honestly (``fresh: False``); basis-greening enforces freshness itself via
    # the sha-check in ``_assertions_green``.
    preview_receipt = notebook_audit.read_preview_receipts(
        experiment_dir, audit_id, current_shas=current_shas
    )

    # The section join (A16 B3-LEAN): the audit-scope runner-observed trace is
    # part of what the sign-off view shows and its view_sha binds. Read here so
    # the gate, the view verb, and the render plugin all recompute the SAME
    # runtime summary (the one-definition guarantee). A tolerant read — absent
    # trace → [] → no summary → byte-identical to a pre-join view.
    audit_traces = read_trace(experiment_dir, "audit", audit_id, 0)

    return build_audit_view(
        source,
        template,
        findings,
        receipt=receipt,
        preview_receipt=preview_receipt,
        attention_order=cfg.attention_order,
        audit_traces=audit_traces,
    )
