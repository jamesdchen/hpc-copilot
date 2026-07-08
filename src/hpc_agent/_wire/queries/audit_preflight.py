"""Pydantic models for the ``audit-preflight`` GO/NO-GO query (Phase 1b).

``audit-preflight`` composes EXISTING notebook-audit substrate checks — template
present + parses + git-committed-clean, version skew, declared-roots validity,
and prior audit state (resuming vs fresh) — into one decision-ready brief. It is
a read-only query: it detects nothing new and blocks nothing itself (the gates it
predicts remain the enforcement). See ``docs/design/audit-preflight.md``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AuditPreflightSpec(BaseModel):
    """Input spec for the ``audit-preflight`` verb."""

    model_config = ConfigDict(extra="forbid", title="audit-preflight input spec")

    template: str = Field(
        description=(
            "Path (experiment-relative, or absolute) to the audit TEMPLATE .py — "
            "the percent-format module whose section slugs are the required "
            "inventory. Checked for: present, parses via parse_percent_source, and "
            "git-committed-clean at that path (an uncommitted/dirty template is an "
            "'unsigned template' NO-GO — the commit IS the signature)."
        )
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
            "resuming-vs-fresh. Omit for a fresh standalone preflight."
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
