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

This module lives in the ``ops/notebook/`` subject (moved from the ``ops/``
role root by the 2026-07-09 reorg, docs/internals/audit-2026-07-09.md R1): its
only cross-package reads are ``state.audit_source`` and ``state.notebook_audit``,
which are substrate (``hpc_agent.state.*``) always allowed by
``scripts/lint_subject_imports.py`` regardless of the importing file's
location, so subject placement never depended on the role-root lint
short-circuit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.queries.notebook_status import (
    NotebookModuleAttention,
    NotebookSectionStatus,
    NotebookStatusResult,
    NotebookStatusSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.notebook.audit_view import AUDIT_NET_CAP, net_tier_label
from hpc_agent.ops.notebook.canonical import read_recorded_config
from hpc_agent.ops.notebook.module_attention import build_module_attention
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.notebook_audit import (
    REUSED,
    SIGNED_CURRENT,
    SIGNED_STALE,
    ModuleAudit,
    audit_module,
    audit_section,
    record_relay_due,
)

try:  # notebook-audit 6a: builder A owns the audit-net machinery; None until it merges.
    from hpc_agent.ops.notebook.linked_sources import (  # type: ignore[attr-defined]
        resolve_audit_net as _resolve_audit_net,
    )
except ImportError:  # pragma: no cover — exercised by the builder-A merge integration
    _resolve_audit_net = None  # type: ignore[assignment]

__all__ = ["notebook_status"]


def _audit_net_summary(entries: tuple[Any, ...]) -> dict[str, Any]:
    """The additive ``audit_net_summary`` rollup (6a): counts by tier + cap flag.

    Pure reduction over the opaque net entries: one count per tier LABEL
    (:func:`~hpc_agent.ops.notebook.audit_view.net_tier_label` — the machinery's
    own tier names, never re-interpreted) plus ``cap_reached`` — whether the net
    arrived at the BFS cap (the walk stopped; the machinery's disclosure). The
    UNRESOLVED count is the ``audit_net_unresolved`` finding's magnitude; the
    gate flips ``human_required`` on it, this surface only COUNTS.
    """
    by_tier: dict[str, int] = {}
    for entry in entries:
        label = net_tier_label(getattr(entry, "tier", None))
        by_tier[label] = by_tier.get(label, 0) + 1
    return {
        "by_tier": {label: by_tier[label] for label in sorted(by_tier)},
        "cap_reached": len(entries) >= AUDIT_NET_CAP,
    }


def _resolve_net(experiment_dir: Path, source: Any, source_roots: list[str]) -> tuple[Any, ...]:
    """The audit's transitive import closure (6a), fail-open to an empty net.

    Routes through builder A's ONE resolver (``resolve_audit_net`` — the
    deterministic BFS over the audited module's imports under the RECORDED
    ``source_roots``, sorted emission, capped at
    :data:`~hpc_agent.ops.notebook.audit_view.AUDIT_NET_CAP`). Fully fail-open:
    no machinery (the pre-merge seam), no roots, or any resolver error yields an
    EMPTY net — the status rollup degrades to its pre-6a shape, never fails on
    presentation. The call adapts the :func:`resolve_section_engines` signature
    to the whole-module walk: ``(parsed source, experiment_dir, root_dirs)``.
    """
    if _resolve_audit_net is None or not source_roots:
        return ()
    root_dirs = [
        (Path(r) if Path(r).is_absolute() else Path(experiment_dir) / r) for r in source_roots
    ]
    try:
        return tuple(_resolve_audit_net(source, experiment_dir, root_dirs))
    except Exception:  # noqa: BLE001 — the net is presentation; never fail the rollup
        return ()


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

    # WAVE-3 PIECE 3 — the module-attention surface. Resolve the audit's linked src
    # modules under its RECORDED source_roots and charge attention ONCE per UNSIGNED
    # module (never per dependent). The moved-code disclosure (piece 5) matches each
    # unsigned module body against this audit's HUMAN-signed section bodies —
    # ADVISORY only, it clears nothing. The corpus is EVERY human-signed source
    # section (not just required ones): a module is often extracted FROM a helper
    # section the template never declared. Fail-open: no roots → no items.
    records = read_decisions(experiment_dir, "notebook", spec.audit_id)
    signed_section_bodies = {
        s.slug: s.source
        for s in source.sections
        if audit_section(records, s.slug, s.section_sha).status in (SIGNED_CURRENT, REUSED)
    }
    cfg = read_recorded_config(experiment_dir, spec.audit_id)

    # WAVE 6a — the audit NET. Resolve the transitive import closure under the
    # RECORDED source_roots (fail-open: no machinery / no roots / any error →
    # empty net → the pre-6a shape). Its UNRESOLVED entries participate in the
    # module-attention ordering (appended after the unsigned-module charges);
    # the whole net reduces to the additive ``audit_net_summary`` rollup.
    audit_net = _resolve_net(experiment_dir, source, cfg.source_roots)
    module_attention = [
        NotebookModuleAttention(
            module=item.module,
            file=item.file,
            module_sha12=item.module_sha12,
            dependents=list(item.dependents),
            last_signed_sha12=item.last_signed_sha12,
            moved_from_section=item.moved_from_section,
            moved_overlap=list(item.moved_overlap) if item.moved_overlap is not None else None,
        )
        for item in build_module_attention(
            experiment_dir,
            source=source,
            source_roots=cfg.source_roots,
            signed_section_bodies=signed_section_bodies,
            audit_net=audit_net,
        )
    ]

    audit_net_summary = _audit_net_summary(audit_net)
    result_kwargs: dict[str, Any] = {
        "audit_id": spec.audit_id,
        "sections": sections,
        "passed": module_audit.passed,
        "module_attention": module_attention,
    }
    # Cross-lane seam (notebook-audit 6a): builder A adds ``audit_net_summary``
    # to the wire result when the machinery lands; pre-merge the model rejects
    # the unknown field (extra=forbid), so it is attached ONLY once the field
    # exists. Post-merge this guard always takes the attach branch.
    if "audit_net_summary" in NotebookStatusResult.model_fields:
        result_kwargs["audit_net_summary"] = audit_net_summary
    return NotebookStatusResult(**result_kwargs)
