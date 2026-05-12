"""Pydantic models for the ``interview`` scaffold's wire contract.

The ``task_generator`` field is a discriminated union over five
recipe shapes (enumerated, cartesian_product, items_x_seeds,
numeric_logspace, numeric_linspace). Pydantic v2 emits this as a
``oneOf`` in the JSON schema with proper field-level constraints.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["mars", "human"]
    session_sha: str | None = Field(
        default=None,
        description="MARs session identifier. Required when kind=mars; null otherwise.",
    )
    at: str | None = Field(default=None, description="ISO-8601 timestamp.")
    operator: str | None = Field(
        default=None,
        description="Human operator name/handle when kind=human; null for MARs.",
    )


class _BudgetSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    wall_clock_max_h: float | None = Field(default=None, gt=0)


class _AbortIfSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    metric: str = Field(min_length=1)
    after_tasks: int = Field(ge=1)


class _ClusterTargetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    constraint: str | None = None


class _TranscriptTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["agent", "operator", "system"]
    text: str
    at: str | None = None  # date-time format


# ── Discriminated union for task_generator ───────────────────────────────────


class _EnumeratedParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[dict[str, Any]] = Field(
        min_length=1,
        description="List of task-kwargs dicts. Most agnostic shape — just stores the list verbatim and resolve(i) returns items[i].",
    )


class _Enumerated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["enumerated"]
    params: _EnumeratedParams


class _CartesianProductParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axes: dict[str, list[Any]] = Field(
        min_length=1,
        description="Named axes; tasks = full Cartesian product. resolve(i) returns a dict with one key per axis.",
    )

    @model_validator(mode="after")
    def _enforce_nonempty_axes(self) -> _CartesianProductParams:
        empties = [name for name, vals in self.axes.items() if not vals]
        if empties:
            raise ValueError(f"Cartesian-product axes must each have >=1 value; empty: {empties}")
        return self


class _CartesianProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["cartesian_product"]
    params: _CartesianProductParams


class _ItemsXSeedsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[dict[str, Any]] = Field(min_length=1)
    seeds: list[int] = Field(min_length=1)


class _ItemsXSeeds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["items_x_seeds"]
    params: _ItemsXSeedsParams


class _NumericLogspaceParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    param: str = Field(min_length=1)
    low: float = Field(gt=0)
    high: float = Field(gt=0)
    n: int = Field(ge=2)
    base: float = Field(default=10.0, gt=1)

    @model_validator(mode="after")
    def _enforce_low_lt_high(self) -> _NumericLogspaceParams:
        if self.low >= self.high:
            raise ValueError(
                f"numeric_logspace requires low < high; got low={self.low}, high={self.high}"
            )
        return self


class _NumericLogspace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["numeric_logspace"]
    params: _NumericLogspaceParams


class _NumericLinspaceParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    param: str = Field(min_length=1)
    low: float
    high: float
    n: int = Field(ge=2)

    @model_validator(mode="after")
    def _enforce_low_lt_high(self) -> _NumericLinspaceParams:
        if self.low >= self.high:
            raise ValueError(
                f"numeric_linspace requires low < high; got low={self.low}, high={self.high}"
            )
        return self


class _NumericLinspace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["numeric_linspace"]
    params: _NumericLinspaceParams


_TaskGenerator = Annotated[
    _Enumerated | _CartesianProduct | _ItemsXSeeds | _NumericLogspace | _NumericLinspace,
    Field(discriminator="kind"),
]


class InterviewSpec(BaseModel):
    """Structured campaign intent produced by an interview between the hpc agent and either MARs or a human.

    Deliberately bare-bones: captures *why* (goal, transcript,
    provenance), *how big* (task_count), *what tags* (task_kind),
    and *what limits* (budget, abort_if). Does NOT typed-encode the
    search space — that would narrow the existing
    experiment-agnostic tasks.py contract.
    """

    model_config = ConfigDict(extra="forbid", title="interview intent")

    goal: str = Field(min_length=1, description="Free-text campaign goal, ~one sentence.")
    task_count: int = Field(
        ge=1,
        description=(
            "Expected number of tasks. The materializer asserts "
            "tasks.total() == task_count and refuses to write "
            "interview.json on mismatch — catches off-by-one bugs in "
            "the agent-written tasks.py at the interview stage."
        ),
    )
    task_kind: str | None = Field(
        default=None,
        description=(
            "Free-text tag identifying the campaign family for "
            "recall queries ('ml-hparam-sweep', 'rl-rollout', "
            "'llm-prompt-eval', 'benchmark-perf', 'data-shard'). No "
            "enum — new families are encouraged. cmd_recall groups "
            "by this tag."
        ),
    )
    budget: _BudgetSpec | None = Field(
        default=None,
        description="Soft caps. Opaque dict — units chosen by interview agent.",
    )
    abort_if: _AbortIfSpec | None = Field(
        default=None,
        description="Optional early-stop criterion. Loose shape.",
    )
    cluster_target: _ClusterTargetSpec | None = Field(
        default=None,
        description="Optional. When present, submit_flow uses these directly. When omitted, the planner is invoked.",
    )
    produced_by: _Provenance = Field(
        description="Who/what produced this intent. Indexed by cmd_recall.",
    )
    transcript: list[_TranscriptTurn] | None = Field(
        default=None,
        description=(
            "Interview Q/A turns. Optional but strongly recommended "
            "for human interviews — the value of being able to "
            "explain 'why did this campaign target cluster X' three "
            "months later vastly outweighs the few KB of storage. "
            "For MARs interviews this is typically the agent's "
            "tool-call trace."
        ),
    )
    notes: str | None = Field(
        default=None,
        description="Free-form supplementary notes from the interview.",
    )
    task_generator: _TaskGenerator | None = Field(
        default=None,
        description=(
            "Optional opt-in: when present, the materializer "
            "regenerates tasks.py from a typed recipe instead of "
            "consuming the agent-written one. The five shapes cover "
            "common ground without locking out exotic campaigns. "
            "Materializer cross-checks the produced task count "
            "against intent.task_count before any disk writes."
        ),
    )


class _InterviewPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first: Any = Field(description="tasks.resolve(0) — opaque (whatever resolve returns).")
    mid: Any = Field(description="tasks.resolve(total_tasks // 2)")
    last: Any = Field(description="tasks.resolve(total_tasks - 1)")


class _InterviewData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_dir: str = Field(
        description="Absolute path to the campaign workdir (where tasks.py and the persisted interview.json live).",
    )
    artifacts: list[str] = Field(
        min_length=1,
        description=(
            "Relative paths (under campaign_dir) of every file the "
            "primitive wrote or updated. Always includes "
            "interview.json; includes meta.json when "
            "intent.cluster_target or intent.budget triggered an update."
        ),
    )
    total_tasks: int = Field(
        ge=1,
        description="Result of calling tasks.total() against the campaign's tasks.py. Cross-checked against intent.task_count.",
    )
    cmd_sha: str = Field(
        pattern=r"^[0-9a-f]{8,64}$",
        description="compute_cmd_sha() over the campaign's tasks.py. Embedded in interview.json so future cmd_recall queries can detect drift.",
    )
    preview: _InterviewPreview = Field(
        description="Three sample resolve() calls. Each is a dict per claude-hpc's pre-existing tasks.py contract.",
    )


class InterviewEnvelope(BaseModel):
    """Envelope returned by ``hpc-agent interview``.

    Reports the artifacts persisted (interview.json, optionally
    meta.json) and a dry-resolve preview so the calling agent can
    echo 'sweep starts here, midpoint here, ends here' back to the
    operator before submit.
    """

    model_config = ConfigDict(extra="forbid", title="interview output envelope")

    ok: Literal[True]
    data: _InterviewData
