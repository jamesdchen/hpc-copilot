"""The challenge authorship gate (C-gate, T5) — structured dissent's family of
attested records: the filing's three locks and the verdict/withdraw floors."""

from __future__ import annotations

from collections.abc import Sequence
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
    _names_target_sha_prefix,
    _read_decisions,
    _read_interview_actors,
    _refuse_missing_authorship,
    _registration_authored_text,
    _session_actor,
    _target_record_ts,
)


def _challenge_filing_citations(experiment_dir: Path, challenge_id: str) -> Sequence[Any]:
    """The verified citations of *challenge_id*'s FILING record, or ``()``.

    Reads the challenge's own journal (the C-shape per-id thread), finds the
    newest ``challenge`` filing record, and returns its validated
    :class:`~hpc_agent.state.evidence.Citation` list — the shas a DISMISSAL must
    engage (C-gate: dismissing evidence requires naming it). ``()`` when no
    parseable filing exists (the caller then refuses the resolution — you cannot
    resolve a challenge that was never filed).
    """
    from hpc_agent.state.challenges import CHALLENGE_BLOCK, validate_challenge_resolved

    citations: Sequence[Any] = ()
    for rec in _read_decisions(experiment_dir, "challenge", challenge_id):
        if rec.get("block") != CHALLENGE_BLOCK:
            continue
        resolved = rec.get("resolved")
        if not isinstance(resolved, dict) or resolved.get("challenge_id") != challenge_id:
            continue
        try:
            citations = validate_challenge_resolved(resolved).citations
        except errors.SpecInvalid:
            continue
    return citations


def _challenge_filing_attestor(experiment_dir: Path, challenge_id: str) -> str | None:
    """The ``attestor_id`` (challenger) of *challenge_id*'s newest FILING, or ``None``.

    MH7: the resolver≠challenger / withdrawer==challenger comparisons read WHO
    filed the challenge from the filing record's own ``attestor_id`` — the opaque
    actor slug the ops append stamped at filing time (server-resolved, never
    caller-suppliable). ``None`` when no parseable filing exists OR the filing was
    unattributed (a zero/one-actor filing, or a >1-actor filing with no resolvable
    session actor) — the caller's >1-actor guard then decides the refusal.
    """
    from hpc_agent.state.challenges import CHALLENGE_BLOCK

    attestor: str | None = None
    for rec in _read_decisions(experiment_dir, "challenge", challenge_id):
        if rec.get("block") != CHALLENGE_BLOCK:
            continue
        resolved = rec.get("resolved")
        if not isinstance(resolved, dict) or resolved.get("challenge_id") != challenge_id:
            continue
        raw = rec.get("attestor_id")
        attestor = raw if isinstance(raw, str) and raw else None
    return attestor


def _recompute_challenge_view_sha(
    experiment_dir: Path, challenge_id: str, carried_view_sha: str
) -> None:
    """RECOMPUTE a carried ``view_sha`` against the challenge-status render (C-verb).

    The ``challenge-status`` brief is a PURE FUNCTION of the projection (no
    wall-clock, no fleet accounting — ``ops/challenge_status_op.py``), so a
    ``view_sha`` a human bound after reading the thread is recomputable: the gate
    re-invokes the ONE render (never a second inlined projection — the v1.6
    recomputable-render precedent) and refuses a mismatch. Reached through the
    top-level ``challenge_status_op`` role-root module (the subject-import lint
    permits the ops-facade form from inside the ``decision`` subject — the
    ``export_dossier`` precedent).

    Structural refusal (UNMARKED): a stale ``view_sha`` names a view that no longer
    renders — a re-elicited utterance cannot fix it, so it carries no E2 marker.

    Runtime note (drift log): the render routes through
    ``state/challenges.py::standing_challenges`` — the ONE collector; the op⇄state
    (T1) and op⇄wire (T2) entry-shape reconciliation is the Wave-A/B integrator's
    step, so this recompute is exercised under the same collector monkeypatch the
    op's own tests use until that lands. The op is reached via
    :func:`importlib.import_module` (not a static ``from ... import``) precisely
    because the op module is PRE-INTEGRATION and carries the placeholder-vs-real
    T2/T1 type divergence: a followed import would drag those known-transient
    errors into this subject's type-check. ``view_sha`` is OPTIONAL, so a verdict
    that carries none never reaches here.
    """
    import importlib

    op = importlib.import_module("hpc_agent.ops.challenge_status_op")
    result = op.challenge_status(
        experiment_dir=experiment_dir,
        spec=op.ChallengeStatusSpec(challenge_id=challenge_id),
    )
    if result.view_sha != carried_view_sha:
        raise errors.SpecInvalid(
            "challenge resolution gate (view_sha recompute): the carried view_sha "
            f"{carried_view_sha!r} does not match the challenge-status render for "
            f"{challenge_id!r} (recomputed {result.view_sha!r}). The view moved after "
            "the human signed it — re-read `challenge-status` and bind the current view_sha."
        )


def _assert_challenge_filing_full(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """The C-gate three locks for a ``challenge`` FILING — human-authored dissent.

    A challenge is an ordinary ``append-decision`` whose ``scope_kind=="challenge"``,
    ``block=="challenge"``, ``resolved={challenge_id, target, citations, grounds}``
    (C-shape). It records a dated, sha-targeted, evidence-bound attestation of
    DISSENT — a HUMAN act (C3: code never files dissent), so it faces both the
    un-fakeable target/citation-verification lock and the raised authorship bar.

    **Lock 1 (no affordance)** — append-decision under this block is the ONLY write
    path; no challenge/contest/dispute/refute verb, chain, or next_block (pinned by
    the T9 contract test).

    **Lock 2 (recompute, un-fakeable)** — ``resolved`` validated server-side
    (:func:`state.challenges.validate_challenge_resolved`: slug ``challenge_id``, a
    full-address ``target``, a NON-EMPTY ``citations`` list, non-empty ``grounds``).
    Then the TARGET is resolved server-side and confirmed committed at the asserted
    ``content_sha`` (:func:`state.challenges.resolve_target_existence` — the
    ``attestation`` kind SCANS the named journal so a non-newest record is findable,
    C2); every CITATION is resolved against the LIVE stores
    (:func:`state.evidence.resolve_citation`) and refused on unresolvable/mismatch —
    you cannot contest what the machine cannot find, nor rest on evidence it cannot
    resolve (the receipt-laundering hole at the dissent boundary). The verified
    ``{target, citations}`` set is then hash-locked: its canonical ``content_sha``
    (:func:`state.challenges.challenge_content_sha`) binds through the ONE kernel
    (:func:`state.attestation.bind`) and persists into ``resolved``.

    **Lock 3 (authorship, the R6 bar reused)** — bare acks refused
    (:func:`_is_bare_ack`); the response must NAME the ``challenge_id`` token-exact
    AND the TARGET's ``content_sha`` by an 8+ hex prefix (:func:`_names_sha_prefix` —
    you must name what you attack) AND at least one CITED sha by an 8+ hex prefix
    (:func:`_names_citation_sha_prefix` — you must name what you rest on). Tiered on
    the harness utterance log (:func:`_registration_authored_text`). NO auto-clear
    tier: a challenge's attestor is ALWAYS human (C3).

    Authorship-bar refusals carry the E2 marker (:func:`_refuse_missing_authorship`);
    Lock-2 shape/target/citation refusals raise plain :class:`errors.SpecInvalid`
    UNMARKED (a re-elicit cannot conjure a moved or absent sha — the E2 scoping).
    """
    from hpc_agent.state import attestation
    from hpc_agent.state.challenges import (
        SUBJECT_KIND as CHALLENGE_SUBJECT_KIND,
    )
    from hpc_agent.state.challenges import (
        challenge_content_sha,
        resolve_target_existence,
        validate_challenge_resolved,
    )
    from hpc_agent.state.evidence import resolve_citation

    # ── Lock 2 shape: slug id + full-address target + non-empty citations/grounds ──
    parsed = validate_challenge_resolved(resolved)

    # ── Base authorship floor (Lock 3, part 1): non-bare + names the id ──
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "challenge gate: filing structured dissent is a HUMAN act — a bare "
            f"{spec.response!r} (a 'y' / click) cannot challenge. Name the challenge "
            f"({parsed.challenge_id!r}), the target sha you attack, and a cited sha you rest on."
        )
    authored = _registration_authored_text(experiment_dir, response)
    if not _names_slug(authored, parsed.challenge_id):
        _refuse_missing_authorship(
            "challenge gate: the filing must NAME the challenge_id "
            f"{parsed.challenge_id!r} token-exact (the #26 floor). Restate, naming the challenge."
        )

    # ── Lock 2 (recompute): the target must exist committed at the asserted sha ──
    dossier_resolver = _conclusion_dossier_resolver(experiment_dir)
    target_res = resolve_target_existence(
        experiment_dir, parsed.target, dossier_resolver=dossier_resolver
    )
    if not target_res.resolved:
        raise errors.SpecInvalid(
            f"challenge gate (lock 2): the target "
            f"{parsed.target.kind}:{parsed.target.subject_kind}/{parsed.target.subject_id} "
            f"is UNRESOLVABLE on this namespace ({target_res.detail}) — you cannot contest a "
            "record the machine cannot find. Address a committed record that exists here."
        )
    if not target_res.matches:
        raise errors.SpecInvalid(
            f"challenge gate (lock 2): the target subject exists but carries NO committed "
            f"record at the asserted content_sha {parsed.target.content_sha!r} "
            f"({target_res.detail}). A challenge binds to an exact committed sha (R3); quote "
            "the sha of a record that exists on record."
        )

    # ── Lock 2 (recompute): every citation must resolve AND match the live store ──
    for cit in parsed.citations:
        res = resolve_citation(experiment_dir, cit, dossier_resolver=dossier_resolver)
        if not res.resolved:
            raise errors.SpecInvalid(
                f"challenge gate (lock 2): citation {cit.kind}:{cit.ref!r} is UNRESOLVABLE on "
                f"this namespace ({res.detail}) — a challenge may only rest on evidence the "
                "machine can find at write time. Cite evidence that exists on this namespace."
            )
        if not res.matches:
            raise errors.SpecInvalid(
                f"challenge gate (lock 2): citation {cit.kind}:{cit.ref!r} sha MISMATCH "
                f"({res.detail}) — the asserted sha {cit.sha!r} is not what the live store "
                "carries. A caller-asserted sha is never trusted-then-recorded (the "
                "receipt-laundering hole). Quote the live sha."
            )

    # ── content_sha bound via the ONE kernel against the re-canonicalized set ──
    content_sha = challenge_content_sha(parsed.target, parsed.citations)
    attestation.bind(
        {
            "attestor": "human",
            "subject_kind": CHALLENGE_SUBJECT_KIND,
            "subject_id": parsed.challenge_id,
            "content_sha": content_sha,
        },
        recompute=lambda: challenge_content_sha(parsed.target, parsed.citations),
    )
    resolved["content_sha"] = content_sha

    # ── Lock 3, part 2: name the TARGET sha AND a CITED sha by 8+ hex prefix ──
    if not _names_target_sha_prefix(authored, parsed.target.content_sha):
        _refuse_missing_authorship(
            "challenge gate (lock 3): the filing must NAME the TARGET's content_sha by an "
            "8+ hex-character prefix (you must name what you attack — a token that can only "
            "derive from the presented record). Quote the target sha prefix from the "
            "challenge-status / verify-registration brief."
        )
    if _names_citation_sha_prefix(authored, parsed.citations) is None:
        _refuse_missing_authorship(
            "challenge gate (lock 3): the filing must NAME at least one CITED sha by an "
            "8+ hex-character prefix (you must name what you rest on). Quote a cited sha "
            "prefix from the evidence you are standing on."
        )


def _assert_challenge_verdict_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """The C-gate verdict/withdraw FLOOR — resolving standing dissent (C4).

    A verdict (``challenge-verdict``) or withdrawal (``challenge-withdraw``) is a
    SEPARATE record from the filing (C-gate: so the resolver≠challenger constraint is
    expressible later without a record-shape change — see the resolver-identity note
    below). Both face the same floor: non-bare, ``challenge_id`` token-exact, and a
    mandatory free-text ``reasoning``/``reason`` (waving dissent away with a bare ack
    is exactly the asymmetry violation the nudge machinery exists to prevent, C4).

    A DISMISSAL additionally must NAME one of the CHALLENGE's cited shas by an 8+ hex
    prefix (:func:`_challenge_filing_citations` — dismissing evidence requires
    engaging it; dismissal is effortful by construction, C4). An UPHELD verdict needs
    no extra sha (upholding agrees with evidence already bound into the record it
    resolves). A carried ``view_sha`` is RECOMPUTED against the challenge-status
    render (:func:`_recompute_challenge_view_sha`, C-verb).

    **Resolver-identity extension (MH7 — LANDED HERE as the multi-human follow-up).**
    ``docs/design/multi-human.md`` MH7 owns attributed authorship and reserved this
    identity comparison to the challenge gate as "a follow-up task executed by
    whichever plan lands second". Multi-human landed second, so it lands here now:
    under >1 declared actors (MH1), the VERDICT gate refuses ``resolver ==
    challenger`` (you may not adjudicate your own objection — the challenger is the
    filing record's ``attestor_id``, :func:`_challenge_filing_attestor`; the resolver
    is the session actor) and refuses an UNATTRIBUTED resolution; the WITHDRAWAL gate
    refuses ``withdrawer != challenger`` (a second actor silencing another's standing
    dissent is the suppression channel) AND refuses withdrawing an UNATTRIBUTED filing
    outright (challenger is ``None`` — RULING 4, bug-sweep #37: no one owns it, so no
    one may withdraw it; ``None != None`` is False, so the compare alone would leave
    the anonymous-suppression channel open — the refusal routes to challenge-verdict,
    which records WHO resolved it, e.g. the cheap "withdrawn-as-stale" shape). Pure
    identity over opaque slugs — Q1-clean.
    Zero/one actor declared → silent, byte-identical (a solo researcher legitimately
    resolves their own past challenge). These identity refusals are the loud/dangling
    posture (NOT the E2 marker — a re-elicited utterance cannot fix WHO the session
    is). The verdict/withdraw being a SEPARATE record from the filing is what kept the
    constraint expressible without a record-shape change.

    Authorship-bar refusals carry the E2 marker; the missing-reason / stale-view /
    MH7-identity structural refusals raise plain :class:`errors.SpecInvalid` UNMARKED.
    """
    from hpc_agent.state.challenges import (
        CHALLENGE_VERDICT_BLOCK,
        DISMISSED,
        validate_verdict_resolved,
        validate_withdraw_resolved,
    )

    is_verdict = spec.block == CHALLENGE_VERDICT_BLOCK
    if is_verdict:
        parsed_v = validate_verdict_resolved(resolved)
        challenge_id = parsed_v.challenge_id
    else:  # CHALLENGE_WITHDRAW_BLOCK
        parsed_w = validate_withdraw_resolved(resolved)
        challenge_id = parsed_w.challenge_id

    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            f"challenge-{'verdict' if is_verdict else 'withdraw'} gate: resolving a challenge "
            f"is a HUMAN act — a bare {spec.response!r} (a 'y' / click) cannot resolve it. "
            f"Name the challenge ({challenge_id!r}) and state your reasoning."
        )
    # B4 ts>=anchor: name the challenge in an utterance logged AT OR AFTER the
    # filing it resolves — the challenger named the id when FILING, so an
    # unbounded read lets an agent-composed verdict/withdraw ride the creation
    # utterance. Anchor = the challenge filing ts (the thread is keyed by
    # challenge_id; None → unfiltered, the filing-existence checks own that case).
    from hpc_agent.state.challenges import CHALLENGE_BLOCK

    anchor = _target_record_ts(
        experiment_dir,
        scope_kind="challenge",
        scope_id=challenge_id,
        filing_block=CHALLENGE_BLOCK,
        id_field="challenge_id",
        target_id=challenge_id,
    )
    authored = _fresh_authored_text(experiment_dir, response, anchor=anchor)
    if not _names_slug(authored, challenge_id):
        _refuse_missing_authorship(
            f"challenge-{'verdict' if is_verdict else 'withdraw'} gate: the resolution must "
            f"NAME the challenge_id {challenge_id!r} token-exact (the #26 floor), in an "
            "utterance logged AT OR AFTER the filing it resolves (B4 ts>=anchor). Restate, "
            "naming the challenge being resolved."
        )

    # A DISMISSAL must engage the challenge's evidence by naming a cited sha prefix.
    if is_verdict and parsed_v.verdict == DISMISSED:
        citations = _challenge_filing_citations(experiment_dir, challenge_id)
        if not citations:
            raise errors.SpecInvalid(
                "challenge-verdict gate: no parseable filing found for challenge "
                f"{challenge_id!r} — a verdict resolves a challenge that was filed. File the "
                "challenge before dismissing it."
            )
        if _names_citation_sha_prefix(authored, citations) is None:
            _refuse_missing_authorship(
                "challenge-verdict gate: a DISMISSAL must NAME one of the challenge's cited "
                "shas by an 8+ hex prefix — dismissing evidence requires engaging it (dismissal "
                "is effortful by construction, C4). Quote a cited sha prefix you are dismissing."
            )

    # A carried view_sha is recomputed against the challenge-status render (C-verb).
    view_sha = resolved.get("view_sha")
    if isinstance(view_sha, str) and view_sha:
        _recompute_challenge_view_sha(experiment_dir, challenge_id, view_sha)

    # ── MH7: resolver ≠ challenger (verdict) / withdrawer == challenger (withdraw) ──
    # Silent under zero/one declared actor (byte-identical — a solo researcher
    # legitimately resolves their own past challenge).
    ids, _policy = _read_interview_actors(experiment_dir)
    if len(ids) > 1:
        session_actor = _session_actor(experiment_dir, ids)
        challenger = _challenge_filing_attestor(experiment_dir, challenge_id)
        if is_verdict:
            if session_actor is None:
                raise errors.SpecInvalid(
                    "challenge-verdict gate (MH7): >1 actor is declared but this "
                    f"session has no resolvable actor, so challenge {challenge_id!r} "
                    "would be resolved anonymously — an unattributed adjudication is "
                    "the laundering channel. Configure HPC_ACTOR to a declared actor."
                )
            if challenger is not None and session_actor == challenger:
                raise errors.SpecInvalid(
                    "challenge-verdict gate (MH7): the resolver "
                    f"({session_actor!r}) is the CHALLENGER who filed "
                    f"{challenge_id!r} — you may not adjudicate your own objection. A "
                    "DIFFERENT declared actor must resolve it."
                )
        else:  # CHALLENGE_WITHDRAW_BLOCK
            if challenger is None:
                # RULING 4 (2026-07-12, bug-sweep #37): an UNATTRIBUTED filing
                # (no HPC_ACTOR at filing time, or filed before actors were
                # declared → attestor_id is None) has NO owner, so NO ONE may
                # withdraw it — a withdrawal is the challenger's own private
                # retraction, and an unowned filing has no challenger to retract.
                # This check MUST precede the identity compare: ``None != None`` is
                # False, so without it an anonymous session (the driving agent that
                # forgot HPC_ACTOR) would silence unowned dissent (the exact #37
                # anonymous-suppression hole), and an attributed actor would hit the
                # confusing "session actor is someone else than None" path. Closure
                # stays possible without erasure: route to challenge-verdict, which
                # RECORDS who resolved it and why (the cheap "withdrawn-as-stale"
                # verdict shape) — no one may withdraw what no one owns.
                raise errors.SpecInvalid(
                    "challenge-withdraw gate (MH7 / RULING 4): challenge "
                    f"{challenge_id!r} was filed WITHOUT actor attribution — no one "
                    "owns it, so no one may withdraw it (a withdrawal is the "
                    "challenger's own retraction, and an unowned filing has no "
                    "challenger to retract). Resolve it via a challenge-verdict "
                    "instead (e.g. reasoning 'withdrawn-as-stale') — that RECORDS "
                    "who resolved it and why. Closure stays possible; erasure does not."
                )
            if session_actor != challenger:
                raise errors.SpecInvalid(
                    "challenge-withdraw gate (MH7): only the CHALLENGER who filed "
                    f"{challenge_id!r} (actor {challenger!r}) may withdraw it — the "
                    f"session actor {session_actor!r} is someone else, and a second "
                    "actor silencing another's standing dissent is the suppression "
                    "channel. The challenger must withdraw their own challenge."
                )


def _assert_challenge_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """The challenge gate — structured dissent's family of attested records (T5).

    Block convention, enforced BOTH directions (mirrors ``conclusion`` /
    ``registration``): a challenge-family block
    (:data:`state.challenges.CHALLENGE_BLOCK_FAMILY`) is refused for any
    ``scope_kind`` other than ``"challenge"``; and the ``"challenge"`` scope accepts
    ONLY the block family (``challenge`` / ``challenge-verdict`` /
    ``challenge-withdraw``). Every other record passes untouched. Dispatches a
    ``challenge`` FILING to the three locks (:func:`_assert_challenge_filing_full`)
    and a ``challenge-verdict`` / ``challenge-withdraw`` to the resolution floor
    (:func:`_assert_challenge_verdict_authorship`).

    Raises :class:`errors.SpecInvalid` on any refusal (authorship-bar refusals carry
    the E2 marker so the single append firing site covers challenges over MCP too;
    shape / target / citation refusals stay unmarked).
    """
    from hpc_agent.state.challenges import (
        CHALLENGE_BLOCK,
        CHALLENGE_BLOCK_FAMILY,
    )

    block = spec.block
    in_family = block in CHALLENGE_BLOCK_FAMILY
    # A challenge-family block is challenge-scope-only.
    if in_family and spec.scope_kind != "challenge":
        raise errors.SpecInvalid(
            f"block {block!r} is a challenge-family block, only valid for "
            f"scope_kind='challenge'; got scope_kind={spec.scope_kind!r}."
        )
    if spec.scope_kind != "challenge":
        return  # not a challenge record — nothing to gate
    # The challenge scope accepts ONLY its block family.
    if not in_family:
        raise errors.SpecInvalid(
            f"scope_kind='challenge' accepts only its block family "
            f"{sorted(CHALLENGE_BLOCK_FAMILY)}; got block={block!r} — a challenge scope "
            "records ONLY challenge / challenge-verdict / challenge-withdraw (C-shape)."
        )
    if not isinstance(resolved, dict):
        raise errors.SpecInvalid(
            "challenge gate: resolved must be a mapping carrying the challenge fields "
            "{challenge_id, target, citations, grounds}."
        )
    if block == CHALLENGE_BLOCK:
        _assert_challenge_filing_full(experiment_dir, spec, resolved)
        return
    # block ∈ {challenge-verdict, challenge-withdraw} — the resolution floor.
    _assert_challenge_verdict_authorship(experiment_dir, spec, resolved)
