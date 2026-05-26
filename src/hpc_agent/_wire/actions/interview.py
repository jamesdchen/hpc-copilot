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

    kind: Literal["agent", "human"]
    session_sha: str | None = Field(
        default=None,
        description="Agent/orchestrator session identifier. Required when kind=agent; null otherwise.",
    )
    at: str | None = Field(default=None, description="ISO-8601 timestamp.")
    operator: str | None = Field(
        default=None,
        description="Human operator name/handle when kind=human; null when kind=agent.",
    )

    @model_validator(mode="after")
    def _check_kind_fields(self) -> _Provenance:
        # Enforce the kind-conditional invariants the field descriptions
        # promise so a malformed provenance fails fast at the schema
        # boundary rather than leaking through to consumers.
        if self.kind == "agent" and self.session_sha is None:
            raise ValueError("provenance kind='agent' requires 'session_sha'")
        if self.kind == "human" and self.operator is None:
            raise ValueError("provenance kind='human' requires 'operator'")
        return self


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


# ── Discriminated union for entry_point ──────────────────────────────────────
#
# Universalizes how the interview learns "what's the user's experiment entry
# point?" — three shapes, normalized through one wire field. The canonical
# Python paths are ``register_run`` (the user's function carries an
# ``@register_run`` decorator the framework can discover directly) and
# ``python_module`` (an importable function the framework can introspect).
# ``shell_command`` is the *fallback* — used only when direct decoration
# isn't possible (non-Python entry point, a CLI library's decorator
# conflicts with ``@register_run``, vendor code) — and materializes a thin
# ``@register_run`` wrapper around an argv. Downstream primitives
# (classify-axis, validate-executor-signatures, submit) read the
# materialized wrapper / module path; the kind only affects the
# interview's intake.


# A pre-compiled regex for parameter-name validation; reused across kinds.
_PARAM_NAME = r"^[a-zA-Z_][a-zA-Z0-9_]*$"

# Type annotations the shell_command wrapper can declare. Kept narrow on
# purpose — these are what the wrapper's signature renders as, and what
# validate-executor-signatures can introspect against tasks.py kwargs.
# Stringly-typed wire form keeps the JSON schema simple; the materializer
# maps these to Python annotations.
_SignatureType = Literal["str", "int", "float", "bool"]


class _RegisterRunEntry(BaseModel):
    """Canonical Python entry point: an ``@register_run``-decorated function.

    The default and recommended shape for Python repos. The user puts
    ``@register_run`` directly on the function the framework should treat
    as the entry point — whether it lives in a notebook
    (``notebooks/<name>.ipynb``) or a ``.py`` file (``train.py``,
    ``main.py``). Pure pointer — no materialization. The framework's
    existing ``discover_runs`` walks the experiment to find the function
    by ``run_name``. For a mature repo with an existing entry-point
    function, this is a two-line code edit (an import and a decorator)
    and is strongly preferred over ``shell_command`` whenever direct
    decoration is possible.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["register_run"]
    run_name: str = Field(
        min_length=1,
        description="The ``@register_run`` function name to discover (in notebooks/ or .py files under the experiment).",
    )


class _PythonModuleEntry(BaseModel):
    """Canonical Python entry point: an importable module + function.

    A second canonical Python shape, alongside ``register_run``: the
    framework can already introspect importable Python, so this kind is
    a wire hint so the interview can validate the module/function exists
    before submit. No wrapper is materialized. Prefer ``register_run``
    when the user can decorate the function directly; use this kind for
    importable Python the framework should target by dotted path
    instead of by ``@register_run`` discovery.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["python_module"]
    module: str = Field(
        min_length=1,
        description="Dotted module path, e.g. ``my_pkg.train``.",
    )
    function: str = Field(
        default="main",
        min_length=1,
        description="The function inside ``module`` to treat as the entry point.",
    )


class _HaloHint(BaseModel):
    """Halo expression for a ``bounded_halo`` ``data_axis_hint``."""

    model_config = ConfigDict(extra="forbid")

    expr: str = Field(
        min_length=1,
        description=(
            "Arithmetic-only expression over the wrapper's parameters; the same "
            "restricted-AST form classify-axis already accepts. Example: "
            "``train_window * 48``."
        ),
    )


class _DataAxisHint(BaseModel):
    """Pre-declared series-axis classification.

    For shell-out wrappers ``classify-axis`` cannot introspect the body
    (it's a ``subprocess.check_call``). When the experimenter knows the
    classification — usually they do, because they wrote ``main.py`` —
    they can declare it here and the interview persists it so
    classify-axis records it directly without an introspection step.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["independent", "associative", "bounded_halo", "sequential"]
    halo: _HaloHint | None = Field(
        default=None,
        description="Required when ``kind='bounded_halo'``; otherwise must be omitted.",
    )
    monoid: Literal["sum", "moments"] | None = Field(
        default=None,
        description="Optional for ``kind='associative'``; defaults to 'moments'. Ignored for other kinds.",
    )

    @model_validator(mode="after")
    def _check_kind_specific_fields(self) -> _DataAxisHint:
        if self.kind == "bounded_halo" and self.halo is None:
            raise ValueError("data_axis_hint kind='bounded_halo' requires 'halo'")
        if self.kind != "bounded_halo" and self.halo is not None:
            raise ValueError(
                f"data_axis_hint.halo only valid when kind='bounded_halo'; got kind={self.kind!r}"
            )
        if self.kind != "associative" and self.monoid is not None:
            raise ValueError(
                f"data_axis_hint.monoid only valid when kind='associative'; got kind={self.kind!r}"
            )
        return self


class _ShellCommandEntry(BaseModel):
    """Fallback entry point: a shell command (compiled binary, decorator-conflicting CLI, ...).

    **The fallback path, used only when direct ``@register_run``
    decoration isn't possible** — non-Python entry points (shell
    scripts, compiled binaries), CLI libraries whose decorators conflict
    with ``@register_run`` (e.g. ``@hydra.main`` rewrites the signature),
    or vendor code the user can't edit. For a Python repo with an
    importable entry-point function, prefer ``register_run`` instead:
    a two-line code edit beats a subprocess shim, and the framework
    introspects the real function rather than the wrapper.

    The interview materializes a thin ``@register_run`` wrapper at
    ``.hpc/wrappers/<run_name>.py`` whose body subprocess-invokes the
    argv with kwargs substituted in. The wrapper's *signature* (built
    from ``signature``) is what downstream introspection reads;
    the underlying entry point stays opaque to the framework.

    ``frozen_configs`` lets the experimenter declare config files
    whose content is part of the experiment's identity. The
    materializer hashes each path's bytes; the interview threads
    ``<basename>_sha`` into every materialized task's kwargs so the
    framework's ``cmd_sha`` correctly distinguishes ``exp_42.yaml``
    from ``exp_43.yaml`` (and catches accidental in-place edits).

    *Constraint*: ``frozen_configs`` requires ``task_generator`` (so the
    framework has somewhere to thread the shas). A hand-written tasks.py
    plus ``frozen_configs`` is rejected at interview time — the framework
    can't safely edit the user's hand-rolled file, and silently dropping
    the shas would defeat the identity guarantee. Use ``task_generator``
    or include the shas in your own tasks.py kwargs.

    *Timing*: ``frozen_configs`` are hashed at interview time. If the
    YAML is edited between interview and submit, the stored sha is the
    interview-time content; the cluster runs whatever rsync ships. The
    window is small (interview is typically immediately before submit)
    but real — re-run the interview to refresh.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["shell_command"]
    run_name: str = Field(
        min_length=1,
        pattern=_PARAM_NAME,
        description=(
            "Name for the generated wrapper file (``.hpc/wrappers/<run_name>.py``) "
            "and the ``@register_run``-decorated function inside it. Must be a "
            "valid Python identifier."
        ),
    )
    argv: list[str] = Field(
        min_length=1,
        description=(
            "Argv template. ``{param}`` placeholders are substituted from the "
            "wrapper's kwargs at call time. Example: "
            '["python3", "main.py", "--config", "{config}", "--seed", "{seed}"].'
        ),
    )
    signature: dict[str, _SignatureType] = Field(
        default_factory=dict,
        description=(
            "Wrapper signature: ``{param_name: type_str}``. Each name must be a "
            "valid Python identifier; types are 'str' / 'int' / 'float' / 'bool'. "
            "The wrapper also takes ``**kwargs`` so framework-injected identity "
            "fields (e.g. ``config_sha``) flow through without polluting main.py."
        ),
    )
    frozen_configs: list[str] = Field(
        default_factory=list,
        description=(
            "Paths (relative to campaign_dir) to config files whose content "
            "should be part of the experiment's identity. For each entry the "
            "interview hashes the bytes and threads ``<basename>_sha`` into "
            "every task's kwargs. Requires ``task_generator`` (see class docstring)."
        ),
    )
    data_axis_hint: _DataAxisHint | None = Field(
        default=None,
        description=(
            "Pre-declared series-axis classification. classify-axis cannot "
            "introspect a shell-out body, so when the experimenter knows the "
            "classification they declare it here. The interview persists it to "
            "``interview.json._materialized.entry_point.data_axis`` so "
            "classify-axis records it directly. Omit when you want the "
            "interactive classification interview."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> _ShellCommandEntry:
        import re

        param_re = re.compile(_PARAM_NAME)
        bad = [name for name in self.signature if not param_re.match(name)]
        if bad:
            raise ValueError(
                f"shell_command.signature: invalid parameter names {bad}; "
                "each name must be a valid Python identifier"
            )
        # Every ``{placeholder}`` in argv must correspond to a declared signature
        # name. Catches the typo class at spec-validation time.
        placeholder_re = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
        referenced = {m for token in self.argv for m in placeholder_re.findall(token)}
        undeclared = referenced - set(self.signature)
        if undeclared:
            raise ValueError(
                f"shell_command.argv references parameters not in signature: "
                f"{sorted(undeclared)}; declared: {sorted(self.signature)}"
            )
        return self


_EntryPoint = Annotated[
    _RegisterRunEntry | _PythonModuleEntry | _ShellCommandEntry,
    Field(discriminator="kind"),
]


class InterviewSpec(BaseModel):
    """Structured campaign intent produced by an interview between the hpc agent and either an external orchestrator or a human.

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
            "Free-text tag the caller picks to group related campaigns "
            "for recall queries. hpc-agent does not own this "
            "vocabulary — there is no enum, no canonical set, and the "
            "framework treats it as an opaque string. Common shapes "
            "callers use today (purely as examples; you are not "
            "required to match them): 'ml-hparam-sweep', 'rl-rollout', "
            "'llm-prompt-eval', 'benchmark-perf', 'data-shard'. recall "
            "groups by exact-match on whatever string the caller wrote, "
            "so sticking with a stable vocabulary within one project "
            "makes the rollup more useful — but that's a caller-side "
            "convention, not a framework requirement."
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
            "For agent-driven interviews this is typically the agent's "
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
    entry_point: _EntryPoint | None = Field(
        default=None,
        description=(
            "Optional declaration of the experiment's entry point. Three shapes, "
            "with a strong default: ``register_run`` (the canonical Python path — "
            "a pointer to an ``@register_run``-decorated function the framework "
            "discovers directly; no materialization) and ``python_module`` (a "
            "second canonical Python path — an importable module + function) are "
            "preferred whenever a Python entry-point function is available. "
            "``shell_command`` (argv + signature; the interview materializes a "
            "``@register_run`` wrapper at ``.hpc/wrappers/<run_name>.py`` that "
            "subprocess-invokes the argv) is the **fallback**, used only when "
            "direct ``@register_run`` decoration isn't possible (non-Python entry "
            "point, decorator conflict, vendor code). Lets a mature repo with "
            "``main.py`` + frozen YAML configs participate in the same intake as "
            "a greenfield notebook — direct decoration is a two-line code edit; "
            "the wrapper is a subprocess shim that gives the framework something "
            "to introspect when the entry point itself can't be decorated."
        ),
    )


class _InterviewPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first: Any = Field(description="tasks.resolve(0) — opaque (whatever resolve returns).")
    mid: Any = Field(description="tasks.resolve(total_tasks // 2)")
    last: Any = Field(description="tasks.resolve(total_tasks - 1)")


class InterviewResult(BaseModel):
    """Data block returned by ``hpc-agent interview``.

    Reports the artifacts persisted (interview.json, optionally
    meta.json) and a dry-resolve preview so the calling agent can
    echo 'sweep starts here, midpoint here, ends here' back to the
    operator before submit.

    The outer ``{ok, data}`` envelope is supplied by ``_ok`` in
    ``hpc_agent.cli._helpers``; the shipped ``interview.output.json``
    matches THIS data-block shape, not the envelope.
    """

    model_config = ConfigDict(extra="forbid", title="interview output")

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
        description="Three sample resolve() calls. Each is a dict per hpc-agent's pre-existing tasks.py contract.",
    )
