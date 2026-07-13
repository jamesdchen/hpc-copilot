"""The registration sign-off gate family (R6/R7 + live-conformance T7) — the
deployment-boundary attestation, its revoke floor, its review floor, and the
conformance-verdict gate."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.state.registration import (
    CONFORMANCE_VERDICT_BLOCK,
    REGISTRATION_BLOCK,
    REGISTRATION_BLOCK_FAMILY,
    REGISTRATION_REVIEW_BLOCK,
    REVOKE_BLOCK,
    SUBJECT_KIND,
)

from ._shared import (
    _HEX_RUN_RE,
    _fresh_authored_text,
    _is_bare_ack,
    _names_slug,
    _names_target_sha_prefix,
    _read_decisions,
    _refuse_missing_authorship,
    _registration_authored_text,
    _target_record_ts,
)

# ── registration authorship gate (R6 three locks + the revoke floor, T7) ──────

# The seven ``resolved`` keys a registration record must carry as non-empty
# values (R6 lock 2). ``view_sha`` is required too (checked separately — it is
# the fourth recompute leg, R6). A registration is the maximal human ceremony:
# every leg is recomputed server-side and no waiver / auto-clear / redundant tier
# exists at this gate (the attestor is ALWAYS human, R6 lock 3).
_REGISTRATION_REQUIRED_KEYS: tuple[str, ...] = (
    "registration_id",
    "run_id",
    "dossier_sha",
    "template",
    "template_sha",
    "fields",
    "prerequisites",
)


def _field_present(value: Any) -> bool:
    """True when a template field value counts as PRESENT — non-None, non-empty.

    Mirrors ``ops/registration/verify_op.py::_nonempty`` (completeness is COUNTING
    over opaque values, R5 — a value is never read for meaning, only for presence).
    """
    if value is None:
        return False
    return not (isinstance(value, (str, list, tuple, dict, set)) and len(value) == 0)


def _names_sha_prefix(text: str, chain_entries: list[Any]) -> str | None:
    """The first chain entry ``content_sha`` a hex run in *text* prefixes, or None.

    R6 lock 3: the sign-off must NAME at least one prerequisite by an 8+ hex-char
    prefix of one chain entry's ``content_sha``, matched against the gate-verified
    chain. Case-insensitive prefix match.
    """
    for run in (m.group(0).lower() for m in _HEX_RUN_RE.finditer(text or "")):
        for entry in chain_entries:
            if str(entry.content_sha).lower().startswith(run):
                return str(entry.content_sha)
    return None


def _assert_revoke_floor(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """The R7 revoke floor for a ``registration-revoke`` record.

    A human overturn: non-bare, NAMES the ``registration_id``, and its free-text
    ``reason`` is MANDATORY ("validate or overturn WITH reason", the consumer-seat
    prior). It binds no new sha (it withdraws), so there is NO recompute leg — but
    it is journaled, attributed, and permanent like everything else. The bare-ack
    and id-naming refusals carry the E2 authorship marker (a freshly typed human
    rationale resolves them); the missing-reason refusal is structural (the agent
    must add ``resolved['reason']``), left UNMARKED.
    """
    registration_id = resolved.get("registration_id")
    if not isinstance(registration_id, str) or not registration_id:
        raise errors.SpecInvalid(
            "registration-revoke gate: resolved must name a non-empty registration_id "
            "(the id being overturned)."
        )
    reason = resolved.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise errors.SpecInvalid(
            "registration-revoke gate: a revoke MUST carry a free-text resolved['reason'] "
            "(validate or overturn WITH reason — the consumer-seat prior, R7). It binds no new "
            "sha, but it is journaled, attributed, and permanent."
        )
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "registration-revoke gate: overturning a registration is a HUMAN act — a bare "
            f"{spec.response!r} (a 'y' / click) cannot revoke it. State why you are revoking "
            f"the registration ({registration_id!r})."
        )
    # B4 ts>=anchor: the naming leg reads only utterances logged at or after the
    # target registration's ts — the human named the id at CREATION, so an
    # unbounded read is permanently satisfied and an agent-composed revoke rides
    # through. Anchor = the registration filing record's ts (None → unfiltered,
    # the existence checks above / below own the never-registered case).
    anchor = _target_record_ts(
        experiment_dir,
        scope_kind=spec.scope_kind,
        scope_id=spec.scope_id,
        filing_block=REGISTRATION_BLOCK,
        id_field="registration_id",
        target_id=registration_id,
    )
    authored = _fresh_authored_text(experiment_dir, response, anchor=anchor)
    if not _names_slug(authored, registration_id):
        _refuse_missing_authorship(
            "registration-revoke gate: the revoke must NAME the registration_id "
            f"{registration_id!r} token-exact (the #26 floor), in an utterance logged "
            "AT OR AFTER the registration it overturns (B4 ts>=anchor). Restate, naming "
            "the registration being overturned."
        )


def _assert_conformance_baseline_membership(resolved: dict[str, Any], sig: Any) -> None:
    """Refuse a ``conformance`` declaration whose baseline is NOT in the sealed dossier.

    The live-conformance C-declare append leg (moved here from registration T6 by
    pre-implementation verification — the state substrate imports no ``ops`` and
    ``compute_dossier_signature`` is an ``ops`` seam). When the registration's
    ``resolved`` carries an optional ``conformance`` block, it is validated
    STRUCTURE-only through the ONE declaration validator
    (:func:`~hpc_agent.state.registration.parse_conformance_declaration` →
    ``state/conformance.py::validate_declaration``; unknown keys refused there),
    and its declared baseline ``{path, sha256}`` must be a MEMBER of *sig*'s
    dry-gathered manifest entries (identity against the ``{path, sha256}`` pairs
    the dossier seals). So the control limits derive from evidence INSIDE the
    sealed dossier by construction — never from a file the caller can swap after
    sign-off. An absent block is a no-op (conformance is opt-in, byte-identical).

    Structural refusal (UNMARKED): a non-member baseline is a moved/absent
    artifact a re-elicited utterance cannot fix.
    """
    from hpc_agent.state.registration import parse_conformance_declaration

    declaration = parse_conformance_declaration(resolved)
    if declaration is None:
        return  # conformance is opt-in — no block, no machinery (byte-identical)
    path = declaration.baseline.path
    sha256 = declaration.baseline.sha256
    # Membership is by the sealed-bytes SHA — the integrity identity. The declared
    # ``path`` is the caller's EXPERIMENT-RELATIVE locator the read-side (T5/T8)
    # resolves; the dossier's manifest entries carry ARCHIVE paths (the ``_aggregated``
    # → ``aggregated`` rename means the two path spellings differ by construction), so
    # the sha is the ONE stable identity across both. A declared sha that is a sealed
    # entry's sha proves the control limits derive from evidence INSIDE the sealed
    # dossier — never a file swapped after sign-off (C-declare; recorded in the drift log).
    if any(entry.get("sha256") == sha256 for entry in sig.entries):
        return
    raise errors.SpecInvalid(
        "registration gate (lock 2, conformance): the declared conformance baseline "
        f"{{path={path!r}, sha256={sha256[:12]}...}} is NOT sealed in the dossier — no manifest "
        "entry carries that sha256. The control limits must derive from evidence INSIDE the "
        "sealed dossier by construction (C-declare), never a file swapped after sign-off. "
        "Declare a baseline artifact the dossier seals."
    )


def _assert_registration_full(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """R6's three locks for a ``registration`` record — every bar at its ceiling.

    Lock 1 (no affordance) is organizational: append-decision under this block is
    the ONLY write path (there is no registration verb / chain / next_block /
    skill; pinned by the contract test). Lock 2 recomputes ALL FOUR legs
    server-side and binds through :func:`state.attestation.bind`:

    * (a) ``dossier_sha`` vs a dry ``compute_dossier_signature`` re-gather from the
      LIVE stores (R2 — you may not register what has drifted since it was
      validated); bound through the ONE attestation kernel.
    * (b) ``template_sha`` vs the template file's raw bytes on disk (R5), plus
      template completeness by COUNTING (every declared field slug non-empty in
      ``resolved['fields']``; every declared prerequisite slot present in the
      chain).
    * (c) every chain entry's ``content_sha`` via ``check_chain`` — ALL slots must
      verdict CURRENT (partial registration REFUSED, naming every failing slot).
    * (d) ``view_sha`` RECOMPUTED via the deterministic verify-registration brief
      projection (:func:`build_view` over the append-time legs — a witness you can
      regenerate is regenerated).

    Lock 3 (authorship, the raised bar): bare acks refused; the response must NAME
    the ``registration_id`` token-exact AND name at least one prerequisite by an 8+
    hex prefix of one chain entry's ``content_sha`` (matched against the verified
    chain). Tiered on the harness utterance log (:func:`_registration_authored_text`).
    NO auto-clear tier, NO redundant-mark path — the attestor is ALWAYS human.

    The authorship-bar refusals (bare ack, missing id, missing sha-prefix) carry
    the E2 marker via :func:`_refuse_missing_authorship`; the Lock-2 sha /
    structural refusals raise plain :class:`errors.SpecInvalid` UNMARKED (a re-elicit
    cannot fix a moved hash — the E2 scoping).
    """
    from hpc_agent._wire.actions.verify_registration import (
        DossierLeg,
        FieldsBlock,
        LegStatus,
        PrerequisiteKind,
        PrerequisiteLeg,
        TemplateLeg,
    )
    from hpc_agent.ops import export_dossier, registration_view
    from hpc_agent.state import attestation
    from hpc_agent.state.registration import CURRENT as REG_CURRENT
    from hpc_agent.state.registration import parse_chain_entry, parse_template

    # ── Lock 2 shape: the seven required non-empty keys + view_sha ──
    missing = [k for k in _REGISTRATION_REQUIRED_KEYS if not resolved.get(k)]
    if missing:
        raise errors.SpecInvalid(
            "registration gate (lock 2): resolved must carry non-empty "
            f"{list(_REGISTRATION_REQUIRED_KEYS)}; missing/empty: {missing}."
        )
    registration_id = resolved["registration_id"]
    run_id = resolved["run_id"]
    dossier_sha = resolved["dossier_sha"]
    template_rel = resolved["template"]
    template_sha = resolved["template_sha"]
    fields = resolved["fields"]
    prerequisites = resolved["prerequisites"]
    view_sha = resolved.get("view_sha")
    if not isinstance(view_sha, str) or not view_sha:
        raise errors.SpecInvalid(
            "registration gate (lock 2): resolved must carry a non-empty view_sha — the "
            "code-rendered verify-registration brief the human saw (R6's fourth recompute leg)."
        )
    if not (
        isinstance(registration_id, str)
        and isinstance(run_id, str)
        and isinstance(dossier_sha, str)
        and isinstance(template_rel, str)
        and isinstance(template_sha, str)
    ):
        raise errors.SpecInvalid(
            "registration gate (lock 2): registration_id / run_id / dossier_sha / template / "
            "template_sha must all be strings."
        )
    if not isinstance(fields, dict):
        raise errors.SpecInvalid(
            "registration gate (lock 2): resolved['fields'] must be a mapping."
        )
    if not isinstance(prerequisites, list):
        raise errors.SpecInvalid(
            "registration gate (lock 2): resolved['prerequisites'] must be a list (the chain)."
        )

    # ── Base authorship floor (Lock 3, part 1): non-bare + names the id ──
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "registration gate: registering a strategy is the maximal HUMAN ceremony — a bare "
            f"{spec.response!r} (a 'y' / click) cannot register. Name the registration "
            f"({registration_id!r}) and a prerequisite sha prefix you reviewed."
        )
    authored = _registration_authored_text(experiment_dir, response)
    if not _names_slug(authored, registration_id):
        _refuse_missing_authorship(
            "registration gate: the sign-off must NAME the registration_id "
            f"{registration_id!r} token-exact (the #26 floor). Restate, naming the registration."
        )

    # ── Lock 2a: dossier — dry re-gather + bind through the ONE kernel ──
    include_lineage = bool(resolved.get("include_lineage", False))
    sig = export_dossier.compute_dossier_signature(
        experiment_dir, run_id, include_lineage=include_lineage
    )
    attestation.bind(
        {
            "attestor": "human",
            "subject_kind": SUBJECT_KIND,
            "subject_id": registration_id,
            "content_sha": dossier_sha,
            "view_sha": view_sha,
        },
        recompute=sig.bundle_sha256,
    )

    # ── Lock 2a': conformance baseline membership (live-conformance C-declare) ──
    # When the registration opts into live conformance, the declared baseline
    # {path, sha256} must be a MEMBER of the dossier's dry-gathered manifest
    # entries — the control limits derive from evidence INSIDE the sealed dossier
    # by construction, never from a file swapped after sign-off.
    _assert_conformance_baseline_membership(resolved, sig)

    # ── Lock 2b: template raw-bytes sha on disk + completeness by counting ──
    try:
        tmpl_bytes = (Path(experiment_dir) / template_rel).read_bytes()
    except OSError as exc:
        raise errors.SpecInvalid(
            f"registration gate (lock 2): template file {template_rel!r} is unreadable ({exc}). "
            "A registration recomputes the template raw-bytes sha from disk; an unresolvable "
            "template is refused, never skipped."
        ) from exc
    recomputed_template_sha = hashlib.sha256(tmpl_bytes).hexdigest()
    if recomputed_template_sha != template_sha:
        raise errors.SpecInvalid(
            f"registration gate (lock 2): template sha mismatch — recorded {template_sha!r} vs "
            f"the on-disk {recomputed_template_sha!r}. A hash cannot be asserted into existence."
        )
    try:
        template = parse_template(
            json.loads(tmpl_bytes.decode("utf-8")), template_sha=recomputed_template_sha
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise errors.SpecInvalid(
            f"registration gate (lock 2): template {template_rel!r} is not valid UTF-8 JSON "
            f"({exc})."
        ) from exc
    missing_fields = [s for s in template.fields if not _field_present(fields.get(s))]
    if missing_fields:
        raise errors.SpecInvalid(
            "registration gate (lock 2): template fields incomplete — every declared field slug "
            f"must carry a non-empty value in resolved['fields']; missing: {missing_fields}."
        )

    # ── Lock 2c: the prerequisite chain — every declared slot filled + all CURRENT ──
    entries = [parse_chain_entry(e) for e in prerequisites]
    declared_slots = {p.slot for p in template.prerequisites}
    chain_slots = {e.slot for e in entries}
    missing_slots = sorted(declared_slots - chain_slots)
    if missing_slots:
        raise errors.SpecInvalid(
            f"registration gate (lock 2): declared prerequisite slot(s) {missing_slots} are not "
            "present in the chain — every declared prerequisite must be filled (counting)."
        )
    verdicts = registration_view.check_chain(
        experiment_dir, entries, dossier_run_ids=set(sig.run_ids)
    )
    failing = [(v.slot, v.status) for v in verdicts if v.status != REG_CURRENT]
    if failing:
        names = ", ".join(f"{slot}={status}" for slot, status in failing)
        raise errors.SpecInvalid(
            "registration gate (lock 2): partial registration REFUSED — prerequisite slot(s) not "
            f"CURRENT: {names}. Every prerequisite must read current at append (R4); the remedy "
            "for partial readiness is not registering."
        )

    # ── Lock 2d: view_sha recomputed via the deterministic brief projection ──
    dossier_leg = DossierLeg(
        recorded_sha=dossier_sha, recomputed_sha=sig.bundle_sha256, drifted_stores=[]
    )
    template_leg = TemplateLeg(
        status="current", recorded_sha=template_sha, recomputed_sha=recomputed_template_sha
    )
    prereq_legs = [
        PrerequisiteLeg(
            slot=v.slot,
            kind=cast("PrerequisiteKind", v.kind),
            status=cast("LegStatus", v.status),
            recorded_sha=v.recorded_sha,
            recomputed_sha=v.recomputed_sha,
            evidence_note=v.evidence_note,
        )
        for v in verdicts
    ]
    declared = list(template.fields)
    fields_report = FieldsBlock(
        declared=declared,
        present=[s for s in declared if _field_present(fields.get(s))],
        missing=[],
    )
    _, recomputed_view_sha = registration_view.build_view(
        status=REG_CURRENT,
        registration_id=registration_id,
        registered_at=None,
        dossier=dossier_leg,
        template=template_leg,
        prerequisites=prereq_legs,
        fields=fields_report,
    )
    if recomputed_view_sha != view_sha:
        raise errors.SpecInvalid(
            "registration gate (lock 2, fourth leg): the recomputed verify-registration view_sha "
            f"({recomputed_view_sha}) does not equal the signed view_sha ({view_sha}). The brief "
            "the human bound must be the deterministic projection over the CURRENT legs — re-run "
            "verify-registration and sign THAT view_sha."
        )

    # ── Lock 3, part 2: the raised bar — name a prerequisite by an 8+ hex sha prefix ──
    if _names_sha_prefix(authored, entries) is None:
        _refuse_missing_authorship(
            "registration gate (lock 3): the sign-off must NAME at least one prerequisite by an "
            "8+ hex-character prefix of one chain entry's content_sha (the diff-token pattern at "
            "its strongest — an 8-hex prefix exists nowhere in a human's prior vocabulary and can "
            "only derive from the presented evidence). Quote a prerequisite sha prefix from the "
            "verify-registration brief."
        )


def _assert_registration_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """The R6 registration sign-off gate — the deployment-boundary attestation.

    Block convention, enforced BOTH directions (mirrors ``scope-unlock`` /
    ``notebook-sign-off``): a registration-family block
    (:data:`REGISTRATION_BLOCK_FAMILY`) is refused for any ``scope_kind`` other
    than ``"registration"``; and the ``"registration"`` scope accepts ONLY the
    block family (``registration`` / ``registration-revoke`` /
    ``registration-review`` / ``conformance-verdict``). Every other record passes
    untouched. Dispatches a ``registration-revoke`` to the revoke floor (R7), a
    ``registration-review`` to the C-horizon re-affirmation floor, a
    ``conformance-verdict`` to the C-verdict drift-verdict gate, and a
    ``registration`` to the full three locks (R6).

    Raises :class:`errors.SpecInvalid` on any refusal (authorship-bar refusals
    carry the E2 marker so the single append firing site covers registration
    sign-offs over MCP too; sha / structural refusals stay unmarked).
    """
    block = spec.block
    in_family = block in REGISTRATION_BLOCK_FAMILY
    # A registration-family block is registration-scope-only.
    if in_family and spec.scope_kind != "registration":
        raise errors.SpecInvalid(
            f"block {block!r} is a registration-family block, only valid for "
            f"scope_kind='registration'; got scope_kind={spec.scope_kind!r}."
        )
    if spec.scope_kind != "registration":
        return  # not a registration record — nothing to gate
    # The registration scope accepts ONLY its block family.
    if not in_family:
        raise errors.SpecInvalid(
            f"scope_kind='registration' accepts only its block family "
            f"{sorted(REGISTRATION_BLOCK_FAMILY)}; got block={block!r} — a registration scope "
            "records ONLY registration / registration-revoke (R6)."
        )
    if not isinstance(resolved, dict):
        raise errors.SpecInvalid(
            "registration gate: resolved must be a mapping carrying the registration fields."
        )
    if block == REVOKE_BLOCK:
        _assert_revoke_floor(experiment_dir, spec, resolved)
        return
    if block == REGISTRATION_REVIEW_BLOCK:
        _assert_registration_review_floor(experiment_dir, spec, resolved)
        return
    if block == CONFORMANCE_VERDICT_BLOCK:
        _assert_conformance_verdict_authorship(experiment_dir, spec, resolved)
        return
    # block == REGISTRATION_BLOCK — the maximal human ceremony (R6 three locks).
    _assert_registration_full(experiment_dir, spec, resolved)


# ── registration-review floor + conformance-verdict gate (live-conformance T7) ─


def _names_any_sha_prefix(text: str, shas: Sequence[str]) -> bool:
    """True iff a hex run in *text* is an 8+ hex prefix of ANY sha in *shas*.

    The R6 sha-prefix bar (:data:`_HEX_RUN_RE`) applied to a bare list of shas
    (the conformance-verdict ``cites`` are raw ``content_sha`` strings, not
    ``.sha``-bearing citation objects like :func:`_names_citation_sha_prefix`).
    Case-insensitive prefix match — a token that can only derive from the
    presented evidence brief.
    """
    for run in (m.group(0).lower() for m in _HEX_RUN_RE.finditer(text or "")):
        for sha in shas:
            if str(sha).lower().startswith(run):
                return True
    return False


def _valid_review_horizon(review_horizon: str) -> None:
    """Refuse a ``registration-review`` horizon that is not ISO-8601 (T7 append check).

    C-horizon: core names no period and computes no cadence — it only compares a
    caller-computed timestamp. The reduction's :func:`state.registration._horizon_lapsed`
    is deliberately tolerant (an unparseable horizon yields "not lapsed"), so the
    *append* gate is where a malformed horizon is caught loudly (its docstring
    names this gate). A trailing ``Z`` normalizes to ``+00:00`` before
    :func:`datetime.fromisoformat`.
    """
    from datetime import datetime

    raw = review_horizon[:-1] + "+00:00" if review_horizon.endswith("Z") else review_horizon
    try:
        datetime.fromisoformat(raw)
    except (ValueError, TypeError) as exc:
        raise errors.SpecInvalid(
            "registration-review gate: resolved['review_horizon'] must be an ISO-8601 "
            f"timestamp (the caller computes the date; core compares timestamps); got "
            f"{review_horizon!r} ({exc})."
        ) from exc


def _assert_registration_review_floor(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """The C-horizon re-affirmation floor for a ``registration-review`` record.

    A review EXTENDS a current registration's ``review_horizon`` WITHOUT
    re-registration, when nothing has drifted — the cheaper tier that answers
    "does a human still stand behind this, today?" (recorded rationale: forcing a
    full re-registration for an unchanged dossier would train horizon inflation).

    ``resolved = {registration_id, dossier_sha, review_horizon}`` — all three
    non-empty; ``review_horizon`` a valid ISO timestamp (:func:`_valid_review_horizon`).

    The authorship floor (the R6 form, tiered on the harness utterance log via
    :func:`_registration_authored_text`): a bare ack cannot re-affirm; the response
    must NAME the ``registration_id`` token-exact AND the dossier sha by an 8+ hex
    prefix (:func:`_names_target_sha_prefix`).

    **The recompute leg — you cannot re-affirm a DRIFTED registration (C-horizon).**
    The gate reduces the id's registration journal to the WINNER, RECOMPUTES the
    live dossier signature via the ONE seam
    (:func:`~hpc_agent.ops.export_dossier.compute_dossier_signature`), and refuses
    when it no longer equals the winner's recorded ``dossier_sha`` — the remedy for
    a moved dossier is re-registration, not review. The review's own asserted
    ``dossier_sha`` must name that SAME sealed dossier.

    Authorship-bar refusals carry the E2 marker (a freshly typed re-affirmation
    resolves them); the shape / drift / missing-winner refusals raise plain
    :class:`errors.SpecInvalid` UNMARKED (a re-elicit cannot un-drift a dossier).
    """
    from hpc_agent.ops import export_dossier
    from hpc_agent.state.registration import ABSENT as REG_ABSENT
    from hpc_agent.state.registration import REVOKED as REG_REVOKED
    from hpc_agent.state.registration import reduce_registration

    registration_id = resolved.get("registration_id")
    if not isinstance(registration_id, str) or not registration_id:
        raise errors.SpecInvalid(
            "registration-review gate: resolved must name a non-empty registration_id "
            "(the registration being re-affirmed)."
        )
    dossier_sha = resolved.get("dossier_sha")
    if not isinstance(dossier_sha, str) or not dossier_sha:
        raise errors.SpecInvalid(
            "registration-review gate: resolved must carry a non-empty dossier_sha (the sealed "
            "dossier being re-affirmed — the review recomputes the live signature against it)."
        )
    review_horizon = resolved.get("review_horizon")
    if not isinstance(review_horizon, str) or not review_horizon:
        raise errors.SpecInvalid(
            "registration-review gate: resolved must carry a non-empty review_horizon (the new "
            "ISO horizon the re-affirmation extends to)."
        )
    _valid_review_horizon(review_horizon)

    # ── authorship floor (part 1): non-bare + names the id ──
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "registration-review gate: re-affirming a registration is a HUMAN act — a bare "
            f"{spec.response!r} (a 'y' / click) cannot re-affirm it. Name the registration "
            f"({registration_id!r}) and the dossier sha prefix you reviewed."
        )
    authored = _registration_authored_text(experiment_dir, response)
    if not _names_slug(authored, registration_id):
        _refuse_missing_authorship(
            "registration-review gate: the re-affirmation must NAME the registration_id "
            f"{registration_id!r} token-exact (the #26 floor). Restate, naming the registration."
        )

    # ── recompute leg: reduce to the winner; the live dossier must NOT have drifted ──
    records = _read_decisions(experiment_dir, spec.scope_kind, spec.scope_id)
    peek = reduce_registration(records, registration_id=registration_id, live_dossier_sha=None)
    winner = peek.winner
    if winner is None or peek.status in (REG_ABSENT, REG_REVOKED):
        raise errors.SpecInvalid(
            f"registration-review gate: no current registration named {registration_id!r} to "
            f"re-affirm (status {peek.status!r}). A review extends a LIVE registration's horizon; "
            "there is nothing to re-affirm — re-register the subject."
        )
    run_id = winner.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise errors.SpecInvalid(
            f"registration-review gate: the winning registration for {registration_id!r} carries "
            "no run_id, so the live dossier signature cannot be recomputed to confirm nothing "
            "drifted."
        )
    recorded_sha = winner.get("dossier_sha")
    sig = export_dossier.compute_dossier_signature(
        experiment_dir, run_id, include_lineage=bool(winner.get("include_lineage", False))
    )
    if sig.bundle_sha256 != recorded_sha:
        raise errors.SpecInvalid(
            "registration-review gate: the live dossier signature "
            f"({sig.bundle_sha256[:12]}...) does not match the registration's recorded "
            f"dossier_sha ({str(recorded_sha)[:12]}...). You CANNOT re-affirm a registration whose "
            "sealed stores have DRIFTED (C-horizon) — the remedy is re-registration, not review."
        )
    if dossier_sha != recorded_sha:
        raise errors.SpecInvalid(
            "registration-review gate: resolved['dossier_sha'] "
            f"({dossier_sha[:12]}...) does not name the registration's sealed dossier "
            f"({str(recorded_sha)[:12]}...). A review re-affirms the EXISTING dossier; name "
            "its sha."
        )

    # ── authorship floor (part 2): name the dossier sha by an 8+ hex prefix ──
    if not _names_target_sha_prefix(authored, str(recorded_sha)):
        _refuse_missing_authorship(
            "registration-review gate: the re-affirmation must NAME the dossier sha by an 8+ "
            "hex-character prefix (a token that can only derive from the presented "
            "verify-registration brief). Quote the dossier sha prefix you reviewed."
        )


def _assert_conformance_verdict_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """The live-conformance drift-verdict gate (C-verdict) — a DATED CONCLUSION.

    A human's resolution of a ``needs_verdict`` / ``nonconforming`` FINDING is an
    ordinary ``append-decision`` (block ``conformance-verdict`` on scope kind
    ``registration`` — no verdict verb, the no-unlock-verb doctrine), citing the
    offending receipts by sha. ``resolved = {registration_id, cites: [<receipt
    content_sha>, ...], note}`` — ``cites`` NON-EMPTY, ``note`` a free-text opaque
    dated conclusion. The verdict binds NO dossier (it is dated evidence, never a
    re-registration) and never mutates the registration's status.

    **Lock 2 (recompute — the E-shape citation posture).** Every cited sha is
    resolved SERVER-SIDE against the registration's conformance ledger
    (:func:`~hpc_agent.state.conformance_store.read_observations`): a sha the ledger
    does NOT carry is refused (a caller-asserted sha is never trusted-then-recorded).

    **Lock 3 (authorship, the R6 bar reused, tiered on the harness log via
    :func:`_registration_authored_text`).** A bare ack cannot resolve a finding;
    the response must NAME the ``registration_id`` token-exact AND at least one
    cited receipt sha by an 8+ hex prefix (:func:`_names_any_sha_prefix`).

    Authorship-bar refusals carry the E2 marker (a freshly typed verdict resolves
    them); the shape / citation refusals raise plain :class:`errors.SpecInvalid`
    UNMARKED (a re-elicit cannot conjure a receipt the ledger never carried).
    """
    from hpc_agent.state.conformance_store import read_observations

    # ── Lock 2 shape: id + non-empty cites + a dated note ──
    registration_id = resolved.get("registration_id")
    if not isinstance(registration_id, str) or not registration_id:
        raise errors.SpecInvalid(
            "conformance-verdict gate: resolved must name a non-empty registration_id (the "
            "registration whose drift this verdict resolves)."
        )
    cites = resolved.get("cites")
    if not isinstance(cites, list) or not cites:
        raise errors.SpecInvalid(
            "conformance-verdict gate: resolved['cites'] must be a NON-EMPTY list of the "
            "offending receipt content_shas the verdict resolves (C-verdict — a drift verdict "
            "cites the receipts it judges)."
        )
    cite_shas: list[str] = []
    for c in cites:
        if not isinstance(c, str) or not c:
            raise errors.SpecInvalid(
                f"conformance-verdict gate: each cite must be a non-empty content_sha string; "
                f"got {c!r}."
            )
        cite_shas.append(c)
    note = resolved.get("note")
    if not isinstance(note, str) or not note.strip():
        raise errors.SpecInvalid(
            "conformance-verdict gate: resolved must carry a free-text 'note' — the human's "
            "dated conclusion over the cited drift (opaque to core, but a verdict is a dated "
            "CONCLUSION, never a bare citation)."
        )

    # ── Lock 3 (part 1): non-bare + names the id ──
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "conformance-verdict gate: judging a drift FINDING is a HUMAN act — a bare "
            f"{spec.response!r} (a 'y' / click) cannot resolve it. Name the registration "
            f"({registration_id!r}) and a cited receipt sha you reviewed."
        )
    authored = _registration_authored_text(experiment_dir, response)
    if not _names_slug(authored, registration_id):
        _refuse_missing_authorship(
            "conformance-verdict gate: the verdict must NAME the registration_id "
            f"{registration_id!r} token-exact (the #26 floor). Restate, naming the registration."
        )

    # ── Lock 2 (recompute): every cited sha must be CARRIED by the ledger ──
    ledger_records, _skipped = read_observations(experiment_dir, registration_id)
    ledger_shas = {
        str(r.get("content_sha"))
        for r in ledger_records
        if isinstance(r.get("content_sha"), str) and r.get("content_sha")
    }
    unknown = [c for c in cite_shas if c not in ledger_shas]
    if unknown:
        raise errors.SpecInvalid(
            "conformance-verdict gate: cited content_sha(s) "
            f"{[c[:12] + '...' for c in unknown]} are NOT carried by registration "
            f"{registration_id!r}'s conformance ledger — a verdict may only cite receipts that "
            "EXIST on record (the E-shape citation posture; a caller-asserted sha is never "
            "trusted-then-recorded). Quote the offending receipts' shas from the "
            "conformance-status brief."
        )

    # ── Lock 3 (part 2): name at least one cited receipt sha by an 8+ hex prefix ──
    if not _names_any_sha_prefix(authored, cite_shas):
        _refuse_missing_authorship(
            "conformance-verdict gate: the verdict must NAME at least one cited receipt sha by an "
            "8+ hex-character prefix (a token that can only derive from the presented evidence "
            "brief). Quote an offending receipt's content_sha prefix."
        )
