"""The notebook section sign-off gate (T8) — recompute locks + the tiered
authorship bar over a ``notebook-sign-off`` record."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput

from ._shared import (
    _fresh_human_texts,
    _is_bare_ack,
    _names_slug,
    _read_interview_actors,
    _refuse_missing_authorship,
    _session_actor,
)

# ── notebook sign-off authorship gate (D5 three locks + D-attention, T8) ──────

# The block-terminator convention for a notebook section SIGN-OFF. A sign-off
# ATTESTS that a human reviewed a section AT A SPECIFIC HASH; it is a HUMAN
# attestation over the ``notebook`` scope, journaled under this distinct block so
# the gate can recognise — and lock — it (mirrors the ``scope-unlock`` block
# convention). Lock 1 (no affordance) is organizational: there is NO sign-off
# verb, chain, or next_block — append-decision under this block is the ONLY write
# path (pinned by the contract test in tests/contracts/).
_SIGNOFF_BLOCK = "notebook-sign-off"

# Identifier-shaped tokens: the substrate for the raised human-required bar and
# the diff-token pool. Mirrors T5's assertion/diff vocabulary (plain identifiers).
_SIGNOFF_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _signoff_token_names(text: str) -> set[str]:
    """The identifier tokens in *text*, lowercased (the #26 token-exact idiom).

    Splits on non-identifier chars exactly like :func:`_prior_nudge_named`, so a
    sign-off response must NAME a thing, never merely contain it as a substring.
    """
    return set(re.split(r"[^a-z0-9_]+", (text or "").lower())) - {""}


def _signoff_fresh_human_texts(
    experiment_dir: Path,
    *,
    actor_ids: list[str],
    audit_id: str,
    section: str,
    view_sha: str,
) -> list[str] | None:
    """The utterance-log evidence pool for ONE sign-off, TEMPORALLY BOUND.

    The actor-scoped log read (:func:`_actor_scoped_human_texts` semantics)
    plus the run-#12 finding-10 filter: a human can only attest a view that
    existed when they typed, so a candidate utterance must be logged at or
    after the signed view's render file was written (its mtime, floored to
    whole seconds — utterance ``ts`` is seconds-resolution). This kills the
    standing-sign-off class: a kickoff / resume prompt that happened to name
    the slug and a diff identifier minutes before the render existed is not
    attestation, and letting it pass is what kept the sign-off popup from
    ever firing (the gate passed instead of refusing).

    ``None`` — no log at all, or an unattributed >1-actor session — falls to
    the friction tier exactly like the unscoped read. An EMPTY list is
    different: the log exists but nothing fresh names this sign-off, so the
    gate refuses with the authorship marker (the popup's cue). An absent /
    unstatable render SKIPS the filter (returns the unfiltered pool): the
    missing-render refusal belongs to the UNMARKED trusted-display lock,
    where re-eliciting an utterance cannot fix it. A record with no
    parseable ``ts`` is excluded — conservative; the popup remedies.

    The temporal filter itself is the ONE shared :func:`_fresh_human_texts`
    helper (the B4 fix-wave generalized this finding-10 pattern); this function
    only computes the render-mtime *anchor* and delegates. An absent /
    unstatable render yields ``anchor=None`` → the unfiltered pool, exactly the
    original missing-render posture.
    """
    from hpc_agent.ops import notebook_view as _notebook_view

    render = _notebook_view.render_path(
        experiment_dir, audit_id=audit_id, section=section, view_sha=view_sha
    )
    try:
        anchor: float | None = int(render.stat().st_mtime)
    except OSError:
        anchor = None
    return _fresh_human_texts(experiment_dir, actor_ids=actor_ids, anchor=anchor)


def _section_specific_tokens(section_view: Any) -> set[str]:
    """The identifier pool the raised human-required bar checks a sign-off against.

    Drawn from the section's DIFF-CHANGED lines (``+``/``-`` bodies, skipping the
    unified-diff ``+++``/``---`` file headers and ``@@`` hunk markers) AND — the
    full-view-recompute addition — from its LINT FLAGS (the identifier tokens in
    each finding's ``detail`` + ``evidence``), so a section made human-required
    SOLELY by a lint flag (an inherited section with no diff and no assertions, e.g.
    a data path under ``input_roots`` that vanished) still demands the human ENGAGE
    the flagged specific, not offer generic praise. Falls back to the section's
    declared ASSERTION identifiers when both the diff and the flags are empty (a
    human-required-but-inherited section whose assertions are ungreen has no diff
    tokens); when ALL are empty the bar reduces to the slug-naming floor already
    enforced (a token that does not exist cannot be demanded).
    """
    tokens: set[str] = set()
    for line in section_view.diff:
        if not line or line.startswith(("+++", "---", "@@")):
            continue
        if line[0] in "+-":
            tokens |= {m.group(0).lower() for m in _SIGNOFF_IDENT_RE.finditer(line[1:])}
    for flag in section_view.lint_flags:
        detail = str(flag.get("detail") or "") if isinstance(flag, dict) else ""
        evidence = flag.get("evidence") if isinstance(flag, dict) else None
        evidence_text = json.dumps(evidence, default=str) if evidence else ""
        tokens |= {
            m.group(0).lower() for m in _SIGNOFF_IDENT_RE.finditer(f"{detail} {evidence_text}")
        }
    if not tokens:
        for assertion in section_view.assertions:
            tokens |= {m.group(0).lower() for m in _SIGNOFF_IDENT_RE.finditer(assertion.test)}
    return tokens


def _read_interview_audited_source(
    experiment_dir: Path, audit_id: str | None
) -> dict[str, Any] | None:
    """The interview.json ``audited_source`` block matching *audit_id*, or ``None``.

    The canonical location is the campaign-dir root (where ``interview`` writes
    it); ``.hpc/interview.json`` is accepted defensively (the ``detect_entry_point``
    posture). A corrupt / non-object file is tolerated as "absent" here — the
    caller then refuses on an unresolvable SOURCE, which is the load-bearing loud
    failure; a duplicate refusal on the JSON shape would only muddy the message.
    """
    for rel in ("interview.json", ".hpc/interview.json"):
        path = experiment_dir / rel
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        block = doc.get("audited_source")
        if isinstance(block, dict) and block.get("audit_id") == audit_id:
            return block
    return None


def _read_signoff_source_text(experiment_dir: Path, rel: str, *, required: bool) -> str | None:
    """Read a source/template ``.py`` at *rel* (relative to *experiment_dir*).

    A missing/unreadable REQUIRED source raises (a sign-off that cannot be
    recomputed is refused, never skipped); a missing template returns ``None`` so
    the caller conservatively treats a template-less audit as HUMAN-REQUIRED.
    """
    path = experiment_dir / rel
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        if required:
            raise errors.SpecInvalid(
                f"notebook sign-off gate: audited source {rel!r} is unreadable "
                f"({exc}). A sign-off RECOMPUTES the section hash from the .py on "
                "disk — an unresolvable source is refused, never skipped."
            ) from exc
        return None


def _resolve_signoff_audit_config(
    experiment_dir: Path, resolved: dict[str, Any]
) -> tuple[str, str, Any]:
    """Resolve ``(source_relpath, template_relpath, AuditConfig)`` for a sign-off.

    The full-view-recompute upgrade resolves the whole CANONICAL audit
    configuration, not just the source/template text:

    * **source / template relpaths** — an explicit ``resolved["source"]`` /
      ``resolved["template"]`` wins; otherwise the interview.json
      ``audited_source`` block (matched by ``audit_id``) supplies them. BOTH must
      resolve now: recomputing ``view_sha`` needs the template as much as the
      source (the diff-from-template is a view ingredient), and every sanctioned
      ``view_sha`` was produced against a real template (``notebook-audit-view``
      requires one), so a template that cannot be resolved means the signed view
      is not reproducible — refused loudly, never a conservative silent pass.
    * **lint roots + attention order** — the recorded audit configuration read
      from the same ``audited_source`` block (``read_recorded_config``); a block
      predating the config fields yields the conservative defaults (empty roots,
      source order), exactly the posture the gate used before the config was
      persisted.

    An unresolvable source or template is REFUSED loudly (this is the opted-in
    surface: recompute or refuse, never pass).
    """
    from hpc_agent.ops import notebook_view as _notebook_view

    audit_id = resolved.get("audit_id")
    src_rel = resolved.get("source")
    tmpl_rel = resolved.get("template")
    if not src_rel or not tmpl_rel:
        block = _read_interview_audited_source(experiment_dir, audit_id)
        if block is not None:
            src_rel = src_rel or block.get("source")
            tmpl_rel = tmpl_rel or block.get("template")
    # ONE-SHOT refusal (run-#12 latency exhibit: agents discovered source and
    # template one bounce at a time, three appends per sign-off): name EVERY
    # unresolvable ingredient in a single refusal, with the complete resolved
    # skeleton the retry needs.
    unresolved = [
        name
        for name, value in (("source", src_rel), ("template", tmpl_rel))
        if not isinstance(value, str) or not value
    ]
    if unresolved:
        raise errors.SpecInvalid(
            f"notebook sign-off gate: could not resolve {' + '.join(unresolved)} for "
            f"audit_id={audit_id!r} — not in resolved{{...}} and no matching "
            "interview.json audited_source block. The gate recomputes the section "
            "hash from the source and rebuilds the canonical view against the "
            "template, so both are required. Retry with the COMPLETE resolved "
            "skeleton: {audit_id, section, section_sha, view_sha, "
            "source: <audited .py relpath>, template: <template .py relpath>}."
        )
    assert isinstance(src_rel, str) and isinstance(tmpl_rel, str)  # narrowed above
    cfg = _notebook_view.read_recorded_config(experiment_dir, audit_id)
    return src_rel, tmpl_rel, cfg


def _assert_signoff_render_current(
    experiment_dir: Path,
    *,
    audit_id: str,
    section: str,
    view_sha: str,
    recomputed_section_sha: str,
) -> None:
    """The TRUSTED-DISPLAY lock: the render for what-the-human-saw must be CURRENT.

    The audit view an agent relays in chat is model-carried and unforceable; the
    trusted artifact is the CONTENT-ADDRESSED render file code wrote
    (``ops/notebook/render_store.py``, the v1.5 trusted-display lock, user-approved
    2026-07-07). This gate leg makes a sign-off unlandable unless that artifact
    exists on disk AND was produced against CURRENT source.

    Recorded boundary (the v1.5 drift log): the gate CANNOT recompute ``view_sha``
    — the view depends on lint findings the gate does not have (the ``view_sha``-is-
    provenance paragraph). So the check is a cross-reference, not a re-derivation:
    the render addressed by the RESOLVED ``view_sha`` must exist, parse, and its
    header must agree on ``view_sha`` + ``section``, and — the freshness leg — its
    header ``section_sha`` must equal the gate's FRESHLY RECOMPUTED section sha. An
    edit after the render moves the recomputed sha, so a stale render's header sha
    no longer matches and the sign-off is refused (the record's own asserted sha is
    already covered by the ``attestation.bind`` lock; this closes the case where the
    record sha was updated but the render step was never re-run).

    The check is reached through the top-level ``notebook_view`` facade — the direct
    ``hpc_agent.ops.notebook.render_store`` spelling trips the subject-import lint
    from inside the ``decision`` subject (the ``audit_view``/``field_ownership``
    precedent). Same trust model as every store: the render file is code-written, so
    tool-surface enforcement is the guarantee and filesystem forgery is out of scope
    (the honest-limit paragraph). Applied to redundant (auto-cleared) sign-offs too:
    the human claims to have reviewed, so the trusted artifact must exist.

    Raises :class:`errors.SpecInvalid` naming the missing/stale render path.
    """
    from hpc_agent.ops import notebook_view as _notebook_view

    path = _notebook_view.render_path(
        experiment_dir, audit_id=audit_id, section=section, view_sha=view_sha
    )
    header = _notebook_view.read_render_header(path)
    if header is None:
        raise errors.SpecInvalid(
            "notebook sign-off gate (trusted-display lock): no parseable render "
            f"artifact for what-the-human-saw at {path} — the audit view relayed in "
            "chat is model-carried and unforceable, so a sign-off requires the "
            "code-written, content-addressed render file. Re-run notebook-audit-view "
            "to produce it against the current source, then sign again."
        )
    if header.get("view_sha") != view_sha or header.get("section") != section:
        raise errors.SpecInvalid(
            "notebook sign-off gate (trusted-display lock): the render artifact at "
            f"{path} does not match the signed view (its header names "
            f"section={header.get('section')!r} / view_sha={header.get('view_sha')!r}, "
            f"the sign-off binds section={section!r} / view_sha={view_sha!r}). "
            "Re-run notebook-audit-view for this section and sign the fresh view."
        )
    if header.get("section_sha") != recomputed_section_sha:
        raise errors.SpecInvalid(
            "notebook sign-off gate (trusted-display lock): the render artifact at "
            f"{path} is STALE — its header section_sha ({header.get('section_sha')}) "
            f"does not match the current source ({recomputed_section_sha}). The source "
            "was edited after the render, so what-the-human-saw no longer reflects the "
            "code being signed. Re-run notebook-audit-view against the current source, "
            "then sign again."
        )


def _assert_signoff_reviewer_not_author(
    experiment_dir: Path, *, audit_id: str, section: str, section_sha: str
) -> None:
    """MH6 reviewer≠author gate — refuse a self-sign under >1 declared actors.

    Active ONLY when interview.json declares >1 actor (MH1); otherwise the gate
    does not exist and this returns silently, byte-identical to today (no draft
    lookup, no actor resolution, no new refusal). Under >1 actor, three refusals,
    all the loud/dangling-reference posture (NOT D7 silence, NOT the E2
    authorship-missing marker — a re-elicited utterance cannot fix a config /
    attribution gap; the remedy is a config or a recorded draft, not a sentence):

    * **No resolvable session actor** — an anonymous sign-off in a
      declared-multi-actor experiment is the laundering channel (sign as nobody,
      be everybody). Refused naming ``HPC_ACTOR``.
    * **No current draft attribution** — the author is the ``attestor_id`` of the
      newest ``notebook-draft`` attestation whose ``content_sha`` equals the
      FRESHLY RECOMPUTED *section_sha* (:func:`state.notebook_audit.read_draft_author`
      — routed through the ONE reducer, so a redrafted section's stale draft is no
      attribution). A missing attribution is REFUSED, not skipped: an unattributed
      section makes self-review undetectable by omission (draft, skip the draft
      record, self-sign). The refusal names the remedy (record the draft).
    * **signer == author** — the drafter's actor cannot sign their own section.
      Pure identity over opaque slugs (Q1-clean); core never knows WHY the lab
      wants this, it compares ids.
    """
    ids, _ = _read_interview_actors(experiment_dir)
    if len(ids) <= 1:
        return  # the gate does not exist under zero/one declared actor
    from hpc_agent.state.notebook_audit import read_draft_author

    signer = _session_actor(experiment_dir, ids)
    if signer is None:
        raise errors.SpecInvalid(
            "notebook sign-off gate (MH6 reviewer≠author): >1 actor is declared "
            f"but this session has no resolvable actor, so section {section!r} would "
            "be signed by nobody — an anonymous act in a declared-multi-actor "
            "experiment is the laundering channel. Configure HPC_ACTOR to a declared "
            "actor before signing."
        )
    author = read_draft_author(experiment_dir, audit_id, section, current_sha=section_sha)
    if author is None:
        raise errors.SpecInvalid(
            "notebook sign-off gate (MH6 reviewer≠author): section "
            f"{section!r} has NO current draft attribution at its recomputed sha "
            "(no `notebook-draft` record for this content), so an unattributed "
            "section could be self-reviewed by omission. Record the draft (the "
            "notebook-draft verb, part of the audit prelude) before signing."
        )
    if signer == author:
        raise errors.SpecInvalid(
            "notebook sign-off gate (MH6 reviewer≠author): the drafter's actor "
            f"({author!r}) cannot sign off their own section {section!r} — a sign-off "
            "by the drafting actor is self-review wearing a review's clothes. A "
            "DIFFERENT declared actor must review and sign."
        )


def _assert_signoff_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """Human-authorship + recompute gate for a NOTEBOOK section sign-off (T8).

    A sign-off is an ordinary ``append-decision`` whose ``scope_kind=="notebook"``,
    ``block=="notebook-sign-off"``, and ``resolved={audit_id, section, section_sha,
    view_sha}`` (D3). It ATTESTS that a human reviewed one section at a specific
    hash — a HUMAN attestation, so it faces both the un-fakeable recompute lock and
    the tiered authorship bar (``docs/design/notebook-audit.md`` D5 + D-attention).

    Block convention, enforced both directions (mirrors ``scope-unlock``): a
    ``notebook-sign-off`` block is refused for any ``scope_kind`` other than
    ``notebook``; every other record passes untouched.

    **Lock 1 (no affordance)** is organizational: there is no sign-off verb / chain
    / next_block — append-decision under this block is the only write path. Pinned
    by the contract test in ``tests/contracts/`` (no primitive is named sign-off).

    **Lock 2 (recompute, un-fakeable)** — the audited ``.py`` is resolved (from
    ``resolved['source']`` or the interview.json ``audited_source`` block), parsed
    (:func:`parse_percent_source`), the named section located, and its
    ``section_sha`` RECOMPUTED. The record binds through the ONE attestation kernel
    (``state.attestation.bind``, D5 lock 2 extracted once): the asserted
    ``section_sha`` must equal the recomputed one or the append is refused — a hash
    cannot be asserted into existence. An unresolvable source / missing section is
    REFUSED loudly, never skipped.

    **Lock 3 (authorship bar, D-attention tiered)** — bare acks are refused
    (:func:`_is_bare_ack`); the sign-off must NAME the section slug (token-exact,
    the #26 precedent). EVIDENCE IS TIERED like the unlock gate (run-#12
    finding 9, closing the run-#11 composed-response laundering hole): with a
    harness utterance log present the naming/engagement legs run over LOGGED
    HUMAN UTTERANCES — chat (capture hook) or the sign-off popup (the E4
    elicitation handler appends to the same log, which is what lets the MCP
    retry-once land) — and the agent-relayed ``response`` carries no authorship
    weight; absent a log the non-bare ``response`` is the friction tier
    (byte-identical v1). Log-tier candidates are TEMPORALLY BOUND (finding 10):
    only utterances logged after the signed view's render was written count —
    a prior prompt that happened to name the slug is not attestation
    (:func:`_signoff_fresh_human_texts`). The tier is RECOMPUTED here over the CANONICAL view
    (:func:`~hpc_agent.ops.notebook.canonical.build_canonical_view`) — with the
    REAL lint findings (recomputed server-side from the recorded roots), the
    journaled fresh receipts, and the recorded attention order. The v1 "statically
    recomputable legs only" boundary is RETIRED: a section made human-required
    *solely* by a lint flag IS now distinguished here (the lint is cheap local
    static analysis; the receipts are journaled; the roots are persisted on the
    ``audited_source`` block). For a **HUMAN_REQUIRED** section the bar RAISES: the
    response must additionally ENGAGE the change — contain at least one identifier
    drawn from the section's diff-changed lines (:func:`_section_specific_tokens`).
    This is the boundary-drift defense: soften the human-required tier only via a
    richer harness-captured utterance, never a bare ack.

    **AUTO_CLEARED + a human sign-off: ACCEPT, but mark ``resolved['redundant'] =
    True``.** The alternative (refuse) was rejected: refusing a human's VOLUNTARY
    review would delete information and create a verb-shaped affordance gap
    (a human who looked would have no way to record it). Marking keeps the
    attention ledger honest — the record shows a real human sign-off that the
    tiering deemed unnecessary. The recompute lock and the base authorship floor
    (non-bare, slug-named) still apply to a redundant sign-off; only the raised
    diff-token bar is waived (an auto-cleared section has no change to engage).

    **TEMPLATE required (full-view recompute).** The canonical view is a
    diff-from-template projection, so a template that cannot be resolved means the
    signed ``view_sha`` is not reproducible — REFUSED loudly
    (:func:`_resolve_signoff_audit_config`), never a conservative empty-template
    pass. Every sanctioned ``view_sha`` was produced against a real template
    (``notebook-audit-view`` requires one), so an absent template at append time is
    a broken setup.

    **view_sha is RECOMPUTED (the defect this gate fixes).** The full-view
    recompute rebuilds the canonical section view and REFUSES unless the section's
    recomputed ``view_sha`` equals the resolved one. Because the section body is
    already confirmed current (the ``section_sha`` bind) and the render is confirmed
    current, a ``view_sha``-only mismatch pinpoints a moved VIEW ingredient — a
    changed lint finding (e.g. a vanished data path under the recorded
    ``input_roots``), a changed journaled receipt, or a changed attention order —
    and the refusal says so.

    **Trusted-display lock (v1.5)** — the audit view an agent relays in chat is
    model-carried and unforceable, so a sign-off additionally requires the
    CONTENT-ADDRESSED render file code wrote (:func:`_assert_signoff_render_current`,
    over ``ops/notebook/render_store.py``): the render addressed by the resolved
    ``view_sha`` must exist, parse, agree on ``view_sha``/``section``, and carry a
    header ``section_sha`` equal to the FRESHLY RECOMPUTED ``sect.section_sha`` — so
    an edit-after-render (render stale vs the recomputed sha) is refused even though
    the record's own asserted sha is already covered by the bind lock. Because the
    gate can't recompute ``view_sha`` (the recorded boundary), the render's header is
    the cross-reference; applied BEFORE the tier branch so redundant/auto-cleared
    sign-offs need the artifact too.

    Raises :class:`errors.SpecInvalid` on any refusal.
    """
    is_signoff_block = spec.block == _SIGNOFF_BLOCK

    # Block convention: notebook-sign-off is a notebook-only action.
    if is_signoff_block and spec.scope_kind != "notebook":
        raise errors.SpecInvalid(
            f"block {_SIGNOFF_BLOCK!r} is only valid for scope_kind='notebook' "
            f"(a notebook section sign-off); got scope_kind={spec.scope_kind!r}."
        )
    if not (is_signoff_block and spec.scope_kind == "notebook"):
        return  # not a notebook sign-off — nothing to gate

    if not isinstance(resolved, dict):
        raise errors.SpecInvalid(
            "notebook sign-off gate: resolved must carry "
            "{audit_id, section, section_sha, view_sha}."
        )

    audit_id = resolved.get("audit_id")
    section = resolved.get("section")
    section_sha = resolved.get("section_sha")
    view_sha = resolved.get("view_sha")
    missing = [
        name
        for name, value in (
            ("audit_id", audit_id),
            ("section", section),
            ("section_sha", section_sha),
            ("view_sha", view_sha),  # binds what-the-human-saw (D5); required.
        )
        if not isinstance(value, str) or not value
    ]
    if missing:
        raise errors.SpecInvalid(
            "notebook sign-off gate: resolved must carry non-empty "
            f"{{audit_id, section, section_sha, view_sha}}; missing/empty: {missing}. "
            "view_sha binds what-the-human-saw into the record (D5) and is required."
        )
    assert isinstance(section, str) and isinstance(section_sha, str)
    assert isinstance(audit_id, str) and isinstance(view_sha, str)

    # Base authorship floor — TIERED exactly like the unlock gate (the shared
    # lock tier, extended to T8): with a harness utterance log present the legs
    # run over LOGGED HUMAN UTTERANCES — text a human verifiably typed, in chat
    # (the UserPromptSubmit capture hook) or in the sign-off POPUP (the E4
    # elicitation handler appends to the SAME log, which is what lets the
    # retry-once land on the human's words) — and the agent-relayed ``response``
    # carries no authorship weight (the run-#11 laundering finding: a composed
    # response passes the token checks mechanically but attests nothing).
    # Absent a log (older harness / no capture hook), the non-bare ``response``
    # is the human's typed sign-off — the v1 friction tier, byte-identical.
    # MH4: >1 declared actors scope the read to the session actor's log only;
    # an unattributed session falls to the friction tier, never the union.
    response = str(spec.response or "")
    _signoff_actor_ids, _signoff_policy = _read_interview_actors(experiment_dir)
    _signoff_harness_texts = _signoff_fresh_human_texts(
        experiment_dir,
        actor_ids=_signoff_actor_ids,
        audit_id=audit_id,
        section=section,
        view_sha=view_sha,
    )
    if _signoff_harness_texts is None:
        if _is_bare_ack(response):
            _refuse_missing_authorship(
                "notebook sign-off gate: signing off a section is a HUMAN act — a bare "
                f"{spec.response!r} (a 'y' / click) cannot sign off. Name the section "
                f"({section!r}) and state what you reviewed."
            )
        if not _names_slug(response, section):
            _refuse_missing_authorship(
                "notebook sign-off gate: the sign-off response must NAME the section "
                f"slug {section!r} (token-exact, the #26 precedent) — a generic ack "
                "cannot attest a specific section. Restate, naming the section."
            )
        signoff_candidates = [response]
    else:
        signoff_candidates = [
            text
            for text in _signoff_harness_texts
            if not _is_bare_ack(text) and _names_slug(text, section)
        ]
        if not signoff_candidates:
            _refuse_missing_authorship(
                "notebook sign-off gate: signing off a section is a HUMAN act — no "
                f"logged human utterance NAMES the section slug {section!r} "
                "(token-exact, the #26 precedent). The human types the sign-off in "
                "their own words (in chat, or in the sign-off popup when it opens); "
                "an agent-relayed response carries no authorship weight here."
            )

    # Lazy, subject-lint-safe imports (state.* is allowed substrate; the ops
    # notebook subject is reached through the top-level ``notebook_view`` facade).
    from hpc_agent.ops import notebook_view as _notebook_view
    from hpc_agent.state import attestation
    from hpc_agent.state.audit_source import parse_percent_source

    # Resolve the CANONICAL audit configuration: source/template relpaths + the
    # recorded lint roots + attention order (the ingredients of the view_sha).
    source_relpath, template_relpath, cfg = _resolve_signoff_audit_config(experiment_dir, resolved)

    # Lock 2 — recompute the section hash from the .py on disk and bind through
    # the ONE attestation kernel (D5 lock 2). Refuses an unresolvable source.
    source_text = _read_signoff_source_text(experiment_dir, source_relpath, required=True)
    assert source_text is not None  # required=True raises rather than returning None
    parsed = parse_percent_source(source_text)
    sect = next((s for s in parsed.sections if s.slug == section), None)
    if sect is None:
        raise errors.SpecInvalid(
            f"notebook sign-off gate: section {section!r} not found in the audited "
            f"source (audit_id={audit_id!r}). A sign-off must name a CURRENT section "
            "— re-view the source and sign an existing section."
        )
    attestation.bind(
        {
            "attestor": "human",
            "subject_kind": _notebook_view.SUBJECT_KIND,
            "subject_id": f"{audit_id}:{section}",
            "content_sha": section_sha,
            "view_sha": view_sha,
        },
        recompute=sect.section_sha,
    )

    # Trusted-display lock (v1.5) — the CONTENT-ADDRESSED render for
    # what-the-human-saw must exist and be CURRENT. Reuses the freshly-recomputed
    # ``sect.section_sha`` (never a second recompute). Applied BEFORE the tier
    # branch so it covers redundant/auto-cleared sign-offs too (the human claims a
    # review; the trusted artifact must exist).
    _assert_signoff_render_current(
        experiment_dir,
        audit_id=audit_id,
        section=section,
        view_sha=view_sha,
        recomputed_section_sha=sect.section_sha,
    )

    # FULL-VIEW RECOMPUTE (the "statically-recomputable legs only" boundary is
    # RETIRED). Build the CANONICAL view SERVER-SIDE — real lint findings from the
    # recorded roots, journaled fresh receipts, recorded attention order (one
    # definition: ``build_canonical_view``, shared with the verbs + the plugin) —
    # and REFUSE unless the section's recomputed view_sha equals the resolved one.
    # The section body is already confirmed current (the bind lock) and the render
    # is confirmed current, so a view_sha-ONLY mismatch means a VIEW ingredient
    # moved: a lint finding changed (a data path under input_roots vanished /
    # appeared), a journaled receipt changed, or the attention order changed.
    view = _notebook_view.build_canonical_view(
        experiment_dir,
        audit_id=audit_id,
        source_relpath=source_relpath,
        template_relpath=template_relpath,
        cfg=cfg,
    )
    section_view = next((v for v in view.sections if v.slug == section), None)
    if section_view is None:
        raise errors.SpecInvalid(
            f"notebook sign-off gate: section {section!r} is not in the recomputed "
            f"canonical view (audit_id={audit_id!r}). Re-run notebook-audit-view and "
            "sign a current section."
        )
    if section_view.view_sha != view_sha:
        raise errors.SpecInvalid(
            "notebook sign-off gate (full-view recompute): the section body is "
            "unchanged (the section_sha bind passed) but the recomputed canonical "
            f"view_sha ({section_view.view_sha}) does not equal the signed view_sha "
            f"({view_sha}). An ingredient of the VIEW moved since it was rendered — a "
            "lint finding changed (e.g. a data path under the recorded input_roots "
            "vanished or appeared), a journaled render receipt changed, or the "
            "attention order changed. Re-run notebook-audit-view for section "
            f"{section!r}, re-inspect the fresh view, and sign THAT view_sha."
        )
    # MH6 (reviewer ≠ author): under >1 declared actors a sign-off may not be
    # authored by the SECTION'S DRAFTER — self-review wearing a review's clothes.
    # Applied BEFORE the tier branch so it covers the redundant (auto-cleared)
    # path too: a redundant self-review is still recorded self-review. Silent under
    # zero/one declared actor (the gate does not exist there — byte-identical).
    _assert_signoff_reviewer_not_author(
        experiment_dir, audit_id=audit_id, section=section, section_sha=sect.section_sha
    )

    # The tier is now REAL — recomputed with the REAL lint flags (the recorded
    # conservative-floor gap is closed). A section made human-required SOLELY by a
    # lint flag is now distinguished here.
    tier = section_view.tier

    if tier == _notebook_view.AUTO_CLEARED:
        # ACCEPT a voluntary human sign-off of an auto-cleared section, but mark it
        # redundant (decision recorded in the docstring). Mutating ``resolved`` in
        # place is visible to the append that follows (same dict object).
        resolved["redundant"] = True
        return

    # HUMAN_REQUIRED — raise the bar: the response must engage a section specific.
    # The slug's OWN tokens are subtracted from both sides: naming the section
    # (already required) must not double as "engaging the change", or a slug like
    # ``model-fit`` whose fragments appear in the diff line would satisfy the bar
    # by itself and the raise would be a no-op.
    slug_tokens = _signoff_token_names(section)
    raw_specifics = _section_specific_tokens(section_view) if section_view is not None else set()
    specifics = raw_specifics - slug_tokens
    # The engaging text must be one that ALSO names the slug (the tiered
    # candidates): a slug-naming utterance and a separate token-dropping one
    # cannot combine into an attestation neither made alone.
    engaged = any(
        (_signoff_token_names(text) - slug_tokens) & specifics for text in signoff_candidates
    )
    if specifics and not engaged:
        _refuse_missing_authorship(
            f"notebook sign-off gate: section {section!r} is HUMAN-REQUIRED "
            "(nonempty diff-from-template / lint flags / ungreen assertions), so the "
            "sign-off must ENGAGE the change — name at least one identifier from the "
            "section's diff, not offer a generic ack (soften only via a richer "
            "utterance, never a bare ack; the boundary-drift flag). Identifiers in "
            f"the change include: {sorted(specifics)[:8]}."
        )
