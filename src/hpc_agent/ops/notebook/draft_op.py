"""``notebook-draft`` — journal a CODE draft attestation, attributed to the session.

The drafter-attribution seam (multi-human MH5, ``docs/design/multi-human.md``).
Reviewer!=author (MH6) needs the section's AUTHOR, and the author of an
LLM-drafted section is settled honestly as **the actor whose session drove the
drafting** — the LLM is transport, the session-owner is the author (the model has
no standing of its own, exactly as it has none in the utterance log). This
``mutate`` verb records that at DRAFT time: given ``{audit_id, source, section}``
it parses the source ``.py`` ON DISK, recomputes the named section's sha (the
parse IS the recompute — never a caller-asserted sha), resolves the SESSION ACTOR
server-side, and journals a CODE draft attestation bound to the section sha via
:func:`hpc_agent.state.notebook_audit.record_draft`.

**No actor field on the wire (the enforcement row).** The actor is resolved from
the session env (``HPC_ACTOR`` via :func:`hpc_agent.infra.env_flags.env_actor`),
validated against the interview's declared ``actors.ids``, and never
caller-asserted — an agent-suppliable actor would let the model choose its own
identity. The three resolution outcomes:

* **>1 declared actor, no session actor resolves → loud refusal.** An anonymous
  act in a declared-multi-actor experiment is the laundering channel (draft as
  nobody, then self-sign undetectably), so the dangling-reference/loud posture
  applies, not D7 silence.
* **zero/one declared actor → records with the resolved actor or ``None``.** A
  draft still lands; ``attestor_id`` is the resolved actor when the session is
  attributed and it is a declared id, else ``None`` (comparisons stay off,
  byte-identical to today's single-actor world). Zero declared actors always
  records ``attestor_id=None`` — there is nothing to attribute against.

Properties by construction (MH5): a redraft moves the sha, so the old draft reads
STALE via the ONE reducer and authorship follows the CURRENT content, with no
state machine. The record is fabrication-resistant where it matters — the sha is
server-recomputed and the actor is harness-asserted from outside the model's tool
surface.

This file lives inside the ``notebook`` subject (beside the draft record writer it
calls), reaching only same-subject ``ops.notebook.*``, the ``state.*`` substrate,
and ``infra.env_flags`` (the session-actor reader) — the subject-imports lint is
satisfied by construction.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.notebook_draft import NotebookDraftResult, NotebookDraftSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef

# NOTE: env_actor + notebook_audit.record_draft are multi-human branch symbols; a
# mypy env pinned to the pre-multi-human package flags them (installed-pkg skew),
# hence the narrow ``type: ignore[attr-defined]`` on the import + the record_draft
# call — both resolve cleanly against the worktree src.
from hpc_agent.infra.env_flags import env_actor  # type: ignore[attr-defined]
from hpc_agent.state import notebook_audit
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.interview_doc import iter_interview_docs

__all__ = ["notebook_draft"]

_PRIMITIVE = "notebook-draft"


def _read_source_file(experiment_dir: Path, relpath: str) -> str:
    """Read the caller-declared source ``.py``, or raise SpecInvalid.

    A missing/unreadable file points at a file that is not there — a malformed
    spec, NOT a section that fails to record. Loud, matching the sibling notebook
    verbs' refusal wording.
    """
    path = Path(relpath)
    if not path.is_absolute():
        path = Path(experiment_dir) / path
    if not path.is_file():
        raise errors.SpecInvalid(f"notebook-draft source file not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise errors.SpecInvalid(
            f"notebook-draft source file could not be read: {path} ({exc})"
        ) from exc


def _declared_actor_ids(experiment_dir: Path) -> list[str]:
    """The interview's declared ``actors.ids`` slugs, or ``[]`` when unopted-in.

    Mirrors the canonical-config reader's file posture
    (:func:`hpc_agent.ops.notebook.canonical.read_interview_audited_source`): the
    campaign-dir root ``interview.json`` first, ``.hpc/interview.json`` accepted
    defensively; a corrupt / non-object file, an absent ``actors`` block, or a
    non-list ``ids`` all read as ZERO declared actors (today's single-actor world,
    the D7 fail-safe — never an error). The count is what turns the >1-actor
    comparisons on; the ids are what a resolved session actor is validated against.
    """
    for doc in iter_interview_docs(experiment_dir):
        actors = doc.get("actors")
        if isinstance(actors, dict):
            ids = actors.get("ids")
            if isinstance(ids, list):
                return [str(i) for i in ids]
        return []
    return []


def _resolve_session_actor(declared_ids: list[str]) -> str | None:
    """The session actor (``HPC_ACTOR``) when it is a DECLARED actor, else ``None``.

    The gate-side resolver (MH4's posture): read the harness-asserted slug, and
    return it ONLY when it is one of the declared ``actors.ids`` — an unset,
    invalid, or UNDECLARED slug resolves to ``None`` (which, under >1 declared
    actors, is the loud-refusal trigger: an undeclared actor may not draft). Never
    verifies who set the env — the attribution tier is harness-asserted, always.
    """
    actor: str | None = env_actor()
    if actor is not None and actor in declared_ids:
        return actor
    return None


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "<experiment>/.hpc/notebooks/<audit_id>.decisions.jsonl"),
    ],
    error_codes=[errors.SpecInvalid],
    # Append-only: each call journals a fresh draft attestation. A re-draft at an
    # unchanged sha appends a new line (the newest valid draft wins on read), so
    # retries are safe but not byte-idempotent — like append-decision.
    idempotent=False,
    cli=CliShape(
        help=(
            "Journal a CODE draft attestation for one section of an audited source "
            ".py — the drafter-attribution seam (multi-human). Records the actor "
            "whose SESSION drove the drafting as the section's author, at DRAFT "
            "time (the reviewer!=author gate resolves it). Parses the source ON "
            "DISK and binds the draft to the freshly-parsed section sha (a draft "
            "can only be recorded against current source, and the old draft reads "
            "stale the moment the section is redrafted). The actor is resolved "
            "SERVER-SIDE from the session env (HPC_ACTOR) and validated against "
            "the interview's declared actors.ids — NEVER a wire field (an "
            "agent-suppliable actor would let the model choose its identity). "
            "Refuses when >1 actor is declared and no session actor resolves (an "
            "anonymous act is the laundering channel); records attestor_id=None "
            "when zero/one actor is declared. Pure local read + journal append, "
            "no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=NotebookDraftSpec,
        schema_ref=SchemaRef(input="notebook_draft"),
    ),
    agent_facing=True,
)
def notebook_draft(*, experiment_dir: Path, spec: NotebookDraftSpec) -> NotebookDraftResult:
    """Journal a CODE draft attestation for *spec.section*, attributed to the session.

    Parses *spec.source* on disk, recomputes the named section's sha, resolves the
    session actor server-side, and appends a ``notebook-draft`` attestation bound
    to the freshly-recomputed section sha.

    Raises :class:`errors.SpecInvalid` on an unreadable source path, a malformed
    percent-format module, a *section* slug absent from the parsed source (no sha
    to bind against), or — when >1 actor is declared — a session with no
    resolvable declared actor (the loud multi-actor refusal).
    """
    experiment_dir = Path(experiment_dir)
    source = parse_percent_source(_read_source_file(experiment_dir, spec.source))
    by_slug = {sect.slug: sect for sect in source.sections}
    section = by_slug.get(spec.section)
    if section is None:
        raise errors.SpecInvalid(
            f"notebook-draft: section {spec.section!r} not found in source "
            f"{spec.source!r} — a draft can only be recorded against a section that "
            f"exists in the parsed source (its slugs: {sorted(by_slug)!r})."
        )

    declared = _declared_actor_ids(experiment_dir)
    # Census-null (A9, RULED 2026-07-12): the attestor stamp is written ONLY
    # when >1 actor is declared — the same zero/one-actor byte-identity floor
    # every decision record holds, so a sole-actor session's draft record
    # never forks its bytes on whether HPC_ACTOR happened to be exported.
    actor = _resolve_session_actor(declared) if len(declared) > 1 else None
    if len(declared) > 1 and actor is None:
        raise errors.SpecInvalid(
            "notebook-draft: this experiment declares more than one actor "
            f"({sorted(declared)!r}) but no session actor resolves — set HPC_ACTOR "
            "to one of the declared actors before drafting. An anonymous draft in a "
            "declared-multi-actor experiment is the laundering channel (draft as "
            "nobody, then self-review undetectably), so it is refused, not skipped."
        )

    # Bind + append. recompute wired to the freshly-parsed sha (the parse IS the
    # recompute — server-computed, never caller-supplied). actor is harness-asserted
    # (HPC_ACTOR), never on the wire; None records an unattributed draft.
    notebook_audit.record_draft(  # type: ignore[attr-defined]
        experiment_dir,
        audit_id=spec.audit_id,
        section=spec.section,
        section_sha=section.section_sha,
        recompute=section.section_sha,
        actor=actor,
    )

    return NotebookDraftResult(
        audit_id=spec.audit_id,
        section=spec.section,
        section_sha=section.section_sha,
        actor=actor,
    )
