"""``notebook-status`` — read the per-section audit state of an audited source.

A read-only ``query`` primitive (notebook-audit T6). Given an experiment dir, a
source ``.py`` relpath, a template ``.py`` relpath, and an ``audit_id``, it:

1. parses the source + template as jupytext percent-format modules
   (:func:`hpc_agent.state.audit_source.parse_percent_source`) — the template's
   section slugs are the REQUIRED inventory;
2. replays the ``audit_id`` decision journal and reduces every required section
   to a T6 status via :mod:`hpc_agent.state.notebook_audit` (whose drift verdict
   routes through the ONE attestation kernel);
3. returns per-section ``{slug, status, current/signed section_sha, view_sha,
   attestor}`` plus the whole-module ``passed`` gate predicate.

Local read, plus ONE narrow write: when the reduction lands on a TERMINAL audit
state — ``passed`` (the gate predicate holds) or ``failed`` (a human sign-off
was drift-revoked: some section reads ``signed_stale``) — the op journals a
relay-due marker (:func:`hpc_agent.state.notebook_audit.record_relay_due`) so
the relay-audit Stop hook can enforce that the verdict actually reaches the
human (the omission gate; ``verify-relay`` covers only distortion). Non-terminal
runs (the ordinary in-loop ``unsigned`` mix) write NOTHING — the narrow set is
deliberate (D8 applied to gates: marking everything relay-due recreates alarm
fatigue inside the enforcement). No SSH, no scheduler. The status itself stays
derived state, recomputed from the ``.py`` on disk + the journal on every call,
so it can never drift from a second source of truth; the marker write is
deduplicated on (state, module sha12), so recomputing the same terminal fact
appends nothing new (idempotent by construction).

This file lives at the ``ops/`` *role root* (sibling to ``export_dossier.py`` /
``trace.py``, NOT inside ``ops/notebook/``) because it reads across subjects —
the ``state.audit_source`` section model and the ``state.notebook_audit``
reduction over the decision journal. The subject-imports lint short-circuits for
role-root files, so the cross-subject reads here are allowed by construction.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.queries.notebook_status import (
    NotebookSectionStatus,
    NotebookStatusResult,
    NotebookStatusSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.notebook_audit import (
    SIGNED_STALE,
    ModuleAudit,
    audit_module,
    record_relay_due,
)

__all__ = ["notebook_status"]


def _read_percent_module(experiment_dir: Path, relpath: str, label: str) -> str:
    """Read an experiment-relative ``.py`` source, or raise a naming SpecInvalid.

    A missing file is a caller-input error (the relpath is wrong), so it surfaces
    as :class:`errors.SpecInvalid` naming which of source/template was absent —
    not a bare ``FileNotFoundError`` the envelope would classify as internal.
    """
    path = (Path(experiment_dir) / relpath).resolve()
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(
            f"notebook-status: {label} not found at {relpath!r} (resolved {path})"
        ) from exc
    except OSError as exc:
        raise errors.SpecInvalid(
            f"notebook-status: {label} at {relpath!r} could not be read: {exc}"
        ) from exc


def _terminal_state(module_audit: ModuleAudit) -> str | None:
    """The TERMINAL relay-due state of a rollup, or ``None`` (non-terminal).

    * ``"passed"`` — the gate predicate holds (every required section current):
      the verdict the human is waiting on (tonight's proving-run omission).
    * ``"failed"`` — some section reads ``signed_stale``: a human sign-off was
      REVOKED by drift, a verdict about their own attention they must hear.
    * ``None`` — the ordinary in-loop mix (``unsigned`` sections still being
      drafted/signed) is NOT terminal and sets no marker: the narrow set is
      deliberate (marking every in-progress reduction relay-due recreates alarm
      fatigue inside the enforcement itself).
    """
    if module_audit.passed:
        return "passed"
    if any(s.status == SIGNED_STALE for s in module_audit.sections):
        return "failed"
    return None


@primitive(
    name="notebook-status",
    verb="query",
    side_effects=[
        SideEffect(
            "file_write",
            "<experiment>/.hpc/notebooks/<audit_id>.decisions.jsonl (relay-due "
            "marker, TERMINAL states only)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Report the per-section audit state of an audited source .py against "
            "its template inventory and audit_id journal. No SSH. Each required "
            "(template) section reduces to signed_current / auto_cleared / "
            "signed_stale / unsigned (drift-revoked by construction); `passed` "
            "is the graduation gate's whole-module predicate (every section "
            "current). Section shas are recomputed from the .py on every call. "
            "A TERMINAL verdict (passed, or failed via a drift-revoked sign-off) "
            "journals a relay-due marker the Stop hook discharges only when the "
            "verdict is actually relayed (deduplicated; non-terminal runs write "
            "nothing)."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=NotebookStatusSpec,
        schema_ref=SchemaRef(input="notebook_status"),
    ),
    agent_facing=True,
)
def notebook_status(*, experiment_dir: Path, spec: NotebookStatusSpec) -> NotebookStatusResult:
    """Reduce every required section of an audited source to its T6 audit status.

    Parses the source + template ``.py`` (percent format), takes the template's
    section slugs as the required inventory, and reduces each against its CURRENT
    source sha and the ``audit_id`` journal. A required template section absent
    from the source reads ``unsigned`` (nothing to sign). ``passed`` is true iff
    every required section is current (``signed_current`` or ``auto_cleared``).

    Idempotent by construction: derived state recomputed from the ``.py`` + the
    journal on every call; the one write (the relay-due marker on a TERMINAL
    ``passed``/``failed`` verdict — the omission gate's obligation record) is
    deduplicated on the (state, module sha12) key tokens, so recomputing the
    same terminal fact appends nothing. A terminal verdict requires journaled
    attestations, so the marker write never scaffolds a journal that does not
    already exist (the no-scaffold posture of a pure read is preserved for
    non-terminal runs).

    Raises :class:`errors.SpecInvalid` on an unreadable source/template path or a
    malformed percent-format module (bad/duplicate/misplaced section marker — the
    parser's own boundary guards).
    """
    experiment_dir = Path(experiment_dir)
    source = parse_percent_source(_read_percent_module(experiment_dir, spec.source, "source"))
    template = parse_percent_source(_read_percent_module(experiment_dir, spec.template, "template"))

    module_audit = audit_module(
        experiment_dir,
        spec.audit_id,
        source=source,
        required_slugs=template.slugs,
    )
    terminal = _terminal_state(module_audit)
    if terminal is not None:
        # The omission gate's obligation record: key tokens are the state word
        # and the module sha12 — either appearing in the final assistant text
        # discharges the marker (the Stop hook's substring check).
        record_relay_due(
            experiment_dir,
            audit_id=spec.audit_id,
            state=terminal,
            module_sha=source.module_sha,
        )
    sections = [
        NotebookSectionStatus(
            slug=a.slug,
            status=a.status,
            current_section_sha=a.current_section_sha,
            signed_section_sha=a.signed_section_sha,
            view_sha=a.view_sha,
            attestor=a.attestor,
        )
        for a in module_audit.sections
    ]
    return NotebookStatusResult(
        audit_id=spec.audit_id,
        sections=sections,
        passed=module_audit.passed,
    )
