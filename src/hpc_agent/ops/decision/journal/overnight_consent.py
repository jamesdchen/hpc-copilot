"""The overnight standing-consent gate (notebook-audit.md item 8) — the human's
acceptance of unattended overnight fallout (bound capture, or token-exact chat
consent naming the rendered coverage), plus the compose seat."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput

from ._shared import (
    _actor_scoped_human_texts,
    _is_bare_ack,
    _names_slug,
    _names_target_sha_prefix,
    _read_interview_actors,
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
    utterances a view-aware BINDING surface captured BOUND to
    ``(scope_kind, scope_id, block)`` — a chat-hook prompt never carries ``bound``,
    so it can never appear here. With the MCP elicitation popup retired
    (2026-07-27) core ships no binding surface of its own; the tier remains for
    any conforming SECOND harness whose capture surface knows exactly what the
    typed text signs (the §2 write API's ``bound`` clause). This reads the
    utterance store but is NOT the "unbounded naming pool" the B4 ts>=anchor
    filter guards: the B4 exploit is that the utterance which CREATED a target
    permanently satisfies a NAMING leg, and it cannot apply to an EXACT binding
    the chat hook is structurally unable to forge (the same "temporal binding by
    vocabulary impossibility" class as the sha-prefix FILING gates). Documented
    as such in the B4 route-through exemption.
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
    """Overnight standing-consent gate — the human's acceptance of fallout.

    A STANDING CONSENT (``docs/design/notebook-audit.md`` item 8) lets named
    boundaries auto-advance while the human sleeps. It is journaled as an
    ``append-decision`` under the distinct block
    :data:`hpc_agent.ops.overnight.OVERNIGHT_CONSENT_BLOCK` (there is no consent
    verb — this gate is the only choke point), so it cannot be laundered around.
    Three legs:

    * **block convention** — the ``overnight-consent`` block is valid only for a
      ``run`` / ``campaign`` scope (a boundary the human sleeps through), refused
      for any other ``scope_kind``.
    * **consent authorship** (two accepted tiers; re-ruled 2026-07-27 with the
      MCP elicitation popup's retirement) —

      1. **bound capture** (``docs/design/bound-capture.md``): a BOUND consent
         record captured at a surface that named EXACTLY what it covers,
         matching this append's ``(scope_kind, scope_id, block)`` AND its
         coverage: the ``cmd_sha`` spec-identity, the ``heal_classes`` the
         consent declares (the record must cover at least them), a non-expired
         coverage window, and non-bare text. Core no longer ships a binding
         surface (the popup retired); the tier stands for any conforming second
         harness whose capture surface knows what the typed text signs.
      2. **token-exact chat consent**: a logged human utterance (the
         ``UserPromptSubmit`` capture hook — actor-scoped under >1 declared
         actors, MH4) that is non-bare, NAMES the boundary ``scope_id``
         token-exact, NAMES every declared ``heal_class`` token-exact, and —
         when the consent binds a ``cmd_sha`` — names that sha by an 8+
         hex-character prefix (the R6 idiom: a token that exists nowhere in a
         human's prior vocabulary and can only derive from the code-rendered
         coverage brief the refusal displays, so the temporal binding the old
         word-overlap tier lacked is carried by vocabulary impossibility).

      The 2026-07-12 bound-capture-ONLY ruling (USER RULING 3) is REVISED, not
      restored to the deleted word-overlap tier: that ruling's premise was the
      elicitation popup as the binding surface, and the popup is retired; the
      chat tier here demands token-exact naming + the sha prefix, a strictly
      higher bar than the deleted forensic word-overlap. The refusal renders the
      full coverage (boundary, classes, caps, window, cmd_sha) INLINE so the
      human reads exactly what they are consenting to in the chat stream and
      types the consent there. Carries the authorship-missing marker (a freshly
      typed consent naming the coverage resolves it).
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

    # Leg 1 — consent authorship: a bound capture from a binding surface, OR a
    # token-exact chat consent naming the rendered coverage (2026-07-27 ruling).
    res = resolved if isinstance(resolved, dict) else {}
    declared_classes = res.get("heal_classes")
    consent_classes = (
        {str(c) for c in declared_classes if isinstance(c, str)}
        if isinstance(declared_classes, list)
        else set()
    )
    bound_cmd_sha = res.get("cmd_sha") if isinstance(res.get("cmd_sha"), str) else None
    # S1 (run-queue plan §10.S1.5): a consent that binds a placement must have the
    # human name its cluster set out loud — the same discipline the sha prefix
    # enforces for spec identity. Absent/unusable placement ⇒ no requirement
    # (every pre-placement consent), mirroring the consumption leg's posture.
    from hpc_agent.state.placement_drift import normalize_recorded_placement

    bound_placement = normalize_recorded_placement(res.get("placement"))
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
        # Placement identity: symmetric both-present compare (S1). A bound record
        # captured before placement existed carries none and still covers a
        # placement-bound consent — the conservative direction; the consumption
        # leg still refuses a placement the consent set does not contain.
        subj_placement = normalize_recorded_placement(subject.get("placement"))
        if bound_placement and subj_placement and set(subj_placement) != set(bound_placement):
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
        # The token-exact CHAT tier: a logged human utterance naming the exact
        # coverage. The sha-prefix leg carries the temporal binding (an 8+ hex
        # prefix of cmd_sha can only derive from the coverage brief below); when
        # the consent binds no cmd_sha the leg is moot here and the caps gate's
        # own missing-cmd_sha refusal owns that case (mirroring the bound tier's
        # both-absent fall-through).
        actor_ids, _ = _read_interview_actors(experiment_dir)
        chat_texts = _actor_scoped_human_texts(experiment_dir, actor_ids) or []
        for text in chat_texts:
            if _is_bare_ack(text):
                continue
            if not _names_slug(text, spec.scope_id):
                continue
            if not all(_names_slug(text, cls) for cls in sorted(consent_classes)):
                continue
            if bound_cmd_sha is not None and not _names_target_sha_prefix(text, bound_cmd_sha):
                continue
            # S1: a placement-bound consent's chat grant must name every cluster
            # in the set — the human says WHERE out loud, not just what.
            if bound_placement and not all(_names_slug(text, c) for c in bound_placement):
                continue
            covered = True
            break

    if not covered:
        # Render the COVERAGE inline (code-selected identifiers only — the same
        # subset the bound tier matches), so the human reads exactly what the
        # consent covers in the chat stream and can type it there.
        composed_expires = res.get("expires_at")
        classes_line = (
            ", ".join(sorted(consent_classes)) if consent_classes else "none (watcher re-arm only)"
        )
        coverage_lines = [
            f"  boundary: {spec.scope_kind} {spec.scope_id}",
            f"  repair classes authorized while you sleep: {classes_line}",
        ]
        if isinstance(composed_expires, str) and composed_expires:
            coverage_lines.append(f"  consent expires at the morning boundary: {composed_expires}")
        caps: list[str] = []
        budget_cap = res.get("budget_cap")
        walltime_cap = res.get("walltime_cap")
        if isinstance(budget_cap, (int, float)) and not isinstance(budget_cap, bool):
            caps.append(f"budget_cap={budget_cap}")
        if isinstance(walltime_cap, (int, float)) and not isinstance(walltime_cap, bool):
            caps.append(f"walltime_cap={walltime_cap}s")
        if caps:
            coverage_lines.append("  hard caps on the fallout: " + ", ".join(caps))
        if bound_cmd_sha:
            coverage_lines.append(f"  spec identity (cmd_sha): {bound_cmd_sha}")
        if bound_placement:
            coverage_lines.append(f"  placement (cluster): {', '.join(bound_placement)}")
            # The {cluster: cap} form's caps are structural (like budget_cap —
            # never spoken in the grant), but the human must READ them here:
            # consenting to a per-cluster ceiling they never saw is the exact
            # gap this inline coverage render exists to close.
            from hpc_agent.state.placement_drift import placement_cluster_caps

            per_cluster = placement_cluster_caps(res.get("placement"))
            for cluster, cluster_caps in sorted(per_cluster.items()):
                if cluster_caps:
                    rendered = ", ".join(
                        f"{field}={value}" for field, value in sorted(cluster_caps.items())
                    )
                    coverage_lines.append(f"    {cluster} caps: {rendered}")
        # A READY-TO-PASTE grant line carrying every token the chat tier reads
        # (boundary slug, each heal class, an 8+ hex sha prefix, every cluster in
        # the set). Rendering it is consistent with the tier's own design: the
        # sha-prefix leg's temporal binding ALREADY assumes the human copies the
        # token from this brief (vocabulary impossibility, the R6 idiom) — the
        # binding lives in the tokens, not in the phrasing, so handing the human
        # the full sentence changes the typing burden, not the trust story.
        # Rendered through the ONE grant-vocabulary home (``ops/overnight.py``)
        # shared with the park-time OFFER (``block_drive``'s answer menu), so
        # the refusal's line and the offered line can never drift apart.
        grant_line = _overnight.render_grant_line(
            scope_kind=spec.scope_kind,
            scope_id=spec.scope_id,
            heal_classes=consent_classes,
            cmd_sha=bound_cmd_sha,
            placement=bound_placement or (),
            expires_at=composed_expires if isinstance(composed_expires, str) else None,
        )
        _refuse_missing_authorship(
            "overnight-consent authorship gate: a standing consent accepts the "
            "fallout of unattended overnight advances. No bound consent record "
            "and no logged human utterance covers this boundary:\n"
            + "\n".join(coverage_lines)
            + "\nTo GRANT: type the consent yourself, in your own words, in chat "
            f"— name the boundary ({spec.scope_id})"
            + (
                f", each repair class ({', '.join(sorted(consent_classes))})"
                if consent_classes
                else ""
            )
            + (
                f", and the cmd_sha by at least its first 8 hex characters ({bound_cmd_sha[:12]}…)"
                if bound_cmd_sha
                else ""
            )
            + (f", and the cluster set ({', '.join(bound_placement)})" if bound_placement else "")
            + ". A bare 'y', a clicked option, or an agent-relayed response "
            "cannot stand in for it.\n"
            "Or copy-paste this grant line into chat (edit freely — the gate "
            "reads the named tokens, not the phrasing):\n"
            f"  {grant_line}"
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
