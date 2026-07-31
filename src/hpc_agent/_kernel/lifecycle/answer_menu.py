"""Pure composer for a parked boundary's PASTE-READY ANSWER MENU.

Lives in ``_kernel.lifecycle`` — NOT in ``ops`` — for exactly the reason
:mod:`hpc_agent._kernel.lifecycle.consent_hint` does: its sole writer is the
driver park (:func:`hpc_agent._kernel.lifecycle.block_drive.park`), and
``_kernel`` is a substrate role that must not import UP into ``ops`` (the
layering + private-cross-package import lints). ``ops.relay_render`` re-exports
:func:`compose_answer_menu` / :func:`answer_menu_of` (a legal DOWNWARD reach) so
the relay-verbatim home and its ``ops``-side consumers (the notification payload
composer, :mod:`hpc_agent.ops.recover.notify`) keep one import path.

WHY (run-queue-placement-2026-07-28.md §8 S13, "merged-park UX at depth"): at
fleet scale a return-when-nothing-drivable sitting hands the human 15+ verbatim
briefs at once. Rule 2 forbids SUMMARIZING a brief and the agent may never
auto-answer one, so the only lever left is the human's TYPING cost at the
boundary — and today that cost is "read the brief, then reconstruct the answer
grammar from memory". This composer removes the reconstruction the same way
``overnight_consent``'s paste-ready grant line did (2026-07-29, THE precedent
this extends): the gate reads TOKENS, not phrasing, so rendering the exact line
to paste changes the typing burden and nothing about the trust story.

Two rules bound what may appear in a menu — both load-bearing:

* **CODE-CARRIED DATA ONLY (§2).** Every option's ``paste`` string is built from
  tokens the brief or the driver ALREADY carries — the greenlight target verb,
  the run id, the ``@<sha8>`` spec pin, and the ``action`` / ``then`` fields of a
  STRUCTURED recommendation (``ops/status_blocks.recommendation_for``'s
  ``classify-failed-tasks``/``resubmit-failed``, ``reconcile-journal``/
  ``confirm-abandoned-then-resubmit``). A recommendation carried as PROSE (the
  aggregate integrity issues, S1's ``safe_default`` mirror) is NEVER rendered as
  a paste-line: :func:`recommendation_options` accepts only a mapping with a
  non-empty string ``action``. Inventing the alternative text would make the menu
  an agent-authored proposal — the exact thing the fork forbids.
* **NOTHING IS AUTO-ANSWERED.** The menu is DISCLOSURE, like ``approve_hint`` and
  ``materialized_successor_spec`` beside it: the human pastes a line, nothing
  fills it, and a bare ``y`` keeps working wherever it works today (the
  block-drive R3 consumption leg — a plain ``y`` advances under the materialized
  successor spec). The menu's ``bare_y_ok`` REPORTS that fact per boundary; it
  never grants it.

The OPTIONAL STANDING OFFER (``standing_offer`` — the 'speculative y done
right', 2026-07-29) obeys both rules: its ``paste`` string is the
overnight-consent grant line rendered by the ONE grant-vocabulary home
(:func:`hpc_agent.ops.overnight.render_grant_line`, the same renderer the
authorship gate's refusal uses) from tokens the driver already carries (the run
id, the SIDECAR ``cmd_sha``, the cluster stamp) — this composer receives it
PRE-RENDERED so it stays pure and ``_kernel`` never imports up into ``ops``.
Pasting it is the human's own typed grant of the AUTHORSHIP leg only (recording
the consent still composes caps + wake and runs every gate unchanged); it is
labelled OPTIONAL, is never ``answer_line`` and never flips ``bare_y_ok``, is
dropped at an anomaly terminator (offering "let it run all night" beside an
OVERRIDE is wrong), and cannot mint a menu on its own — it answers nothing at
THIS boundary.

PURE + DETERMINISTIC: no I/O, no clock, no registry read — the caller supplies
every token, and the same inputs always render the same menu (the property
``tests/contracts/test_bare_y_coverage.py`` enumerates the boundary census
against).
"""

from __future__ import annotations

from typing import Any

from hpc_agent._kernel.lifecycle.consent_hint import _sha8

__all__ = [
    "answer_menu_of",
    "compose_answer_menu",
    "compose_draft_ask",
    "draft_ask_of",
    "recommendation_options",
]

#: Widest ``paste`` column the rendered lines pad to before falling back to a
#: single space. Keeps a menu readable in a terminal without ever TRUNCATING an
#: option (a truncated paste-line would be un-pasteable — worse than unaligned).
_PASTE_COLUMN_MAX = 44

#: The fixed literal every structured-recommendation option carries as its
#: ``means``. Deliberately ONE constant rather than a per-case sentence: a
#: per-case sentence would be code-authored prose ABOUT the recommendation, and
#: the recommendation's own ``action``/``then`` tokens already say what it is.
_RECOMMENDATION_MEANS = "structured recommendation carried by this brief"

#: The fixed ``means`` label of the standing-consent OFFER (same one-constant
#: discipline as :data:`_RECOMMENDATION_MEANS`). It must say HONESTLY what the
#: pasted line does and does not do: OPTIONAL and STANDING (not an answer to
#: this park, never the default), and it pre-authorizes only the grant's
#: AUTHORSHIP leg — recording the consent still runs the poka-yoke caps + wake
#: composition and every gate unchanged.
_STANDING_OFFER_MEANS = (
    "OPTIONAL standing grant — overnight consent so later clean boundaries "
    "advance unattended; pre-authorizes the grant only (caps + wake still "
    "compose at record time), not an answer to this park, never the default"
)


def _option(action: str, then: str | None) -> dict[str, Any]:
    """One structured-recommendation option from its OWN ``action``/``then`` tokens.

    The connective ``", then "`` is the only string this function contributes;
    both operands come from the brief. A recommendation with no ``then`` renders
    the bare action, so the paste-line is never padded with invented words.
    """
    paste = f"{action}, then {then}" if then else action
    option: dict[str, Any] = {
        "paste": paste,
        "means": _RECOMMENDATION_MEANS,
        "kind": "recommendation",
        "action": action,
    }
    if then:
        option["then"] = then
    return option


def _as_structured(value: Any) -> tuple[str, str | None] | None:
    """``(action, then)`` when *value* is STRUCTURED recommendation data, else ``None``.

    The §2 admission test, in one place: a recommendation qualifies only when it
    is a mapping carrying a non-empty string ``action``. A prose recommendation
    (``str``), a bare ``safe_default`` mirror, or a mapping with no action is
    rejected — it has no code-carried token to paste, and the menu must not
    author one.
    """
    if not isinstance(value, dict):
        return None
    action = value.get("action")
    if not (isinstance(action, str) and action):
        return None
    then = value.get("then")
    return action, then if isinstance(then, str) and then else None


def recommendation_options(brief: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every STRUCTURED recommendation *brief* carries, as de-duplicated options.

    Scans three shapes, in a fixed order so the menu is byte-stable:

    1. the brief's OWN top-level ``recommendation``;
    2. the ``recommendation`` of each mapping-valued brief key (the watch/S3
       anomaly brief nests its ``ops/status_blocks.recommendation_for`` data under
       ``brief["anomaly"]``);
    3. the ``recommendation`` of each mapping inside any list-valued brief key
       (the snapshot brief's ``anomalies`` rows).

    Generic over the KEY (never a hardcoded list of brief shapes) but strict
    about the VALUE (:func:`_as_structured`), so a new brief type gets its menu
    for free while a prose recommendation still renders nothing. Bounded to one
    level of nesting on purpose: an unbounded walk would eventually reach a
    ``recommendation`` buried in some sub-record the boundary does not actually
    offer, and a menu line the boundary cannot honour is worse than none.
    Duplicates collapse on the rendered paste-line — one anomaly class repeated
    across ten rows is ONE option, not ten.
    """
    if not isinstance(brief, dict):
        return []
    found: list[tuple[str, str | None]] = []
    top = _as_structured(brief.get("recommendation"))
    if top is not None:
        found.append(top)
    for key, value in brief.items():
        if key == "recommendation":
            continue  # already taken above; re-taking would duplicate the scan
        rows = [value] if isinstance(value, dict) else value if isinstance(value, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            nested = _as_structured(row.get("recommendation"))
            if nested is not None:
                found.append(nested)
    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for action, then in found:
        option = _option(action, then)
        if option["paste"] in seen:
            continue
        seen.add(str(option["paste"]))
        options.append(option)
    return options


def _standing_offer_option(
    standing_offer: dict[str, Any] | None, *, is_anomaly_terminator: bool
) -> dict[str, Any] | None:
    """The OPTIONAL standing-consent grant option from a caller's offer, or ``None``.

    The admission mirror of :func:`_as_structured`'s posture: only a mapping
    carrying a non-empty string ``grant_line`` (the pre-rendered one-home
    sentence — see the module docstring) renders; a torn offer renders nothing
    rather than an un-pasteable line. Dropped unconditionally at an anomaly
    terminator: offering "let it run all night" beside a failed-canary OVERRIDE
    invites exactly the unattended advance the anomaly exists to interrupt.
    ``block_drive`` never composes the offer there either, but the rule must
    hold for EVERY caller of this public composer, so this guard is the one the
    battery fires directly.
    """
    if is_anomaly_terminator or not isinstance(standing_offer, dict):
        return None
    grant_line = standing_offer.get("grant_line")
    if not (isinstance(grant_line, str) and grant_line):
        return None
    option: dict[str, Any] = {
        "paste": grant_line,
        "means": _STANDING_OFFER_MEANS,
        "kind": "standing-offer",
        "optional": True,
    }
    # The scope tokens the line names, carried structured beside the sentence
    # (the ``scope_tokens`` discipline of ``consent_hint.compose_approve_hint``)
    # so a downstream surface never has to re-parse the rendered prose.
    # ``regrant_reason``/``previous_cmd_sha`` ride only on the RE-GRANT form (a
    # prior consent died on spec change and this line re-grants for the current
    # identity — block_drive stamps them from ``ops.overnight``'s re-grant leg).
    for key in ("scope_kind", "run_id", "cmd_sha", "cluster", "regrant_reason", "previous_cmd_sha"):
        value = standing_offer.get(key)
        if isinstance(value, str) and value:
            option[key] = value
    return option


def _render_lines(options: list[dict[str, Any]], *, bare_y_ok: bool) -> list[str]:
    """The human-facing menu block: a header, one line per option, a footer.

    Every line is ``<paste>  → <means>``; the paste column is padded to the widest
    option (capped at :data:`_PASTE_COLUMN_MAX`) so the arrow column lines up
    without any option ever being shortened.
    """
    width = min(max((len(str(o["paste"])) for o in options), default=0), _PASTE_COLUMN_MAX)
    lines = ["Answer menu — paste ONE line into chat (nothing here is auto-filled):"]
    lines += [f"  {str(o['paste']):<{width}}  → {o['means']}" for o in options]
    lines.append(
        "A bare 'y' is accepted at this boundary; anything else is a nudge."
        if bare_y_ok
        else "This boundary has no materialized default — a bare 'y' has nothing to "
        "advance to; answer with one of the lines above or your own nudge."
    )
    return lines


def compose_answer_menu(
    *,
    brief: dict[str, Any] | None,
    block: str | None,
    run_id: str | None,
    target: str | None,
    next_spec_sha: str | None = None,
    approve_hint: dict[str, Any] | None = None,
    is_anomaly_terminator: bool = False,
    standing_offer: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Compose the paste-ready answer menu for one parked boundary.

    * ``target`` is the boundary's greenlight target — the parked ``next_verb``
      else the block's chain-forward successor, resolved by the caller through
      :func:`hpc_agent._kernel.lifecycle.block_drive.greenlight_target`, the SAME
      rule ``committed_greenlight_for_boundary`` applies at consumption, so "what
      a bare ``y`` does" here cannot disagree with what consuming one actually
      does. A ``None`` target means no materialized default exists
      (``bare_y_ok`` False).
    * ``approve_hint`` is the boundary's already-composed OFFERED-CONSENT
      utterance (:func:`hpc_agent._kernel.lifecycle.consent_hint.compose_approve_hint`).
      When present its ``utterance`` becomes a second, scope-naming form of the
      SAME advance — and the menu's ``answer_line``, because a one-line answer
      read on a phone must name the run it advances (see
      :func:`hpc_agent.ops.recover.notify.compose_park_notice`).
    * every other option comes from :func:`recommendation_options`.
    * ``is_anomaly_terminator`` is the caller's lookup of
      :data:`hpc_agent.infra.block_chain.ANOMALY_TERMINATORS`. That registry
      already DECLARES that "a bare ``y`` at an anomaly has no successor — the
      human's nudge names the recovery" (:func:`block_chain.recovery_arm_verb`),
      yet the shared boundary rule still resolves a chain-forward OVERRIDE
      target there, so a bare ``y`` is in fact consumed. Both facts are true and
      the menu must not hide either: the advance option is still listed (it is
      what consumption does) and is labelled an OVERRIDE, so "one keystroke
      submits the main array after a failed canary" can never be a surprise.
    * ``standing_offer`` is the caller's OPTIONAL overnight-consent grant offer
      (``block_drive._compose_standing_offer_for_park`` — composed only at a
      run-scope park with no live standing consent), appended LAST via
      :func:`_standing_offer_option`: it never becomes ``answer_line``, never
      flips ``bare_y_ok``, is dropped at an anomaly terminator, and an offer
      alone never mints a menu (it answers nothing at THIS boundary).

    Returns ``{options, lines, text, answer_line, summary, bare_y_ok, note}`` or
    ``None`` when there is nothing to offer at all (no default advance AND no
    structured recommendation) — the caller then simply omits the menu rather
    than rendering an empty one.
    """
    sha8 = _sha8(next_spec_sha)
    options: list[dict[str, Any]] = []
    answer_line: str | None = None
    if isinstance(target, str) and target:
        pin = f" under the code-composed spec (sha {sha8})" if sha8 else ""
        override = " — an OVERRIDE: this is an anomaly terminator" if is_anomaly_terminator else ""
        options.append(
            {
                "paste": "y",
                "means": f"advance to {target}{pin}{override}",
                "kind": "advance",
                "target": target,
                "override": bool(is_anomaly_terminator),
            }
        )
        answer_line = "y"
        utterance = (approve_hint or {}).get("utterance")
        if isinstance(utterance, str) and utterance:
            options.append(
                {
                    "paste": utterance,
                    "means": "the same advance, with its scope named",
                    "kind": "advance-scoped",
                    "target": target,
                    "override": bool(is_anomaly_terminator),
                }
            )
            answer_line = utterance
    options += recommendation_options(brief)
    if not options:
        # The standing OFFER alone must not mint a menu: it answers nothing at
        # THIS boundary, so a menu holding only the offer would violate its own
        # header ("paste ONE line" — into a park none of the lines resolve).
        return None
    offer_option = _standing_offer_option(
        standing_offer, is_anomaly_terminator=is_anomaly_terminator
    )
    if offer_option is not None:
        options.append(offer_option)

    bare_y_ok = answer_line is not None
    where = f"parked at {block}" if block else "parked"
    who = f" for {run_id}" if run_id else ""
    if bare_y_ok:
        pin = f" (spec sha {sha8})" if sha8 else ""
        summary = f"{where}{who} — a bare 'y' advances to {target}{pin}"
    else:
        summary = f"{where}{who} — no materialized default; this answer names a value"
    lines = _render_lines(options, bare_y_ok=bare_y_ok)
    return {
        "options": options,
        "lines": lines,
        "text": "\n".join(lines),
        "answer_line": answer_line,
        "summary": summary,
        "bare_y_ok": bare_y_ok,
        "note": (
            "code-composed answer menu (S13 merged-park UX): DISPLAY only. Every "
            "paste-line is built from tokens this brief or the driver already "
            "carries — nothing is auto-filled, nothing is summarized, and no "
            "alternative is invented."
        ),
    }


#: What an AGENT park asks for, per ``(block, stage)``. One sentence each, kept
#: as DATA beside the menu composer for the same reason
#: :data:`_RECOMMENDATION_MEANS` is one constant: the ask must be code-authored
#: and fixed, never prose an LLM composes about its own next act.
_DRAFT_ASKS: dict[tuple[str, str], str] = {
    ("audit-preflight", "awaiting_draft"): (
        "Write or revise the audited source .py (jupytext percent format) so it "
        "carries every template section slug, in template order. Then re-tick "
        "`block-drive` — the chain re-enters at `audit-preflight`, which reads the "
        "source off disk and advances to `notebook-lint`."
    ),
    ("wrap-entry-point-auto", "needs_wrapper_argv"): (
        "Compose the wrapper's `argv` template and its typed `signature` from the "
        "CLI parameters ALREADY extracted for you — the brief's `argv_params` "
        "(each with its `dest` and type) and `argv_head` (the leading argv code "
        "composed from the entry-point shape). Append one `{placeholder}` per "
        "swept kwarg to that head and give each placeholder a str/int/float/bool "
        "type in `signature`. Read nothing you were not handed: a param carrying "
        "an `unextracted` marker is the one case that still needs a source read. "
        "Then re-invoke `wrap-entry-point-auto` with `argv` + `signature` "
        "supplied — the chain advances to `interview`."
    ),
}


#: Every ``(block, stage)`` in :data:`hpc_agent.infra.block_chain.AGENT_PARKS`
#: MUST have a :data:`_DRAFT_ASKS` entry. An agent park suppresses every consent
#: affordance the human park machinery composes — greenlight target, approve
#: hint, standing-consent offer, the bare-``y`` menu line — so a member with no
#: ask hands the LLM a stop with NOTHING stated, which is strictly worse than any
#: wrong affordance and is exactly what ``block_drive.park``'s own docstring
#: forbids. Pinned by ``tests/_kernel/lifecycle/test_agent_park.py::
#: test_every_agent_park_emits_a_draft_ask_that_states_what_is_owed``, which
#: LOOPS over the registry rather than naming a member, so a future park cannot
#: join ``AGENT_PARKS`` silently without one.


def compose_draft_ask(
    *,
    block: str | None,
    stage: str | None,
    run_id: str | None,
) -> dict[str, Any] | None:
    """The AGENT park's brief block: what the LLM must PRODUCE (P2.a).

    The agent-actor mirror of :func:`compose_answer_menu`, and deliberately NOT a
    variant of it: an answer menu is a CONSENT surface (its first line is the
    bare ``y`` that advances a boundary), and an agent park has no consent to
    give or take. So this composer renders no ``y``, no scoped-consent utterance,
    no standing-consent offer and no ``bare_y_ok`` — only the DRAFT ASK: the one
    code-authored sentence naming what the LLM produces, and the fact that the
    evidence for it is the file on disk, not a journaled approval.

    Returns ``None`` for a boundary with no registered ask — a park that cannot
    say what it wants renders nothing rather than inventing an instruction.
    """
    ask = _DRAFT_ASKS.get((str(block), str(stage)))
    if ask is None:
        return None
    where = f"parked at {block}" if block else "parked"
    who = f" for {run_id}" if run_id else ""
    lines = [
        "Draft ask — this park is the AGENT's, not the human's:",
        f"  {ask}",
        (
            "Nothing is authorized here: a draft is AUTHORSHIP, not "
            "authorization. No greenlight is sought, none is consumed, and no "
            "`y` applies at this boundary."
        ),
    ]
    return {
        "actor": "agent",
        "kind": "awaiting_draft",
        "block": block,
        "stage": stage,
        "ask": ask,
        "lines": lines,
        "text": "\n".join(lines),
        "summary": f"{where}{who} — awaiting a DRAFT from the agent (no consent semantics)",
        "note": (
            "code-composed draft ask (P2.a agent-actor park): DISPLAY only. It "
            "states what the agent must produce; it grants nothing, consumes no "
            "consent, and offers no answer to type."
        ),
    }


def draft_ask_of(brief: Any) -> dict[str, Any] | None:
    """The ``draft_ask`` a parked brief carries, or ``None``.

    The reader sibling of :func:`answer_menu_of`, with the same fail-open posture:
    a brief from a driver predating agent parks — or a torn one — yields ``None``.
    """
    if not isinstance(brief, dict):
        return None
    ask = brief.get("draft_ask")
    return ask if isinstance(ask, dict) else None


def answer_menu_of(brief: Any) -> dict[str, Any] | None:
    """The ``answer_menu`` a parked brief carries, or ``None``.

    The ONE reader every consumer of a persisted park brief goes through (the
    pending-decision marker's brief round-trips through JSON, so a consumer must
    tolerate any shape). Fail-open by construction: a brief from a driver that
    predates the menu, or a torn one, simply yields ``None`` — a missing menu
    degrades a surface's richness, never its correctness.
    """
    if not isinstance(brief, dict):
        return None
    menu = brief.get("answer_menu")
    return menu if isinstance(menu, dict) else None
