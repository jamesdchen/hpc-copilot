"""Pydantic models for the ``notebook-draft`` mutate verb (multi-human MH5).

Wire surface over :mod:`hpc_agent.ops.notebook.draft_op` — the drafter-attribution
seam. A draft attestation records "the actor whose SESSION drove the drafting of
this section", recorded at DRAFT time by the drafting session (the LLM is
transport; the session-owner is the author, exactly as in the utterance log). The
reviewer!=author gate (MH6) resolves the section author from the newest draft
attestation fresh at the current section sha.

**No actor field on the wire (the enforcement row).** The spec carries
``{audit_id, source, section}`` and NOTHING that names an actor: the drafting
actor is resolved SERVER-SIDE from the session env (``HPC_ACTOR`` via
:func:`hpc_agent.infra.env_flags.env_actor`), never caller-asserted. An
agent-suppliable actor field would let the model choose its own identity — the
actor must arrive from outside the model's tool surface, exactly like the
utterance text it attributes. The resolved actor is ECHOED on the RESULT
(``actor``) for transparency; it is a server-computed output, never an input.

**The parse IS the recompute.** The verb parses the source ``.py`` ON DISK and
binds the draft to the FRESHLY-PARSED section sha (never a caller-asserted sha),
so a draft can only ever be recorded against the source as it currently sits on
disk, and the old draft reads stale the moment the section is redrafted
(``docs/design/multi-human.md`` MH5).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class NotebookDraftSpec(BaseModel):
    """Inputs to ``notebook-draft`` — the drafter-attribution seam.

    Deliberately carries NO actor field (the enforcement row: the actor is
    resolved server-side from the session env, never on the wire). ``audit_id``
    is the notebook decision-journal scope; ``source`` is the audited source
    ``.py`` relpath (parsed on disk — the parse IS the recompute the draft binds
    against); ``section`` is the slug being attributed (must exist in the parsed
    source, else a loud refusal — there is no sha to bind a draft against).
    """

    model_config = ConfigDict(extra="forbid", title="notebook-draft input spec")

    audit_id: str = Field(
        min_length=1,
        description=(
            "The notebook decision-journal scope id (filesystem-safe slug) the "
            "draft attestation is appended to (journal at "
            ".hpc/notebooks/<audit_id>.decisions.jsonl). Caller-authored."
        ),
    )
    source: str = Field(
        min_length=1,
        description=(
            "Experiment-relative path to the audited source .py (jupytext percent "
            "format). Parsed on disk; the draft binds the FRESHLY PARSED section "
            "sha — a draft can only be recorded against current source."
        ),
    )
    section: str = Field(
        min_length=1,
        description=(
            "The section slug being attributed. Must exist in the parsed source "
            "(else a loud spec_invalid — there is no section sha to bind a draft "
            "against). One draft attestation is journaled for it."
        ),
    )


class NotebookDraftResult(BaseModel):
    """Echo of the journaled draft attestation.

    ``section_sha`` is the freshly-parsed sha the draft was bound at; ``actor`` is
    the SERVER-RESOLVED drafting actor (the session ``HPC_ACTOR`` when it is a
    declared actor, else ``null`` for an unattributed draft — zero/one declared
    actor). It is a server-computed OUTPUT, echoed for transparency; the wire
    input never carries an actor.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-draft output data")

    audit_id: str
    section: str
    section_sha: str
    actor: str | None = Field(
        default=None,
        description=(
            "The server-resolved drafting actor stamped as the attestation's "
            "attestor_id (opaque, harness-asserted, never verified), or null for "
            "an unattributed draft (zero/one declared actor). Server-computed; "
            "never a wire input."
        ),
    )
