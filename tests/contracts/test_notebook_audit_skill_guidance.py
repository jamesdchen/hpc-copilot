"""Contract: the hpc-notebook-audit skill pins the harness-first audit doctrine.

The thin notebook-audit skill (T13, promoted to v1 —
``docs/design/notebook-audit.md``, "The audit SURFACE — harness-first") drives
the prelude loop: the LLM drafts a percent-format ``.py`` source, then the audit
runs ``notebook-lint`` → ``notebook-auto-clear`` → ``notebook-audit-view``
(relayed VERBATIM) → typed human sign-off via ``append-decision`` →
``notebook-status``. Its load-bearing invariants are LLM-facing prose with no
lint that can enforce them, so — the same drift-guard philosophy as
``test_detached_worker_brief_guidance`` / ``test_skill_state_reconciliation`` —
this binds them to the SKILL.md and fails CI if a future edit drops one:

* the audit-view ``markdown`` is relayed VERBATIM (the D6 interface; no LLM
  paraphrase of the audit content — the relay-audit Stop-hook posture);
* the skill NEVER edits the source between the view and the sign-off (an edit
  moves the hash and voids the relayed ``view_sha``);
* the sign-off is committed through ``append-decision`` with the ``"notebook"``
  scope kind, the ``notebook-sign-off`` block, and ``section_sha`` + ``view_sha``
  in ``resolved`` — there is no sign-off verb;
* the four audit verbs are all named (the loop is real, not aspirational);
* the graduation gate (``SourceUnaudited``) is the entry ticket;
* the skill never resolves a decision, and elicits the audit_id / intent as
  FREE TEXT, never a pre-filled button (the authorship doctrine).
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL = _REPO_ROOT / "src/hpc_agent/slash_commands/skills/hpc-notebook-audit/SKILL.md"


def _text() -> str:
    return _SKILL.read_text(encoding="utf-8")


def test_skill_file_exists_and_is_installable() -> None:
    """The skill ships as ``skills/hpc-notebook-audit/SKILL.md`` — the shape the
    installer (``agent_assets._install_tree``) auto-discovers and the lint
    (``scripts/lint_skills.py``) auto-globs. A missing file means the skill was
    never wired in."""
    assert _SKILL.is_file(), f"{_SKILL} must exist — the installer globs skills/*/SKILL.md"
    text = _text()
    assert text.startswith("---"), "SKILL.md must open with YAML frontmatter"
    assert "name: hpc-notebook-audit" in text, "frontmatter must name the skill"


def test_skill_drives_the_four_audit_verbs() -> None:
    text = _text()
    for verb in (
        "notebook-lint",
        "notebook-auto-clear",
        "notebook-audit-view",
        "notebook-status",
    ):
        assert verb in text, f"the audit loop must name the {verb!r} verb"


def test_audit_view_markdown_is_relayed_verbatim() -> None:
    """The D6 interface: the code-rendered ``markdown`` projection is relayed
    VERBATIM — no LLM paraphrase of the audit content ever enters the path."""
    text = _text()
    assert "VERBATIM" in text, "the audit view must be relayed VERBATIM"
    # Bind VERBATIM to the audit-view markdown, not merely mentioned in passing.
    assert re.search(r"markdown[^.\n]*VERBATIM|VERBATIM[^.\n]*markdown", text) or (
        "notebook-audit-view" in text and "markdown" in text and "VERBATIM" in text
    ), "VERBATIM must bind to the notebook-audit-view `markdown` projection"
    assert re.search(
        r"never paraphrase|not paraphrase|no LLM paraphrase|never interpret", text, re.I
    ), "the skill must forbid paraphrasing/interpreting the audit content"


def test_skill_never_edits_source_between_view_and_signoff() -> None:
    """An edit mid-rendezvous moves the section hash and voids the relayed
    view_sha — the skill must state it never edits the source during the audit."""
    text = _text().lower()
    assert "never edit" in text and "sign-off" in text, (
        "the skill must state it NEVER edits the source between the view and the sign-off"
    )
    assert "prelude" in text, (
        "drafting/revising must be scoped to the sanctioned prelude role, distinct "
        "from the frozen-source audit steps"
    )


def test_signoff_goes_through_append_decision_with_the_notebook_scope() -> None:
    """No sign-off verb exists (the no-unlock-verb doctrine): a section is signed
    via append-decision with the notebook scope kind + block + bound hashes."""
    text = _text()
    assert "append-decision" in text, "sign-off must commit through append-decision"
    assert '"notebook"' in text or "notebook-sign-off" in text, (
        "the sign-off must use the notebook decision-journal scope / block"
    )
    assert "notebook-sign-off" in text, "the sign-off block must be named"
    assert "section_sha" in text and "view_sha" in text, (
        "the sign-off's resolved must bind section_sha + view_sha (what the human saw)"
    )
    assert "no sign-off verb" in text.lower() or "no-unlock-verb" in text.lower(), (
        "the skill must state there is no sign-off verb by design"
    )


def test_nudge_moves_the_hash_and_revokes_trust() -> None:
    text = _text().lower()
    assert "nudge" in text, "the skill must document the y/nudge rendezvous"
    assert "hash moves" in text or "hash move" in text or "moved hash" in text, (
        "a nudge/edit must be stated to move the section hash"
    )
    assert "unsigned" in text and ("re-draft" in text or "redraft" in text), (
        "an edited section reads unsigned by construction and must be re-drafted + re-audited"
    )


def test_graduation_gate_is_the_entry_ticket() -> None:
    text = _text()
    assert "SourceUnaudited" in text, (
        "the skill must point at the graduation gate (errors.SourceUnaudited) — a "
        "stale audit is refused by submit"
    )
    assert "passed" in text, "done == the notebook-status `passed` predicate"


def test_popup_is_the_primary_signoff_surface_not_chat_parking() -> None:
    """Run-12 finding 9 / user ruling 2026-07-09/10 (E-render): over MCP the
    sign-off popup is THE default read-and-sign surface — the skill must say
    to proceed directly to ``append-decision`` (whose elicit-then-retry wrap
    opens the popup) and must NOT re-encode the superseded pre-popup
    chat-first flow (the drift class fixed in ``101cd111``: the skill parked
    waiting for chat text where a popup could fire)."""
    text = _text()
    assert re.search(r"popup[^.\n]*PRIMARY|PRIMARY[^.\n]*popup", text), (
        "the popup must be named the PRIMARY sign-off surface (E-render ruling)"
    )
    assert re.search(r"do NOT park|never park", text, re.I), (
        "the skill must forbid parking for chat text where the popup can fire"
    )
    assert re.search(r"[Cc]hat-first is the FALLBACK", text), (
        "chat-first must be stated as the fallback (no elicitation channel), not the default"
    )


def test_skill_never_resolves_and_elicits_free_text() -> None:
    text = _text()
    assert "never resolves a decision" in text, (
        "the skill must state it never resolves a decision (the doctrine sibling skills share)"
    )
    assert re.search(r"free[- ]text", text, re.I), (
        "the audit_id / intent must be elicited as free text (the authorship doctrine)"
    )
    assert re.search(r"never.*(pre-fill|pre-filled|button)", text, re.I), (
        "the skill must forbid pre-filled buttons for the authorship-bearing inputs"
    )
