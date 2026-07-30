"""Pydantic models for the ``audit-preflight`` GO/NO-GO query (Phase 1b).

``audit-preflight`` composes EXISTING notebook-audit substrate checks — template
present + parses + git-committed-clean, version skew, declared-roots validity,
and prior audit state (resuming vs fresh) — into one decision-ready brief. It is
a read-only query: it detects nothing new and blocks nothing itself (the gates it
predicts remain the enforcement). See ``docs/design/audit-preflight.md``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AuditPreflightSpec(BaseModel):
    """Input spec for the ``audit-preflight`` verb."""

    model_config = ConfigDict(extra="forbid", title="audit-preflight input spec")

    template: str | None = Field(
        default=None,
        description=(
            "Path (experiment-relative, or absolute) to the audit TEMPLATE .py — "
            "the percent-format module whose section slugs are the required "
            "inventory. Checked for: present, parses via parse_percent_source, and "
            "git-committed-clean at that path (an uncommitted/dirty template is an "
            "'unsigned template' NO-GO — the commit IS the signature). OMITTED => "
            "composed from the bound pack's audit_template seam (the interview's "
            "own selection, one definition — run-#12 finding 5); the composed path "
            "is echoed on the result, and nothing composable is a loud refusal, "
            "never a guess."
        ),
    )
    source_roots: list[str] | None = Field(
        default=None,
        description=(
            "Opaque import roots (the linked-sources lint's roots). When omitted "
            "AND audit_id names an existing audit, defaults from that audit's "
            "recorded configuration (the one-declaration rule); otherwise []."
        ),
    )
    input_roots: list[str] | None = Field(
        default=None,
        description=(
            "Opaque data-path roots (the executes-live lint's roots). When omitted "
            "AND audit_id names an existing audit, defaults from that audit's "
            "recorded configuration; otherwise []."
        ),
    )
    audit_id: str | None = Field(
        default=None,
        description=(
            "The caller-authored audit slug. When it names an existing audit its "
            "recorded roots default the roots above, and its journal decides "
            "resuming-vs-fresh. Omit for a fresh standalone preflight. REQUIRED "
            "for the block-drive `audit` chain (P2.b): it is the seat every "
            "downstream block is keyed by, so a preflight without one emits no "
            "`next_block` and the chain does not start."
        ),
    )
    source: str | None = Field(
        default=None,
        description=(
            "Experiment-relative path to the audited source .py the loop will "
            "lint. Present-and-readable is the DRAFT-READINESS check (P2.b): a GO "
            "preflight whose source is absent has nothing to lint yet, so the "
            "chain parks for the LLM to draft it (`awaiting_draft`) instead of "
            "chaining into a lint that would refuse. Omitted (the default) reads "
            "as 'no source yet' — byte-identical to a pre-P2.b preflight in every "
            "check, verdict, blocker and brief line."
        ),
    )


class PreflightBlocker(BaseModel):
    """One NO-GO blocker: the failing check, what is wrong, and its remedy.

    Every blocker carries a PRE-DRAFTED remedy so the brief is decision-ready —
    the human reads the fix, never has to derive it.
    """

    model_config = ConfigDict(extra="forbid", title="audit-preflight blocker")

    check: Literal["template", "version_skew", "roots"] = Field(
        description="Which substrate check produced this blocker."
    )
    blocker: str = Field(description="What is wrong (the NO-GO reason), stated plainly.")
    remedy: str = Field(description="The pre-drafted fix that clears this blocker.")


class AuditPreflightResult(BaseModel):
    """Shape of the ``data`` field on an ``audit-preflight`` envelope."""

    model_config = ConfigDict(extra="forbid", title="audit-preflight output data")

    verdict: Literal["GO", "NO-GO"] = Field(
        description="GO iff there are zero blockers; NO-GO otherwise."
    )
    audit_id: str | None = Field(
        default=None, description="The audit slug the preflight ran against, or null (standalone)."
    )
    template: str = Field(description="The template path the preflight checked.")
    template_state: str = Field(
        description=(
            "The template's resolved state: 'clean' (committed, no changes), "
            "'dirty' (tracked with uncommitted changes), 'untracked' (present but "
            "not committed), 'missing', 'unparseable', 'unreadable', or 'no_git' "
            "(no git repo to verify the commit-signature)."
        )
    )
    resuming: bool = Field(
        description="True when audit_id already has a journal (resuming); False = fresh."
    )
    journal_records: int = Field(
        default=0, description="Number of prior journal records for audit_id (0 when fresh)."
    )
    source_roots: list[str] = Field(
        default_factory=list, description="The resolved source_roots (spec, else recorded config)."
    )
    input_roots: list[str] = Field(
        default_factory=list, description="The resolved input_roots (spec, else recorded config)."
    )
    blockers: list[PreflightBlocker] = Field(
        default_factory=list,
        description="One entry per NO-GO blocker, each with its pre-drafted remedy. Empty on GO.",
    )
    disclosures: list[str] = Field(
        default_factory=list,
        description=(
            "Non-blocking disclosures rendered alongside the verdict — the "
            "data-manifest drift line (Phase-1a seam) and the resuming note. "
            "NEVER flip the verdict (the attention contract)."
        ),
    )
    brief: str = Field(
        description=(
            "The D8 decision-ready brief, code-rendered — GO, or NO-GO with each "
            "blocker named and its remedy pre-drafted. Relayed to the human "
            "VERBATIM; the verb never blocks anything itself."
        )
    )
    source_present: bool = Field(
        default=False,
        description=(
            "True when the spec named a `source` and that file exists and reads. "
            "Draft-readiness only — NEVER a blocker and never a verdict input "
            "(the verdict is the substrate checks alone); it selects the chain's "
            "next edge (lint vs the agent draft park)."
        ),
    )
    stage_reached: Literal["preflight_go", "awaiting_draft", "preflight_blocked"] = Field(
        default="preflight_go",
        description=(
            "The terminator this preflight stopped at (decision-as-data, #231) — "
            "the key `block_chain.SUCCESSORS` routes the `audit` chain on. "
            "`preflight_go` = GO with a source to lint; `awaiting_draft` = GO with "
            "nothing to lint yet (the AGENT park — the LLM drafts; no consent is "
            "sought or consumed); `preflight_blocked` = NO-GO."
        ),
    )
    needs_decision: bool = Field(
        default=False,
        description=(
            "True at a boundary the driver must PARK at. `awaiting_draft` parks "
            "for the AGENT (a draft is authorship, not authorization — no "
            "greenlight, no consent, no approve_hint is composed there); "
            "`preflight_blocked` parks for the HUMAN, who clears the blockers."
        ),
    )
    next_block: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The DETERMINISTICALLY-computed next block — `{verb, why, spec_hint}` "
            "— or null at a park / when no `audit_id` seat exists to key the chain "
            "on. Computed by `block_chain.next_block_hint`; the `spec_hint` is the "
            "successor's COMPLETE code-composed input spec, never a skeleton the "
            "agent finishes."
        ),
    )
