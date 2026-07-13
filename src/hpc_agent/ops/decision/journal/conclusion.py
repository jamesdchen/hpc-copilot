"""The conclusion authorship gate (E-shape, T8) — evidence memory's one new
attested record, its three locks, and the revoke floor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput

from ._shared import (
    _conclusion_dossier_resolver,
    _fresh_authored_text,
    _is_bare_ack,
    _names_citation_sha_prefix,
    _names_slug,
    _refuse_missing_authorship,
    _registration_authored_text,
    _target_record_ts,
)


def _assert_conclusion_revoke_floor(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """The E-shape revoke floor (the R7 form) for a ``conclusion-revoke`` record.

    A human withdrawal: non-bare, NAMES the ``conclusion_id``, and its free-text
    ``reason`` is MANDATORY. It binds no new sha (it withdraws — a conclusion is
    dated evidence, never re-verified at withdrawal), so there is NO recompute
    leg; but it is journaled, attributed, and permanent like everything else. The
    bare-ack and id-naming refusals carry the E2 authorship marker (a freshly
    typed human rationale resolves them); the missing-reason refusal is structural
    (the agent must add ``resolved['reason']``), left UNMARKED.
    """
    conclusion_id = resolved.get("conclusion_id")
    if not isinstance(conclusion_id, str) or not conclusion_id:
        raise errors.SpecInvalid(
            "conclusion-revoke gate: resolved must name a non-empty conclusion_id "
            "(the finding being withdrawn)."
        )
    reason = resolved.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise errors.SpecInvalid(
            "conclusion-revoke gate: a revoke MUST carry a free-text resolved['reason'] "
            "(why the finding no longer holds). It binds no new sha, but it is journaled, "
            "attributed, and permanent."
        )
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "conclusion-revoke gate: withdrawing a conclusion is a HUMAN act — a bare "
            f"{spec.response!r} (a 'y' / click) cannot revoke it. State why you are "
            f"withdrawing the conclusion ({conclusion_id!r})."
        )
    # B4 ts>=anchor: name the conclusion in an utterance logged AT OR AFTER the
    # conclusion it withdraws — the creation utterance (which named the id) no
    # longer self-satisfies the naming leg. Anchor = the conclusion filing ts.
    from hpc_agent.state.evidence import CONCLUSION_BLOCK

    anchor = _target_record_ts(
        experiment_dir,
        scope_kind=spec.scope_kind,
        scope_id=spec.scope_id,
        filing_block=CONCLUSION_BLOCK,
        id_field="conclusion_id",
        target_id=conclusion_id,
    )
    authored = _fresh_authored_text(experiment_dir, response, anchor=anchor)
    if not _names_slug(authored, conclusion_id):
        _refuse_missing_authorship(
            "conclusion-revoke gate: the revoke must NAME the conclusion_id "
            f"{conclusion_id!r} token-exact (the #26 floor), in an utterance logged "
            "AT OR AFTER the conclusion it withdraws (B4 ts>=anchor). Restate, naming "
            "the conclusion being withdrawn."
        )


def _assert_conclusion_full(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """E-shape's three locks for a ``conclusion`` record — a human-authored finding.

    A conclusion is an ordinary ``append-decision`` whose ``scope_kind=="conclusion"``,
    ``block=="conclusion"``, and ``resolved={conclusion_id, tags, concludes?,
    citations, finding}`` (E-shape). It records a dated, sha-linked finding over
    sealed evidence — a HUMAN attestation, so it faces both the un-fakeable
    citation-verification lock and the tiered authorship bar.

    **Lock 1 (no affordance)** is organizational: there is no conclusion verb /
    chain / next_block / skill — append-decision under this block is the only write
    path (pinned by the T11 contract test; no primitive is named conclude).

    **Lock 2 (recompute, un-fakeable)** — the ``resolved`` shape is validated
    server-side (:func:`state.evidence.validate_conclusion_resolved` — slug-validated
    ``conclusion_id``, shape-validated ``tags``, a NON-EMPTY ``citations`` list). Then
    EVERY citation is resolved against the LIVE stores through its kind's ONE resolver
    (:func:`state.evidence.resolve_citation`, the ``dossier`` slot fed the injected
    :func:`_conclusion_dossier_resolver`): an unresolvable or mismatched citation
    REFUSES with the recorded-vs-live pair — you cannot conclude about evidence the
    machine cannot find on this namespace at write time (the receipt-laundering hole,
    closed at the memory boundary). The whole verified set is then hash-locked: its
    canonical ``content_sha`` (:func:`state.evidence.citations_content_sha`) binds
    through the ONE attestation kernel (:func:`state.attestation.bind`) and is persisted
    into ``resolved`` so the reduction's stored-sha fallback agrees.

    **Lock 3 (authorship, the R6 bar reused)** — bare acks refused
    (:func:`_is_bare_ack`); the response must NAME the ``conclusion_id`` token-exact
    AND name at least one CITED sha by an 8+ hex prefix
    (:func:`_names_citation_sha_prefix`) matched against the citations just verified.
    Tiered on the harness utterance log (:func:`_registration_authored_text` — the
    LOCK when present, the agent-relayed ``response`` FRICTION tier otherwise). There
    is NO auto-clear / redundant tier: a conclusion's attestor is ALWAYS human (a
    machine has no findings, only measurements, and the measurements are already
    attested elsewhere).

    The authorship-bar refusals (bare ack, missing id, missing sha-prefix) carry the
    E2 marker via :func:`_refuse_missing_authorship`; the Lock-2 shape / citation
    refusals raise plain :class:`errors.SpecInvalid` UNMARKED (a re-elicit cannot fix
    a moved or absent evidence sha — the E2 scoping).
    """
    from hpc_agent.state import attestation
    from hpc_agent.state.evidence import (
        SUBJECT_KIND as CONCLUSION_SUBJECT_KIND,
    )
    from hpc_agent.state.evidence import (
        citations_content_sha,
        resolve_citation,
        validate_conclusion_resolved,
    )

    # ── Lock 2 shape: slug-validated id + non-empty shape-validated citations ──
    parsed = validate_conclusion_resolved(resolved)

    # ── Base authorship floor (Lock 3, part 1): non-bare + names the id ──
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "conclusion gate: recording a finding is a HUMAN act — a bare "
            f"{spec.response!r} (a 'y' / click) cannot conclude. Name the conclusion "
            f"({parsed.conclusion_id!r}) and a cited sha prefix you reviewed."
        )
    authored = _registration_authored_text(experiment_dir, response)
    if not _names_slug(authored, parsed.conclusion_id):
        _refuse_missing_authorship(
            "conclusion gate: the finding must NAME the conclusion_id "
            f"{parsed.conclusion_id!r} token-exact (the #26 floor). Restate, naming the "
            "conclusion."
        )

    # ── Lock 2 (recompute — the load-bearing verification): every citation must
    # resolve AND match against the LIVE stores or the append is refused. ──
    dossier_resolver = _conclusion_dossier_resolver(experiment_dir)
    for cit in parsed.citations:
        res = resolve_citation(experiment_dir, cit, dossier_resolver=dossier_resolver)
        if not res.resolved:
            raise errors.SpecInvalid(
                f"conclusion gate (lock 2): citation {cit.kind}:{cit.ref!r} is UNRESOLVABLE "
                f"on this namespace ({res.detail}) — a conclusion may only cite evidence the "
                "machine can find at write time. Cite evidence that exists on this namespace."
            )
        if not res.matches:
            raise errors.SpecInvalid(
                f"conclusion gate (lock 2): citation {cit.kind}:{cit.ref!r} sha MISMATCH "
                f"({res.detail}) — the asserted sha {cit.sha!r} is not what the live store "
                "carries. A caller-asserted sha is never trusted-then-recorded (the "
                "receipt-laundering hole). Quote the live sha."
            )

    # ── content_sha bound via the ONE kernel against the re-canonicalized set ──
    content_sha = citations_content_sha(parsed.citations)
    attestation.bind(
        {
            "attestor": "human",
            "subject_kind": CONCLUSION_SUBJECT_KIND,
            "subject_id": parsed.conclusion_id,
            "content_sha": content_sha,
        },
        recompute=lambda: citations_content_sha(parsed.citations),
    )
    # Persist the hash-lock so reduce_conclusion's stored-sha fallback agrees.
    resolved["content_sha"] = content_sha

    # ── Lock 3, part 2: the raised bar — name a CITED sha by an 8+ hex prefix ──
    if _names_citation_sha_prefix(authored, parsed.citations) is None:
        _refuse_missing_authorship(
            "conclusion gate (lock 3): the finding must NAME at least one cited sha by an "
            "8+ hex-character prefix (the diff-token pattern at its strongest — a token that "
            "exists nowhere in a human's prior vocabulary and can only derive from the "
            "presented evidence). Quote a cited sha prefix from the evidence-brief."
        )


def _assert_conclusion_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """The E-shape conclusion gate — evidence memory's one new attested record (T8).

    Block convention, enforced BOTH directions (mirrors ``registration`` /
    ``notebook-sign-off``): a conclusion-family block
    (:data:`state.evidence.CONCLUSION_BLOCK_FAMILY`) is refused for any
    ``scope_kind`` other than ``"conclusion"``; and the ``"conclusion"`` scope
    accepts ONLY the block family (``conclusion`` / ``conclusion-revoke``). Every
    other record passes untouched. Dispatches a ``conclusion-revoke`` to the revoke
    floor and a ``conclusion`` to the full three locks.

    Raises :class:`errors.SpecInvalid` on any refusal (authorship-bar refusals carry
    the E2 marker so the single append firing site covers conclusions over MCP too;
    shape / citation refusals stay unmarked).
    """
    from hpc_agent.state.evidence import (
        CONCLUSION_BLOCK_FAMILY,
        CONCLUSION_REVOKE_BLOCK,
    )

    block = spec.block
    in_family = block in CONCLUSION_BLOCK_FAMILY
    # A conclusion-family block is conclusion-scope-only.
    if in_family and spec.scope_kind != "conclusion":
        raise errors.SpecInvalid(
            f"block {block!r} is a conclusion-family block, only valid for "
            f"scope_kind='conclusion'; got scope_kind={spec.scope_kind!r}."
        )
    if spec.scope_kind != "conclusion":
        return  # not a conclusion record — nothing to gate
    # The conclusion scope accepts ONLY its block family.
    if not in_family:
        raise errors.SpecInvalid(
            f"scope_kind='conclusion' accepts only its block family "
            f"{sorted(CONCLUSION_BLOCK_FAMILY)}; got block={block!r} — a conclusion scope "
            "records ONLY conclusion / conclusion-revoke (E-shape)."
        )
    if not isinstance(resolved, dict):
        raise errors.SpecInvalid(
            "conclusion gate: resolved must be a mapping carrying the conclusion fields "
            "{conclusion_id, tags, citations, finding}."
        )
    if block == CONCLUSION_REVOKE_BLOCK:
        _assert_conclusion_revoke_floor(experiment_dir, spec, resolved)
        return
    # block == CONCLUSION_BLOCK — the human finding (E-shape three locks).
    _assert_conclusion_full(experiment_dir, spec, resolved)
