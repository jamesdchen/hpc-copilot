"""Pydantic models for the ``notebook-dry-run`` mutate verb (the preview affordance).

Wire surface over :mod:`hpc_agent.ops.notebook.dry_run_op` — the drafting-loop
PREVIEW run: execute an audited (or standalone) percent-format ``.py`` over a small
slice of data, in the CURRENT LOCAL environment, and return a code-rendered,
deterministic per-section outcome the human reads to see "what it will do" before
committing to the audit. Experiment-AGNOSTIC: the source need not be bound to any
audit (``audit_id`` optional) — some code the LLM drafts never ends up on a cluster.

**The trust boundary this verb must NOT cross.** A dry-run is a SAMPLE, not a
proof: it journals its render receipts with ``execution_scope="sampled"``
(:data:`~hpc_agent.state.notebook_audit.EXECUTION_SCOPE_SAMPLED`), which
:func:`~hpc_agent.state.notebook_audit.read_render_receipts` filters out of the
clearing/tier path. So a sampled run can NEVER green / auto-clear an
assertion-bearing section the way a full run (``notebook-render --execute``) can —
the maintainer's explicit constraint (this slice changes no trust semantics).

**Sample bounding is a DISCLOSED CONTRACT, never a silent full run.** The cap is
exposed to the source via the ``HPC_NOTEBOOK_SAMPLE_N`` env var; core cannot
mechanically truncate an arbitrary source's inputs safely, so the env var plus a
prominent disclosure (``sample_disclosure``) IS the contract — the result says the
cap is advisory (the source must honor it), never that the data was capped for it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NotebookDryRunSpec(BaseModel):
    """Inputs to ``notebook-dry-run``.

    ``source`` is required; everything else has a small, bounded default. With an
    ``audit_id`` the run resolves the observation plan (declared observables) from
    the recorded audit config and journals sampled receipts under that scope; WITHOUT
    one it runs standalone (no ``.hpc`` state touched) — the experiment-agnostic case.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-dry-run input spec")

    source: str = Field(
        min_length=1,
        description=(
            "Experiment-relative path to the percent-format .py to preview-run "
            "(jupytext `# %%` cells; `# hpc-audit-section:` markers segment sections). "
            "Executed section-by-section in ONE namespace in the CURRENT LOCAL env."
        ),
    )
    audit_id: str | None = Field(
        default=None,
        description=(
            "Optional notebook decision-journal scope id. When PRESENT: roots + the "
            "observation plan are read from the recorded audit config, and one "
            "SAMPLED (non-clearing) render receipt is journaled per executed section. "
            "When ABSENT: standalone mode — no .hpc state is read or written (the "
            "experiment-agnostic case)."
        ),
    )
    sample_n: int = Field(
        default=50,
        ge=1,
        description=(
            "The advisory sample cap exposed to the source via the "
            "HPC_NOTEBOOK_SAMPLE_N env var. Small by default so the preview is cheap. "
            "The source must READ the var to honor it — core never silently truncates "
            "arbitrary inputs (disclosed in `sample_disclosure`)."
        ),
    )
    sections: list[str] | None = Field(
        default=None,
        description=(
            "Optional section-slug filter. When given, execution runs every section "
            "in source order UP TO AND INCLUDING the last-named one (so earlier "
            "sections a named one depends on still run); later sections are reported "
            "`skipped`. A named slug the source lacks is a loud spec_invalid. Default "
            "(null) runs every section."
        ),
    )
    timeout_sec: int = Field(
        default=300,
        ge=1,
        description=(
            "Hard wall-clock cap on the whole preview run (generous but bounded). A "
            "runaway source is abandoned and the in-progress section is reported "
            "`timeout`; the verb always returns within this bound."
        ),
    )


class NotebookDryRunAssertion(BaseModel):
    """One statically-declared ``assert`` with its EXECUTED verdict.

    Distinct from the static assertion table: ``outcome`` reflects whether the
    assert LINE actually ran in this sampled execution — ``passed`` (reached, did
    not raise), ``failed`` (reached and raised AssertionError), or ``not_run``
    (never reached — the section raised earlier, or the branch was not taken).
    """

    model_config = ConfigDict(extra="forbid", title="notebook-dry-run assertion outcome")

    test: str
    lineno: int
    msg: str | None = None
    outcome: str


class NotebookDryRunSection(BaseModel):
    """The deterministic per-section outcome of the sampled run."""

    model_config = ConfigDict(extra="forbid", title="notebook-dry-run section outcome")

    slug: str
    # One of: ran | raised | skipped | timeout.
    outcome: str
    ran: bool
    error: bool
    elapsed_sec: float
    # Verbatim tail of the traceback when the section raised (else null) — core
    # relays the source's own crash, never an interpretation of it.
    traceback_tail: str | None = None
    # Bounded tail of the section's captured stdout (else null).
    stdout_tail: str | None = None
    # sha256 of the section's captured stdout — the receipt's opaque output_sha.
    output_sha: str | None = None
    assertions: list[NotebookDryRunAssertion] = Field(default_factory=list)


class NotebookDryRunObservable(BaseModel):
    """One declared observable measured in the final namespace (audit config only)."""

    model_config = ConfigDict(extra="forbid", title="notebook-dry-run observable")

    name: str
    # The section slug executing when the value was last measured, or null.
    section: str | None = None
    atoms: dict[str, Any] = Field(default_factory=dict)


class NotebookDryRunResult(BaseModel):
    """The whole sampled-run projection: per-section outcomes + disclosures + render.

    ``markdown`` is the code-rendered artifact for VERBATIM relay (the LLM never
    interprets the run). ``executed_scope`` is always ``"sampled"`` — a reminder,
    carried into the journaled receipts, that this run cleared nothing.
    """

    model_config = ConfigDict(extra="forbid", title="notebook-dry-run output data")

    audit_id: str | None = None
    executed_scope: str = Field(
        default="sampled",
        description="Always 'sampled' — a preview run; it never clears or signs a section.",
    )
    env_disclosure: str = Field(
        description=(
            "Explicit statement that the run executed in the CURRENT LOCAL env "
            "(naming the interpreter), NOT the cluster."
        ),
    )
    interpreter: str = Field(description="The local interpreter path the run executed under.")
    sample_n: int
    sample_env_var: str = Field(
        default="HPC_NOTEBOOK_SAMPLE_N",
        description="The env var the sample cap was exposed to the source through.",
    )
    sample_disclosure: str = Field(
        description=(
            "How the sample cap was applied: the env var + advisory note that the "
            "source must honor it (no silent input truncation), and — when detectable "
            "— whether the source appeared to consume the cap."
        ),
    )
    sample_cap_consumed: bool | None = Field(
        default=None,
        description=(
            "Whether the source was observed to READ HPC_NOTEBOOK_SAMPLE_N (true), "
            "observed NOT to (false), or undetectable (null — the advisory case)."
        ),
    )
    timed_out: bool = Field(
        default=False,
        description="True when the run hit `timeout_sec` and the in-progress section was abandoned.",
    )
    sections: list[NotebookDryRunSection] = Field(
        default_factory=list,
        description="Per-section outcome, in source order.",
    )
    receipts_recorded: list[str] = Field(
        default_factory=list,
        description=(
            "Slugs a SAMPLED (non-clearing) render receipt was journaled for (empty "
            "in standalone mode — no audit_id, no journal scope)."
        ),
    )
    observables: list[NotebookDryRunObservable] = Field(
        default_factory=list,
        description="Declared observables measured in the final namespace (audit config only).",
    )
    markdown: str = Field(
        description="The code-rendered projection for VERBATIM relay. Deterministic (no timing).",
    )
