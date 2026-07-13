"""The overnight standing-consent gate (notebook-audit.md item 8) — the human's
BOUND acceptance of unattended overnight fallout, plus the compose seat."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput

from ._shared import (
    _is_bare_ack,
    _refuse_missing_authorship,
)


def _compose_overnight_consent(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Poka-yoke seat: compose the wake + cap defaults for a standing consent.

    The item-8 conversions (notebook-audit.md ruling, 2026-07-10) run HERE, before
    the gates, so a composed block satisfies the caps + wake assertions rather than
    tripping their refusals — the human is handed a complete, editable ``resolved``
    (with every composed field disclosed in ``composed_defaults``) instead of a
    NO-GO. A non-``overnight-consent`` record passes untouched; off a run/campaign
    scope nothing is composed (the authorship gate raises on the bad scope). Never
    composes ``cmd_sha`` — its absence still refuses at
    :func:`hpc_agent.ops.overnight.assert_consent_hard_caps` (the identity binding
    is not a default). Reached through the top-level ``hpc_agent.ops.overnight``
    role-root sibling, exactly as the authorship gate below imports it.
    """
    from hpc_agent.ops import overnight as _overnight

    if spec.block != _overnight.OVERNIGHT_CONSENT_BLOCK:
        return resolved
    if spec.scope_kind not in _overnight.CONSENT_SCOPE_KINDS:
        return resolved
    return _overnight.compose_overnight_consent(
        experiment_dir,
        scope_kind=spec.scope_kind,
        scope_id=spec.scope_id,
        resolved=resolved if isinstance(resolved, dict) else {},
    )


def _bound_consent_records(
    experiment_dir: Path, *, scope_kind: str, scope_id: str, block: str
) -> list[dict[str, Any]]:
    """Utterance records whose ``bound`` names EXACTLY this scope + block.

    THE bound-capture reader (``docs/design/bound-capture.md``): selects only the
    utterances a view-aware surface (the MCP elicitation popup) captured BOUND to
    ``(scope_kind, scope_id, block)`` — a chat-hook prompt never carries ``bound``,
    so it can never appear here. This reads the utterance store but is NOT the
    "unbounded naming pool" the B4 ts>=anchor filter guards: the B4 exploit is that
    the utterance which CREATED a target permanently satisfies a NAMING leg, and it
    cannot apply to an EXACT binding the chat hook is structurally unable to forge
    (the same "temporal binding by vocabulary impossibility" class as the
    sha-prefix FILING gates). Documented as such in the B4 route-through exemption.
    """
    from hpc_agent.state.utterances import read_utterances

    out: list[dict[str, Any]] = []
    for rec in read_utterances(experiment_dir):
        bound = rec.get("bound")
        if not isinstance(bound, dict):
            continue
        if bound.get("scope_kind") != scope_kind:
            continue
        if bound.get("scope_id") != scope_id:
            continue
        if bound.get("block") != block:
            continue
        out.append(rec)
    return out


def _assert_overnight_consent_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """Overnight standing-consent gate — the human's BOUND acceptance of fallout.

    A STANDING CONSENT (``docs/design/notebook-audit.md`` item 8) lets named
    boundaries auto-advance while the human sleeps. It is journaled as an
    ``append-decision`` under the distinct block
    :data:`hpc_agent.ops.overnight.OVERNIGHT_CONSENT_BLOCK` (there is no consent
    verb — this gate is the only choke point), so it cannot be laundered around.
    Three legs:

    * **block convention** — the ``overnight-consent`` block is valid only for a
      ``run`` / ``campaign`` scope (a boundary the human sleeps through), refused
      for any other ``scope_kind``.
    * **bound authorship** (USER RULING 3, 2026-07-12 — bound-capture ONLY) — a
      BOUND consent record (``docs/design/bound-capture.md``) captured at a surface
      that named EXACTLY what it covers must exist, matching this append's
      ``(scope_kind, scope_id, block)`` AND its coverage: the ``cmd_sha``
      spec-identity, the ``heal_classes`` the consent declares (the record must
      cover at least them), a non-expired coverage window, and non-bare text. The
      FORENSIC word-overlap tier (an agent-relayed ``response`` word-matched over
      the unbounded chat log) is DELETED: overnight consent is valid ONLY when
      captured through a binding surface, never reconstructed from the stream. The
      missing-bound refusal carries the E2 authorship-missing marker so the MCP
      popup fires, captures the typed consent BOUND to this coverage
      (``mcp_server._overnight_consent_binding``), and the retry finds it.
    * **hard caps + spec identity + the wake** (pins b + c + the wake amendment) —
      :func:`hpc_agent.ops.overnight.assert_consent_hard_caps` and
      :func:`hpc_agent.ops.overnight.assert_wake_armed`. STRUCTURAL refusals (a
      fresh utterance cannot supply a cap or arm a watch), so deliberately NOT
      marked with the authorship-missing marker.

    Every non-``overnight-consent`` record passes untouched. Reached through the
    top-level ``hpc_agent.ops.overnight`` module (a role-root sibling, allowed
    from inside the ``decision`` subject exactly like the ``field_ownership``
    facade import).
    """
    from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow
    from hpc_agent.ops import overnight as _overnight

    if spec.block != _overnight.OVERNIGHT_CONSENT_BLOCK:
        return  # not a standing consent — nothing to gate
    if spec.scope_kind not in _overnight.CONSENT_SCOPE_KINDS:
        raise errors.SpecInvalid(
            f"block {_overnight.OVERNIGHT_CONSENT_BLOCK!r} is a standing consent, only "
            f"valid for scope_kind in {sorted(_overnight.CONSENT_SCOPE_KINDS)} (a run "
            f"or campaign boundary the human sleeps through); got "
            f"scope_kind={spec.scope_kind!r}."
        )

    # Leg 1 — BOUND authorship (bound-capture ONLY, USER RULING 3): a consent is
    # valid only when captured at a surface that named exactly what it covers.
    res = resolved if isinstance(resolved, dict) else {}
    declared_classes = res.get("heal_classes")
    consent_classes = (
        {str(c) for c in declared_classes if isinstance(c, str)}
        if isinstance(declared_classes, list)
        else set()
    )
    bound_cmd_sha = res.get("cmd_sha") if isinstance(res.get("cmd_sha"), str) else None
    now = utcnow()

    covered = False
    for rec in _bound_consent_records(
        experiment_dir,
        scope_kind=spec.scope_kind,
        scope_id=spec.scope_id,
        block=_overnight.OVERNIGHT_CONSENT_BLOCK,
    ):
        if _is_bare_ack(str(rec.get("text") or "")):
            continue  # a typed 'y' bound to the coverage is still a bare ack
        subject = rec["bound"].get("subject")
        subject = subject if isinstance(subject, dict) else {}
        # Spec identity: the bound record must name THIS consent's cmd_sha (a
        # consent binds to a spec; both-absent falls through to the caps refusal).
        subj_sha = subject.get("cmd_sha") if isinstance(subject.get("cmd_sha"), str) else None
        if subj_sha != bound_cmd_sha:
            continue
        # Repair-class coverage: the human's bound consent must cover at least the
        # classes this consent declares (it can cover more — a superset is fine).
        subj_classes_raw = subject.get("heal_classes")
        subj_classes = (
            {str(c) for c in subj_classes_raw if isinstance(c, str)}
            if isinstance(subj_classes_raw, list)
            else set()
        )
        if not consent_classes <= subj_classes:
            continue
        # Coverage window: a bound consent whose window has passed no longer covers.
        subj_expires = subject.get("expires_at")
        expires = parse_iso_utc_or_none(subj_expires if isinstance(subj_expires, str) else None)
        if expires is not None and now >= expires:
            continue
        covered = True
        break

    if not covered:
        _refuse_missing_authorship(
            "overnight-consent bound-authorship gate: a standing consent accepts the "
            "fallout of unattended overnight advances and is valid ONLY when captured "
            "at a binding surface that names exactly what it covers (bound-capture, "
            "USER RULING 3) — there is no bound consent record covering this boundary "
            f"({spec.scope_kind} {spec.scope_id!r}), its cmd_sha, its declared "
            f"heal_classes {sorted(consent_classes)}, and a live coverage window. A "
            "free-text chat utterance (however it names the boundaries) can NEVER "
            "satisfy it — the chat channel captures no binding. To GRANT: run under an "
            "elicitation-capable harness so the overnight-consent popup fires and "
            "captures your typed consent BOUND to this coverage; type the consent "
            "there (a bare 'y' cannot stand in for it)."
        )

    # Legs 2 + 3 — structural (never the authorship marker): hard caps + spec
    # identity, then the armed wake.
    _overnight.assert_consent_hard_caps(resolved)
    _overnight.assert_wake_armed(
        experiment_dir,
        scope_kind=spec.scope_kind,
        scope_id=spec.scope_id,
        resolved=resolved,
    )
