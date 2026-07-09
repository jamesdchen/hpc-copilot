"""Pydantic models for the ``interview`` scaffold's wire contract.

The ``task_generator`` field is a discriminated union over six
recipe shapes (enumerated, cartesian_product, items_x_seeds,
numeric_logspace, numeric_linspace, chunked_series). Pydantic v2 emits
this as a ``oneOf`` in the JSON schema with proper field-level constraints.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import RunIdStrict


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


def _default_items_x_seeds_items() -> list[dict[str, Any]]:
    """One no-op item, so the no-frozen-config case is a pure seed sweep.

    See :class:`_ItemsXSeedsParams` for the rationale.
    """
    return [{}]


class _ItemsXSeedsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ``items`` defaults to ``[{}]`` — one no-op item, so a caller with no
    # frozen kwargs can submit ``{"kind": "items_x_seeds", "params": {"seeds":
    # [...]}}`` and get the pure seed-sweep case (N tasks parameterised only
    # by seed). The materializer renders ``_TASKS = [{**item, 'seed': seed}
    # for item in _ITEMS for seed in _SEEDS]``; with ``_ITEMS=[{}]`` that
    # collapses to ``[{'seed': s} for s in _SEEDS]``, which is the natural
    # sweep. Explicit ``items`` still works for the cartesian case.
    items: list[dict[str, Any]] = Field(default_factory=_default_items_x_seeds_items, min_length=1)
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


class _ChunkedSeriesParams(BaseModel):
    """Tile a series ``[start + halo, series_length)`` into contiguous chunks.

    Materializes one task per chunk (crossed with ``extra_axes``), each
    carrying its ``chunk_start`` / ``chunk_end`` / ``halo`` bounds. The
    scoring space is tiled into ``chunks`` contiguous chunks; the LAST chunk
    absorbs the remainder so the final ``chunk_end`` equals ``series_length``
    exactly (no gaps, no overlaps). A run-#11-style executor replays ``halo``
    bars before each ``chunk_start`` (reading from ``chunk_start - halo``,
    which stays >= ``start``). Adds a code seat for the bounds arithmetic that
    a hand-written ``enumerated`` list only ever cross-checked by COUNT — the
    off-by-one in halo / last-chunk-end otherwise sails through.
    """

    model_config = ConfigDict(extra="forbid")

    series_length: int = Field(
        ge=1,
        description="Length of the full series; the tiled space is [start + halo, series_length).",
    )
    chunks: int = Field(
        ge=1, description="Number of contiguous chunks; the last absorbs the remainder."
    )
    halo: int = Field(
        ge=0,
        description="Bars each task replays before its chunk_start (so it reads from chunk_start - halo).",
    )
    start: int = Field(default=0, description="First index of the series (default 0).")
    extra_axes: dict[str, list[Any]] | None = Field(
        default=None,
        description="Optional named axes multiplied against the chunk grid (bucket x chunk). Each must have >=1 value.",
    )

    @model_validator(mode="after")
    def _check_bounds(self) -> _ChunkedSeriesParams:
        # Mirrors interview._validate_chunked_series so a bad recipe fails at
        # the schema boundary too (the cartesian-product empty-axes precedent).
        if self.start + self.halo >= self.series_length:
            raise ValueError(
                f"chunked_series requires start + halo < series_length; got "
                f"start={self.start}, halo={self.halo}, series_length={self.series_length}"
            )
        span = self.series_length - self.start - self.halo
        if span // self.chunks < 1:
            raise ValueError(
                f"chunked_series chunk width < 1: span {span} with chunks="
                f"{self.chunks} leaves an empty chunk"
            )
        empties = [name for name, vals in (self.extra_axes or {}).items() if not vals]
        if empties:
            raise ValueError(
                f"chunked_series extra_axes must each have >=1 value; empty: {empties}"
            )
        return self


class _ChunkedSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["chunked_series"]
    params: _ChunkedSeriesParams


_TaskGenerator = Annotated[
    _Enumerated
    | _CartesianProduct
    | _ItemsXSeeds
    | _NumericLogspace
    | _NumericLinspace
    | _ChunkedSeries,
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

# Shared description for the ``fixed_params`` field on every entry-point kind.
# A *fixed* param is one the executor requires but that does NOT vary per task
# (not a sweep axis) — e.g. ``samples`` when only ``seed`` is swept. Without
# this, resolve(i) returns only the axis params, the cluster never exports
# HPC_KW_<param>, and the executor crashes on every task (#195). The interview
# threads each entry into every materialized task's kwargs (same seam as the
# frozen-config shas), so resolve() returns the merged dict.
_FIXED_PARAMS_DESC = (
    "Constant (non-axis) kwargs the executor requires but that do not vary per "
    "task — e.g. {'samples': 10000} when only 'seed' is a sweep axis. Each is "
    "baked into every materialized task's kwargs (resolve(i)), so the cluster "
    "exports HPC_KW_<param> and the executor command is complete. Keys must be "
    "valid Python identifiers; values are JSON scalars. A swept axis of the "
    "same name wins (the axis value is per-task; this is the constant fallback). "
    "Requires task_generator — a hand-written tasks.py must include the constants "
    "itself. Resolves #195: a required signature param left uncovered."
)


def _validate_fixed_params(value: dict[str, Any]) -> dict[str, Any]:
    """Reject non-identifier keys in a ``fixed_params`` mapping."""
    import re

    name_re = re.compile(_PARAM_NAME)
    for key in value:
        if not name_re.match(key):
            raise ValueError(
                f"fixed_params key {key!r} is not a valid Python identifier; "
                "fixed params become kwargs on tasks.resolve(i)"
            )
    return value


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
    fixed_params: dict[str, Any] = Field(default_factory=dict, description=_FIXED_PARAMS_DESC)

    @model_validator(mode="after")
    def _check_fixed_params(self) -> _RegisterRunEntry:
        _validate_fixed_params(self.fixed_params)
        return self


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


class _PetscSolverHint(BaseModel):
    """Checkpoint-instrumentation hint for a PETSc-based shell_command entry point.

    A PETSc app owns its solve loop (``TSSolve``/``SNESSolve`` are library
    code), so the executor-side checkpoint helpers can't be called from a
    loop body the way ``run_iterations`` assumes. PETSc's options database is
    the injection channel instead: any app that calls ``setFromOptions()``
    honors ``PETSC_OPTIONS`` from the environment. Declaring this hint makes
    the interview materialize a checkpoint-instrumented wrapper that exports
    the per-step solution-dump option (into the stable per-task checkpoint
    dir), caps the solve at 2 steps under the checkpoint-canary probe, and —
    when ``resume_flag`` is declared — hands a previous attempt's dump back
    to the app on resume. The entry point itself stays opaque and untouched.

    ``resume_flag`` is deliberately explicit: writing checkpoints is generic,
    but *loading* one is app-specific (there is no universal PETSc restart
    option). Omit it and the wrapper still writes checkpoints; resume just
    never feeds them back.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["petsc"]
    solver_object: Literal["ts", "snes"] = Field(
        default="ts",
        description=(
            "Which PETSc object owns the solve loop: 'ts' (time stepper — the "
            "usual shape for a transient PDE solve) or 'snes' (nonlinear "
            "solver). Selects the option family the wrapper exports "
            "(-ts_monitor_solution / -snes_monitor_solution and the canary's "
            "-ts_max_steps 2 / -snes_max_it 2)."
        ),
    )
    resume_flag: str | None = Field(
        default=None,
        pattern=r"^-{1,2}[A-Za-z0-9_][A-Za-z0-9_\-]*$",
        description=(
            "The app's own restart flag (e.g. '-restart_file' or "
            "'--resume-from'). When declared, the wrapper appends "
            "``<resume_flag> <path-to-previous-attempt-dump>`` to argv on a "
            "resumed attempt. The app must interpret the file (a PETSc binary "
            "viewer dump). Omit when the app has no restart surface."
        ),
    )


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
    solver: _PetscSolverHint | None = Field(
        default=None,
        description=(
            "Optional checkpoint-instrumentation hint for a known solver "
            "library. When present, the materialized wrapper injects the "
            "library's checkpoint hooks around the argv (see "
            "``_PetscSolverHint``) so a long solve becomes preemption-safe "
            "without touching the entry point. One adapter today: 'petsc'."
        ),
    )
    fixed_params: dict[str, Any] = Field(default_factory=dict, description=_FIXED_PARAMS_DESC)

    @model_validator(mode="after")
    def _validate(self) -> _ShellCommandEntry:
        import re

        _validate_fixed_params(self.fixed_params)
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


class _AuditedSource(BaseModel):
    """Opt-in link from the campaign to an audited ``.py`` source and its audit trail (D7).

    Present only when the notebook-audit prelude produced the experiment
    code (idea → LLM draft → human audit → GRADUATION). When present the
    downstream graduation gate refuses an entry point not hash-linked to a
    current audit; when the whole field is ABSENT every notebook-audit gate
    passes silently and interview.json is byte-identical to the pre-audit
    output (the fail-safe posture).

    ``source`` and ``template`` are campaign-dir-relative paths to the
    percent-format ``.py`` (jupytext ``# %%`` cells) — the source of truth,
    not a rendered notebook. ``audit_id`` is the CALLER-authored slug that
    keys the audit's decision-journal trail
    (``.hpc/notebooks/<audit_id>.decisions.jsonl``); core never invents or
    defaults it (the fabrication class, D3). ``rendered_notebook`` is
    opaque render metadata (the caller-side jupytext/nbclient projection):
    recorded verbatim, NEVER hashed or validated by core — the audit
    identity is the ``.py`` source, not its notebook render.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        min_length=1,
        description=(
            "Campaign-dir-relative path to the audited percent-format ``.py`` "
            "source of truth (jupytext ``# %%`` cells)."
        ),
    )
    audit_id: str = Field(
        min_length=1,
        description=(
            "Caller-authored slug keying the audit's decision-journal trail "
            "(``.hpc/notebooks/<audit_id>.decisions.jsonl``). Never invented "
            "or defaulted by core — the caller owns this identity (D3)."
        ),
    )
    template: str = Field(
        min_length=1,
        description=(
            "Campaign-dir-relative path to the percent-format ``.py`` template "
            "the source was drafted from (diff-from-template is the audit view)."
        ),
    )
    rendered_notebook: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional opaque render metadata (the caller-side jupytext/nbclient "
            "notebook projection). Recorded verbatim; NEVER hashed or validated "
            "by core — the audit identity is the ``.py`` source, not its render."
        ),
    )
    # ── the CANONICAL audit configuration (full-view-recompute upgrade) ──────
    # The per-invocation ephemera the audit was run with — the ONLY ingredient of
    # a section's ``view_sha`` that was never persisted. Recording it here makes a
    # sign-off's ``view_sha`` fully recomputable by the T8 gate (the audit's lint
    # roots + presentation order are now durable, not lost with the session). All
    # three default to None so that when the block is written WITHOUT them the
    # ``exclude_none`` serialization keeps interview.json byte-identical to a
    # pre-upgrade record (a missing field reads as the conservative default: empty
    # roots, source-order presentation). Consumers coerce None → the empty form.
    input_roots: list[str] | None = Field(
        default=None,
        description=(
            "Opaque data-path roots the executes-live lint tested path literals "
            "against during the audit. Persisted so the graduation/sign-off gate "
            "recomputes the SAME lint findings (a data path that later vanishes "
            "flips the section's tier and moves its view_sha). Absent → []."
        ),
    )
    source_roots: list[str] | None = Field(
        default=None,
        description=(
            "Opaque import roots the linked-sources lint resolved imports under "
            "during the audit. Persisted alongside input_roots so the canonical "
            "view is recomputable. Absent → []."
        ),
    )
    attention_order: list[str] | None = Field(
        default=None,
        description=(
            "The section-slug presentation order the audit view used (T12). Feeds "
            "the module roll-up view_sha (it changes what the human saw). Absent → "
            "source order."
        ),
    )
    output_roots: list[str] | None = Field(
        default=None,
        description=(
            "Opaque WRITE-target roots: a path literal under one is a declared "
            "output, exempt from the executes-live not-exists flag (reported in "
            "declared_outputs, never flagged — the run-#10 output-literal noise "
            "fix). Absent → []."
        ),
    )


class ReceiptBinding(BaseModel):
    """One caller-authored receipt obligation inside a ``packs`` opt-in entry.

    Binds a caller-named *slot* (the obligation, e.g. ``"data-audit"``) to the
    *pack* whose current receipt fills it. Both are opaque slugs core never
    interprets — the slot is the caller's name for the requirement (DP4: a
    requirement always originates with the caller, never the pack), the pack is
    the name a bound pack manifest declares. Cross-pack is legal: the slot's
    ``pack`` need not equal the enclosing entry's ``pack``.

    ``receipt_bindings`` is the object form, renamed from ``required_receipts``
    in the coherence review (2026-07-07) to disambiguate it from the S6 manifest
    list ``required_receipts: [<slot slug>]`` — that list is a plain slug list on
    the pack side; this is the caller-side slot→pack binding.
    """

    model_config = ConfigDict(extra="forbid")

    slot: RunIdStrict = Field(
        min_length=1,
        description=(
            "Caller-authored slot slug — the caller's name for one receipt "
            "obligation a gate requires (opaque to core; the fabrication class "
            "forbids core inventing or defaulting one)."
        ),
    )
    pack: RunIdStrict = Field(
        min_length=1,
        description=(
            "The pack whose current receipt fills this slot (a bound-pack slug). "
            "Need not equal the enclosing entry's pack — a slot may bind cross-pack."
        ),
    )


class PackOptIn(BaseModel):
    """One domain-pack opt-in entry on the InterviewSpec ``packs`` block (bind-as-data).

    Sibling to ``audited_source``: a caller-referenced pack, persisted verbatim
    in interview.json, absent → byte-identical. ``pack`` is the pack slug (it
    keys the pack decision journal, so it is filesystem-safe); ``manifest`` is a
    campaign-dir-relative path to the pack manifest core reads and hashes (the
    ``_AuditedSource`` relpath precedent — never a blessed dir, never a search
    path, DP1). ``receipt_bindings`` lists the caller-authored slot→pack
    obligations the gate requires; empty means the pack contributes seam data
    (vocabularies, patterns) but gates on no receipt.

    Core copies the ``{pack, version, sha}`` echo verbatim onto every record
    that consumed pack content and never reads a declared value for meaning —
    identity only.
    """

    model_config = ConfigDict(extra="forbid")

    pack: RunIdStrict = Field(
        min_length=1,
        description=(
            "Pack slug — keys the pack decision journal "
            "(``.hpc/packs/<pack>.decisions.jsonl``), so it must be "
            "filesystem-safe. Opaque to core; never a core vocabulary."
        ),
    )
    manifest: str = Field(
        min_length=1,
        description=(
            "Campaign-dir-relative path to the pack manifest core reads and "
            "hashes (the ``_AuditedSource`` relpath precedent). Core never asks "
            "how the bytes got there (DP3: distribution invisible)."
        ),
    )
    receipt_bindings: list[ReceiptBinding] = Field(
        default_factory=list,
        description=(
            "Caller-authored slot→pack receipt obligations the gate requires "
            "(DP4). Empty → the pack contributes seam data but gates on no "
            "receipt. Renamed from ``required_receipts`` (coherence review) to "
            "disambiguate from the S6 manifest slug list."
        ),
    )


class ActorsBlock(BaseModel):
    """The multi-human ``actors`` opt-in on the InterviewSpec (MH1).

    Sibling to ``packs`` / ``audited_source``: a caller-declared block,
    persisted verbatim in interview.json, absent → byte-identical (the D7
    fail-safe, ``exclude_none``). Makes today's IMPLICIT single-actor identity
    EXPLICIT — an opaque slug the harness stamps and gates COMPARE, without core
    ever verifying who anyone is (the honest tier is *harness-asserted*, never
    verified).

    ``ids`` — the declared actor slugs. Each uses the shared filesystem-safe
    tag class (``RunIdStrict`` — the same ``^[A-Za-z0-9._\\-]+$`` class
    ``state/scopes.py::validate_tag`` pins), because a slug becomes a PATH
    SEGMENT in the per-actor utterance-log locator (MH2): the shape is
    load-bearing, not stylistic. Opaque to core — NEVER a role vocabulary: core
    has no idea what a "PI", "postdoc", or "reviewer" is and no field may ever
    carry those words (the caller-vocabulary rule).

    **Default-single-actor semantics (the D7 posture, exactly):** an absent
    block, or ``ids`` with fewer than two entries, means every identity
    comparison and policy consultation in every gate returns silently,
    byte-identical to today. Zero declared actors is not an error, not a
    warning — it is today's system; so ``ids`` may be empty.

    ``policy`` — optional delegation mapping (MH8): ``{<gated block name>:
    [<actor slug>, ...]}``. Keys are existing gated block names core already
    owns (``"notebook-sign-off"``, ``"campaign-greenlight"``, ``"scope-unlock"``,
    ``"registration"`` when the sibling kernel lands) — opaque strings core
    membership-tests, never a closed vocabulary pinned here. Values are subsets
    of ``ids``. Absent → no policy gating. A policy entry naming an actor NOT in
    ``ids`` is a LOUD refusal at validation time (the dangling-reference
    posture: an opted-in reference core cannot resolve must never silently
    pass) — deliberately NOT D7 silence, which belongs to the un-opted-in world.

    **How a session knows its actor is harness configuration** (``HPC_ACTOR``),
    NOT a spec field: an agent-suppliable actor would let the model choose its
    identity. The actor arrives from outside the model's tool surface, exactly
    like the utterance text itself.
    """

    model_config = ConfigDict(extra="forbid")

    ids: list[RunIdStrict] = Field(
        default_factory=list,
        description=(
            "Declared actor slugs (the shared filesystem-safe tag class — each "
            "becomes a path segment in the per-actor utterance-log locator). "
            "Opaque to core; never a role vocabulary. Fewer than two entries → "
            "single-actor semantics (comparisons stay off), byte-identical to "
            "today. May be empty (zero declared actors is not an error)."
        ),
    )
    policy: dict[str, list[RunIdStrict]] | None = Field(
        default=None,
        description=(
            "Optional delegation mapping {<gated block name>: [<actor slug>, "
            "...]} (MH8). Keys are existing gated block names core "
            "membership-tests (opaque strings, never a closed vocabulary); "
            "values are subsets of ``ids``. Absent → no policy gating. A value "
            "slug not in ``ids`` is refused at validation (the dangling-reference "
            "posture). Pure lists+mappings core COMPARES, never evaluates — never "
            "a predicate, a role vocabulary, or a quorum scheme."
        ),
    )

    @model_validator(mode="after")
    def _check_policy_slugs_declared(self) -> ActorsBlock:
        # The dangling-reference refusal (MH1): a policy value naming an actor
        # not in ``ids`` is an opted-in reference core cannot resolve — LOUD at
        # validation, never a silent pass. Core compares identity only; it never
        # interprets WHY the lab granted a block to an actor.
        if self.policy is None:
            return self
        declared = set(self.ids)
        for block, allowed in self.policy.items():
            dangling = [slug for slug in allowed if slug not in declared]
            if dangling:
                raise ValueError(
                    f"actors.policy[{block!r}] names actor(s) {sorted(dangling)!r} "
                    f"not in actors.ids ({sorted(declared)!r}); a policy entry may "
                    f"only reference declared actors (add them to ids or drop them "
                    f"from the policy)."
                )
        return self


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
            "consuming the agent-written one. The six shapes cover "
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
    audited_source: _AuditedSource | None = Field(
        default=None,
        description=(
            "Opt-in link to an audited ``.py`` source and its audit trail (D7). "
            "When present, the graduation gate refuses an entry point not "
            "hash-linked to a current audit; when ABSENT every notebook-audit "
            "gate passes silently and interview.json is byte-identical to the "
            "pre-audit output. The caller authors the ``audit_id`` slug — core "
            "never invents or defaults it (the fabrication class)."
        ),
    )
    packs: list[PackOptIn] | None = Field(
        default=None,
        description=(
            "Opt-in domain-pack bindings (bind-as-data). Sibling to "
            "``audited_source``: when present, each entry references a pack "
            "manifest (relpath core reads + hashes) and the caller-authored "
            "receipt-binding slots a gate requires; when ABSENT every pack gate "
            "returns silently and interview.json is byte-identical to a repo "
            "that never opted in (the D7 fail-safe). Core copies the opaque "
            "``{pack, version, sha}`` echo verbatim and never reads a declared "
            "pack value for meaning."
        ),
    )
    actors: ActorsBlock | None = Field(
        default=None,
        description=(
            "Opt-in multi-human actor declaration (MH1). Sibling to ``packs`` / "
            "``audited_source``: when present, ``ids`` names the shared repo's "
            "actor slugs and optional ``policy`` delegates gated blocks to actor "
            "subsets; when ABSENT (or with fewer than two ids) every identity "
            "comparison and policy consultation returns silently and "
            "interview.json is byte-identical to today's single-actor system "
            "(the D7 fail-safe). Slugs are opaque to core — never a role "
            "vocabulary; the attribution tier is harness-asserted, never "
            "verified. A session's own actor arrives via the HPC_ACTOR harness "
            "config, never this spec (the model must not choose its identity)."
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
        description="Absolute path to the campaign workdir. interview.json is written here at the root; generator-mode tasks.py is materialized into campaign_dir/.hpc/tasks.py (the canonical framework location deploy + the dispatcher read).",
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
