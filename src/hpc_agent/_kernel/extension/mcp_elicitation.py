"""Elicitation prompt + render-digest composition for the MCP server.

The bytes-in / typed-utterance-out half of :mod:`hpc_agent._kernel.extension.mcp_server`
(``docs/design/mcp-elicitation.md``): everything that COMPOSES what a human sees in
an ``elicitation/create`` popup and what a captured reply is BOUND to, kept as pure,
transport-free functions so they unit-test without a stdio pump.

Two clusters live here:

* **Prompt composition** — :func:`_render_elicitation_prompt` assembles the signing
  dialog from CODE-SELECTED identifiers only (never model free text), routing an
  overnight standing-consent block to :func:`_render_overnight_consent_block` and a
  notebook sign-off to the render digest. :func:`_overnight_consent_binding` builds
  the ``bound`` coverage a captured overnight utterance is pinned to.
* **Render digest** — :func:`_render_digest_block` reads the content-addressed render
  off disk and emits a bounded, honesty-capped signing digest (:data:`_DIGEST_BLOCK_MAX_BYTES`),
  with the diff body riding in its own bounded block (:func:`_render_diff_body_lines`,
  :data:`_DIFF_EMBED_MAX_BYTES`) and :func:`_tier_trigger_headline` naming why a
  human's judgment is required.

Plus the two response-side helpers the firing site uses: :func:`_accepted_utterance`
(the filtered human-typed text, or the decline-equivalent ``None``) and
:func:`_with_capture_markers` (the fingerprint the retried result carries — never the
words). This module has NO dependency on the server class or the JSON-RPC pump; the
server imports FROM here and re-exports these symbols so their historical
``mcp_server.<name>`` import paths continue to resolve.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def _render_elicitation_prompt(
    arguments: Mapping[str, Any], experiment_dir: Path | None = None
) -> str:
    """Build the elicitation prompt from CODE-SELECTED identifiers + a code-read
    render digest (D5 + E-render).

    The prompt is assembled from fixed instructional text plus the journal-derived
    identifiers the gate itself token-matches — the block name, the scope kind/id,
    and (for a notebook sign-off) the section slug. It NEVER embeds any
    model-authored free text from the tool arguments: not the ``proposal``, not the
    ``response``, not the ``evidence_digest``. If the model could author the prompt
    it would bait the human's reply into the trust anchor — the laundering channel
    the authorship gate exists to close. The refusal envelope's own ``message`` is
    deliberately NOT interpolated either: some authorship-gate messages quote the
    model's ``response`` (the bare-ack refusal names it), so echoing the message
    would reopen the same laundering channel.

    E-render (``docs/design/mcp-elicitation.md``, SHIPPED 2026-07-09): when the
    refusal is a NOTEBOOK sign-off, the server reads the section's content-addressed
    render off disk and embeds a CODE-COMPUTED digest (diff stats, assert table,
    lint-flag count) + the ``view_sha12`` — code-read bytes in, typed utterance out,
    one channel. Trust is unchanged: the digest is derived from the code-authored
    render (the trusted-display artifact the T8 gate binds), NEVER from the notebook
    source or any model text (RULING 1: digest, not full render — the full render
    stays on disk for the Read pane). Missing/stale render → an explicit,
    reason-disclosing fallback line, never an unmarked silent omission and never a
    crash.
    """
    spec = arguments.get("spec")
    spec = spec if isinstance(spec, dict) else {}
    block = str(spec.get("block") or "").strip()
    scope_kind = str(spec.get("scope_kind") or "").strip()
    scope_id = str(spec.get("scope_id") or "").strip()
    resolved = spec.get("resolved")
    resolved = resolved if isinstance(resolved, dict) else {}
    section = ""
    view_sha = ""
    if scope_kind == "notebook":
        raw_section = resolved.get("section")
        section = str(raw_section).strip() if isinstance(raw_section, str) else ""
        raw_view_sha = resolved.get("view_sha")
        view_sha = str(raw_view_sha).strip() if isinstance(raw_view_sha, str) else ""

    # Overnight standing consent (USER RULING 3, 2026-07-12): the popup IS the
    # binding surface — it names EXACTLY what the consent covers (the boundary,
    # the repair classes, the caps + morning boundary) so the human's typed reply
    # is captured BOUND to that coverage (docs/design/bound-capture.md). The
    # binding itself is built by ``_overnight_consent_binding``; here we render
    # what it covers, from code-selected identifiers only.
    if block == _OVERNIGHT_CONSENT_BLOCK and scope_kind in ("run", "campaign"):
        return "\n".join(_render_overnight_consent_block(scope_kind, scope_id, resolved))

    lines = ["Your sign-off must be typed by you, in your own words."]
    if scope_kind and scope_id:
        lines.append(f"Decision scope: {scope_kind} {scope_id}.")
    if block:
        lines.append(f"Block awaiting sign-off: {block}.")
    if section:
        lines.append(f"Notebook section to name in your sign-off: {section}.")
    if scope_kind == "notebook":
        lines.extend(
            _render_digest_block(
                experiment_dir, audit_id=scope_id, section=section, view_sha=view_sha
            )
        )
    lines.append(
        "Type what you reviewed and your decision, in your own words. A bare "
        "'y' or a clicked option cannot stand in for it."
    )
    return "\n".join(lines)


# The overnight standing-consent block terminator, duplicated as a plain literal
# (like ``ops/overnight._DEFAULT_CHAIN_TICK_SECONDS``) so the elicitation firing
# site never imports the ops role-root at module load. Kept in lockstep with
# ``hpc_agent.ops.overnight.OVERNIGHT_CONSENT_BLOCK`` (a drift test could pin it).
_OVERNIGHT_CONSENT_BLOCK = "overnight-consent"


def _render_overnight_consent_block(
    scope_kind: str, scope_id: str, resolved: Mapping[str, Any]
) -> list[str]:
    """The coverage a standing-consent popup names, from code-selected identifiers.

    Names EXACTLY what the human's typed consent will be BOUND to (the same subset
    ``_overnight_consent_binding`` copies into the record): the boundary scope, the
    repair classes it authorizes (``heal_classes``), and the caps + morning
    boundary. Never embeds model free text (``response`` / ``proposal`` /
    ``evidence_digest``). The morning boundary shown is the code-composed default
    (:func:`ops.overnight.compose_consent_defaults`) so it matches what the gate
    records, even when the caller omitted ``expires_at``.
    """
    heal_classes = resolved.get("heal_classes")
    classes = (
        sorted(str(c) for c in heal_classes if isinstance(c, str))
        if isinstance(heal_classes, list)
        else []
    )
    try:
        from hpc_agent.ops.overnight import compose_consent_defaults

        composed = compose_consent_defaults(dict(resolved))
        expires_at = str(composed.get("expires_at") or "")
    except Exception:  # noqa: BLE001 — a compose failure must not wedge the popup
        expires_at = str(resolved.get("expires_at") or "")
    budget_cap = resolved.get("budget_cap")
    walltime_cap = resolved.get("walltime_cap")

    lines = [
        "Your OVERNIGHT consent must be typed by you, in your own words.",
        f"Boundary you are consenting to advance unattended: {scope_kind} {scope_id}.",
    ]
    lines.append(
        "Repair classes you authorize while you sleep: "
        + (", ".join(classes) if classes else "none (watcher re-arm only)")
        + "."
    )
    if expires_at:
        lines.append(f"Consent expires at the morning boundary: {expires_at}.")
    caps = []
    if isinstance(budget_cap, (int, float)) and not isinstance(budget_cap, bool):
        caps.append(f"budget_cap={budget_cap}")
    if isinstance(walltime_cap, (int, float)) and not isinstance(walltime_cap, bool):
        caps.append(f"walltime_cap={walltime_cap}s")
    if caps:
        lines.append("Hard caps on the fallout: " + ", ".join(caps) + ".")
    lines.append(
        "Type your consent, naming the boundary and the caps you accept, in your "
        "own words. A bare 'y' or a clicked option cannot stand in for it."
    )
    return lines


def _overnight_consent_binding(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``bound`` mapping a captured overnight-consent utterance carries, or ``None``.

    For an ``append-decision`` refusal on the ``overnight-consent`` block, binds the
    human's typed reply to the EXACT coverage the popup displayed
    (:func:`_render_overnight_consent_block`) so the gate's evidence is one exact
    lookup, never a word-overlap over the unbound chat stream (USER RULING 3,
    docs/design/bound-capture.md). Every value is a CODE-SELECTED identifier copied
    from the spec — the scope tuple, the ``heal_classes`` list, the ``cmd_sha``
    spec-identity, and a code-composed morning-boundary coverage window — NEVER
    model free text (``response`` / ``proposal`` / ``evidence_digest``), mirroring
    :func:`_render_elicitation_prompt`'s selection. ``None`` for any non-overnight
    refusal (a notebook / scope-unlock sign-off carries no overnight binding).
    """
    spec = arguments.get("spec")
    spec = spec if isinstance(spec, dict) else {}
    if str(spec.get("block") or "").strip() != _OVERNIGHT_CONSENT_BLOCK:
        return None
    scope_kind = str(spec.get("scope_kind") or "").strip()
    scope_id = str(spec.get("scope_id") or "").strip()
    if scope_kind not in ("run", "campaign") or not scope_id:
        return None
    resolved = spec.get("resolved")
    resolved = resolved if isinstance(resolved, dict) else {}
    heal_classes = resolved.get("heal_classes")
    classes = (
        sorted(str(c) for c in heal_classes if isinstance(c, str))
        if isinstance(heal_classes, list)
        else []
    )
    cmd_sha_raw = resolved.get("cmd_sha")
    cmd_sha = cmd_sha_raw if isinstance(cmd_sha_raw, str) and cmd_sha_raw else None
    try:
        from hpc_agent.ops.overnight import compose_consent_defaults

        composed = compose_consent_defaults(dict(resolved))
        expires_at = composed.get("expires_at")
    except Exception:  # noqa: BLE001 — a compose failure must not wedge capture
        expires_at = resolved.get("expires_at")
    return {
        "channel": "elicitation",
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "block": _OVERNIGHT_CONSENT_BLOCK,
        "subject": {
            "heal_classes": classes,
            "expires_at": expires_at if isinstance(expires_at, str) else None,
            "cmd_sha": cmd_sha,
        },
    }


# The digest is a SIGNING surface, not a reading surface (RULING 2): a byte budget
# on the render-derived block keeps the terminal dialog from scrolling. When the
# HONEST digest exceeds it (pathologically many hunks/flags), the composer does NOT
# compress harder — it emits an honest-refusal block (identity + counts + the
# pointer), because a digest that could silently drop a judgment-critical item to
# fit a cap is the misleading-summary class. The budget is generous for the normal
# case (the per-item caps in ``render_store`` bound that); it is the last-ditch
# guard against a render whose sheer item count would blow the popup.
_DIGEST_BLOCK_MAX_BYTES: int = 1400

#: Budget for the popup's embedded DIFF BODY (run-#12 finding 11). RULING 2's
#: "per-hunk one-liners — never the diff body" was REVERSED by live run-#12
#: feedback at the first popup ("there's not enough diff showed for me to
#: properly review"): a signing surface must carry enough of the change to
#: review, not just count it. The body rides in its OWN bounded block so the
#: digest's honesty budget above is untouched; truncation is always DISCLOSED
#: with the on-disk remainder pointed at. Interim until unified-render O3+
#: (chunked popups carrying the full render) supersedes.
_DIFF_EMBED_MAX_BYTES: int = 6000


def _render_diff_body_lines(path: Path) -> list[str]:
    """The render's fenced diff-from-template body, embedded BOUNDED.

    Extracts the FIRST ```diff fence from the code-written render file —
    code-read bytes, nothing recomputed, no model text — capped at
    :data:`_DIFF_EMBED_MAX_BYTES` on a line boundary with the elision count
    disclosed. Empty when the render has no diff fence (an inherited section)
    or cannot be read — the digest already routes to the on-disk render.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    marker = "\n```diff\n"
    start = text.find(marker)
    if start < 0:
        return []
    body_start = start + len(marker)
    end = text.find("\n```", body_start)
    if end < 0:
        return []
    diff_lines = text[body_start:end].splitlines()
    out = ["", "Diff from template (code-read from the render):", "```diff"]
    used = 0
    shown = 0
    for line in diff_lines:
        used += len(line.encode("utf-8")) + 1
        if used > _DIFF_EMBED_MAX_BYTES:
            break
        out.append(line)
        shown += 1
    out.append("```")
    if shown < len(diff_lines):
        out.append(
            f"… (+{len(diff_lines) - shown} more diff lines — the full render on disk carries them)"
        )
    return out


def _render_digest_block(
    experiment_dir: Path | None, *, audit_id: str, section: str, view_sha: str
) -> list[str]:
    """The E-render DIGEST v2 lines for a notebook sign-off, or a disclosed fallback.

    Reads the content-addressed render off disk and returns a BOUNDED, three-JOB
    signing digest (RULING 2, ``docs/design/mcp-elicitation.md``):

    * **BIND** — audit id, section slug, ``view_sha12``, freshness. A STALE render
      (the signed ``view_sha`` no longer addresses the on-disk render) says
      STALE — do NOT sign and shows nothing but the pointer (Job 1).
    * **WHY YOUR JUDGMENT** — the tier-trigger headline (which of diff / lint /
      assertions fired, with counts), the declared-assertion table (marked
      unverified — the trusted render is STATIC, no execution, so there is no
      computed value to show and none is fabricated), the lint-flag NAMES +
      locations, and per-hunk one-liners (line range + first changed line)
      (Job 2). The DIFF BODY additionally rides in its own bounded block
      (:func:`_render_diff_body_lines` — run-#12 finding 11 reversed RULING 2's
      never-the-diff-body clause: a signing surface must carry enough of the
      change to review).
    * **ROUTE** — the on-disk render path, stated plainly (Job 3).

    Every non-embeddable condition — no experiment context, no bound ``view_sha``
    yet, no render on disk, or a header that disagrees with the signed view —
    returns a single reason-disclosing line instead (never a crash, never an
    unmarked silent omission). THE HONESTY RULE: when the honest digest exceeds
    :data:`_DIGEST_BLOCK_MAX_BYTES`, the composer emits an honest-refusal block
    ("too large to digest honestly: N hunks, M flags — read the render") rather than
    compressing until a judgment-critical item silently drops. The digest is
    code-authored throughout: no notebook source, no model text, ever enters it.
    """
    view_sha12 = view_sha[:12]

    def _fallback(reason: str) -> list[str]:
        return [
            "",
            f"(render digest unavailable: {reason} — open the section render in your "
            "Read pane before signing.)",
        ]

    if experiment_dir is None:
        return _fallback("no experiment context on this call")
    if not (audit_id and section and view_sha):
        return _fallback("the sign-off carries no bound view_sha yet")

    from hpc_agent.ops import notebook_view

    path = notebook_view.render_path(
        experiment_dir, audit_id=audit_id, section=section, view_sha=view_sha
    )

    def _do_not_sign() -> list[str]:
        # Job 1 freshness failure: the render is content-addressed by the SIGNED
        # view_sha, so an absent/unreadable render for it means the source drifted
        # (the view_sha moved) or the section was never rendered at this view — the
        # STALE case. Never summarize it as current; say do-not-sign and show only
        # the pointer.
        return [
            "",
            f"STALE or missing render — do NOT sign: no current code-written render "
            f"on disk for the view_sha you are signing ({view_sha12}).",
            f"Re-render and open the section render in your Read pane before signing: {path}",
        ]

    digest = notebook_view.read_render_digest(path)
    if digest is None:
        return _do_not_sign()
    if digest.view_sha != view_sha or digest.section != section:
        return _do_not_sign()

    # Job 1 — BIND.
    lines = [
        "",
        f"Reviewed render digest — view_sha {view_sha12} (fresh):",
        f"- audit {digest.audit_id} / section {digest.section}",
    ]
    # Job 2 — WHY YOUR JUDGMENT.
    lines.append(f"- {_tier_trigger_headline(digest)}")
    if digest.assertion_count:
        lines.append(
            f"- assertions (declared, unverified — static audit, no execution): "
            f"{digest.assertion_count}"
        )
        for entry in digest.assertions:
            lines.append(f"    · {entry}")
        elided = digest.assertion_count - len(digest.assertions)
        if elided > 0:
            lines.append(f"    · … ({elided} more — read the render)")
    if digest.lint_flag_count:
        lines.append(f"- lint flags ({digest.lint_flag_count}):")
        for name in digest.lint_flags:
            lines.append(f"    · {name}")
        elided = digest.lint_flag_count - len(digest.lint_flags)
        if elided > 0:
            lines.append(f"    · … ({elided} more — read the render)")
    if digest.diff_hunk_count:
        lines.append(
            f"- diff from template: +{digest.diff_added} / -{digest.diff_removed} "
            f"lines across {digest.diff_hunk_count} hunk(s):"
        )
        for hunk in digest.diff_hunks:
            lines.append(f"    · {hunk}")
        elided = digest.diff_hunk_count - len(digest.diff_hunks)
        if elided > 0:
            lines.append(f"    · … ({elided} more — read the render)")
    # Job 3 — ROUTE.
    lines.append(f"- full render on disk: {path}")

    # THE HONESTY RULE: if the honest digest overruns the budget, do not compress
    # harder — refuse to digest and point at the render, disclosing the counts.
    if len("\n".join(lines).encode("utf-8")) > _DIGEST_BLOCK_MAX_BYTES:
        return [
            "",
            f"Reviewed render digest — view_sha {view_sha12} (fresh):",
            f"- audit {digest.audit_id} / section {digest.section}",
            f"- too large to digest honestly: {digest.diff_hunk_count} diff hunks, "
            f"{digest.lint_flag_count} lint flags, {digest.assertion_count} assertions "
            "— read the render.",
            f"- full render on disk: {path}",
            # The bounded diff body still rides (finding 11): the digest refused
            # to COMPRESS, but review material with disclosed truncation is
            # additive, not a silent drop.
            *_render_diff_body_lines(path),
        ]
    return lines + _render_diff_body_lines(path)


def _tier_trigger_headline(digest: Any) -> str:
    """The one-line "why your judgment is required" headline for a render digest.

    Names which of the three D-attention tier legs FIRED, with counts — derived from
    the render's own fields (``classification`` for the diff leg, ``lint_flag_count``
    for the flags leg, ``assertion_count`` for the assertions leg; in the static
    audit any declared assertion is unverified and so a judgment trigger). An
    ``auto_cleared`` render (a redundant/voluntary sign-off) has no trigger and says
    so — the human is signing something the tiering deemed already clear.
    """
    triggers: list[str] = []
    if digest.classification and digest.classification != "inherited":
        triggers.append(f"diff: {digest.classification}")
    if digest.lint_flag_count:
        triggers.append(f"lint: {digest.lint_flag_count} flag(s)")
    if digest.assertion_count:
        triggers.append(f"assertions: {digest.assertion_count} unverified")
    if not triggers:
        return "requires your judgment: no tier trigger (auto-cleared — a voluntary review)"
    return "requires your judgment — " + "; ".join(triggers)


def _accepted_utterance(response: dict[str, Any] | None) -> str | None:
    """The filtered, human-typed text from an elicitation response, or ``None``.

    ``None`` is the decline-equivalent for EVERY non-capture outcome (D3): a
    ``None`` response (no transport / timeout / EOF), an error response, a
    decline/cancel action, a malformed shape, or a body whose every string field
    is empty or harness-injected. Each returned string field is filtered through
    the ONE reference provenance filter
    (:func:`hpc_agent.state.utterances.is_harness_injected`) and a non-empty
    check — mirroring ``answer_capture._typed_texts`` — so a nonconforming client
    that returns canned/injected text still degrades to decline.
    """
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None  # an ``error`` response, or a malformed one
    if result.get("action") != "accept":
        return None  # decline / cancel
    content = result.get("content")
    if not isinstance(content, dict):
        return None
    from hpc_agent.state.utterances import is_harness_injected

    texts: list[str] = []
    for value in content.values():
        if not isinstance(value, str):
            continue
        if not value.strip():
            continue
        if is_harness_injected(value):
            continue
        texts.append(value.strip())
    if not texts:
        return None
    return "\n".join(texts)


def _with_capture_markers(result: dict[str, Any], sha256: str) -> dict[str, Any]:
    """Return *result* with the elicitation capture markers merged in (D5).

    The retried tool result — whatever verdict the gate returned — gains
    ``{elicitation: "captured", sha256: <digest>}``: the FINGERPRINT of the
    recorded utterance, never the human's words. The model learns the gate's
    verdict from the retried envelope and the sha, so the response text never
    passes through the model. A result with no ``structuredContent`` (a shape
    the runner never produces here) is returned untouched.
    """
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        return result
    merged = dict(structured)
    merged["elicitation"] = "captured"
    merged["sha256"] = sha256
    return {
        "content": [{"type": "text", "text": json.dumps(merged, sort_keys=True)}],
        "structuredContent": merged,
        "isError": result.get("isError", merged.get("ok") is not True),
    }
