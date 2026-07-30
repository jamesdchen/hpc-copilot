"""Pydantic models for the ``suggest-prelude-action`` query verb's output.

``suggest-prelude-action`` is the deterministic "what is the next prelude step"
ladder (P1.b of ``docs/plans/prelude-chain-2026-07-30.md``): it reads the five
prelude substrates — the notebook decision journal, the notebook-audit-config
seat, the pack journal + ``interview.json``'s ``packs`` opt-in, ``.hpc/axes.yaml``,
and ``interview.json`` itself — and returns exactly ONE next action, the ``why``,
and a scaffold of the exact call to make.

Boundary posture: every field is a mechanical reduction over presence, record
order, and hash comparison of OPAQUE caller content. The verb never names what a
section, pack, or axis MEANS; ``action`` is a closed vocabulary of the repo's own
verb names, and the evidence vector (:class:`PreludeSubstrates`) is disclosed
verbatim so the ladder's choice is auditable rather than trusted. An
unknown/corrupt substrate is a DISCLOSED rung (``doctor``), never a crash.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: The closed action vocabulary — each member names the verb (or the file edit)
#: the ladder's rung recommends. Ordered by rung, lowest first.
PreludeAction = Literal[
    "doctor",
    "pack-optin-repair",
    "notebook-record-config",
    "notebook-status",
    "notebook-audit-view",
    "audit-handoff",
    "interview",
    "classify-axis",
    "notebook-scaffold-template",
    "submit-s1",
]


class PreludeFinding(BaseModel):
    """One named substrate observation — the evidence a rung fired on.

    A finding is never a judgement: ``substrate`` names WHICH of the five was
    read, ``detail`` states the mechanical fact, and ``remedy`` (when there is a
    named one) states the repair in the repo's own vocabulary — e.g. the
    2026-07-30 live fumble, "bound but not opted in — add the packs entry".
    """

    model_config = ConfigDict(extra="forbid", title="prelude substrate finding")

    substrate: Literal["notebook-journal", "audit-config", "pack", "axes", "interview"] = Field(
        description="Which of the five prelude substrates this observation came from.",
    )
    detail: str = Field(description="The mechanical fact observed (never an interpretation).")
    remedy: str | None = Field(
        default=None,
        description=(
            "The named repair, when the finding has one (e.g. 'bound but not opted "
            "in — add the packs entry'). Null for a finding that is only disclosure."
        ),
    )


class PreludeScaffold(BaseModel):
    """The exact call the suggested action is made with.

    Either a VERB invocation (``cli`` carries the exact command line, ``spec``
    the ``--spec`` skeleton) or a FILE EDIT (``verb`` names the file, ``cli`` is
    null and ``spec`` carries the JSON fragment to add) — the pack opt-in remedy
    is the latter. ``unresolved_fields`` flags the placeholder paths the caller
    must fill, the ``scaffold-spec`` convention.
    """

    model_config = ConfigDict(extra="forbid", title="prelude action scaffold")

    verb: str = Field(
        description=(
            "The target verb to invoke, or the file to edit (e.g. `interview.json`) "
            "when the remedy is not a verb call."
        ),
    )
    cli: str | None = Field(
        default=None,
        description=(
            "The exact CLI invocation, with every derivable value filled. Null when "
            "the remedy is a file edit rather than a verb call."
        ),
    )
    spec: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The `--spec` skeleton for the target verb, or the JSON fragment to add "
            "to the named file. Null when the verb takes no spec."
        ),
    )
    unresolved_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Dotted paths inside `spec` whose values are PLACEHOLDERS the ladder "
            "could not derive from a durable record — the caller fills them before "
            "invoking (never guessed)."
        ),
    )


class PreludeSubstrates(BaseModel):
    """The disclosed evidence vector the ladder decided over.

    Every field is presence, a count, or an identity list over opaque content —
    published so a caller can see WHY a rung fired without re-reading the five
    substrates itself.
    """

    model_config = ConfigDict(extra="forbid", title="prelude substrate evidence")

    audit_ids: list[str] = Field(
        default_factory=list,
        description="Every audit_id with a notebook decision journal on disk, sorted.",
    )
    audit_id: str | None = Field(
        default=None,
        description=(
            "The audit the ladder is reporting on (the first, sorted, that a "
            "notebook rung fired for), or null when no notebook rung fired."
        ),
    )
    audit_config_recorded: bool = Field(
        default=False,
        description=(
            "True when the reported audit has a config seat — either interview.json's "
            "`audited_source` block or a journaled `notebook-audit-config` record."
        ),
    )
    audited_source_seat: bool = Field(
        default=False,
        description=(
            "True when interview.json's `audited_source` block names the reported "
            "audit's source + template (the only durable seat that does)."
        ),
    )
    notebook_passed: bool | None = Field(
        default=None,
        description=(
            "notebook-status's `passed` predicate for the reported audit, recomputed "
            "through the SAME reduction (never a second definition). Null when the "
            "source/template are not resolvable from a durable seat."
        ),
    )
    sections_awaiting: int | None = Field(
        default=None,
        ge=0,
        description=(
            "How many REQUIRED sections are not current (awaiting sign-off) for the "
            "reported audit. Null when `notebook_passed` is null."
        ),
    )
    packs_opted_in: list[str] = Field(
        default_factory=list,
        description="Pack names carried by interview.json's `packs` opt-in block, sorted.",
    )
    packs_bound: list[str] = Field(
        default_factory=list,
        description="Pack names with a CURRENT bind on their pack journal, sorted.",
    )
    axes_yaml: bool = Field(default=False, description="Whether `.hpc/axes.yaml` exists.")
    interview_json: bool = Field(
        default=False,
        description="Whether an `interview.json` exists at either probed location.",
    )
    materialized: bool = Field(
        default=False,
        description="Whether an interview.json carries a `_materialized` block.",
    )
    entry_point_run_name: str | None = Field(
        default=None,
        description=(
            "The materialized entry point's `run_name`, when one was recorded — the "
            "key the axes.yaml `executors` staleness check is made against."
        ),
    )


class SuggestPreludeActionResult(BaseModel):
    """The ONE next prelude step, its ``why``, and the call to make.

    A TOTAL ladder: every state of the five substrates maps to exactly one
    ``action``. ``rung`` is the priority the match came from (0 = a corrupt /
    unreadable substrate, which is always disclosed rather than crashed on; the
    last rung is the catch-all "the prelude is settled"). ``substrates`` is the
    evidence vector the decision was made over.
    """

    model_config = ConfigDict(extra="forbid", title="suggest-prelude-action output data")

    rung: int = Field(
        ge=0,
        description=(
            "The priority tier that matched — lower fires first. The ladder is total, "
            "so a rung always resolves (no escalation)."
        ),
    )
    action: PreludeAction = Field(description="The single next prelude step to take.")
    why: str = Field(
        description="One line naming the substrate fact this action follows from.",
    )
    scaffold: PreludeScaffold = Field(description="The exact call the action is made with.")
    findings: list[PreludeFinding] = Field(
        default_factory=list,
        description=(
            "Every named substrate observation, including the ones a lower rung "
            "pre-empted — so nothing read is silently dropped."
        ),
    )
    disclosures: list[str] = Field(
        default_factory=list,
        description=(
            "Honest notes about what could not be determined (a source the parser "
            "refused, a run the AST scan did not find, additional audits not "
            "reported). Advisory; never blocks."
        ),
    )
    substrates: PreludeSubstrates = Field(
        description="The disclosed evidence vector the ladder decided over.",
    )
