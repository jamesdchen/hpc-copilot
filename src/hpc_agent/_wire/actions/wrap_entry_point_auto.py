"""Pydantic models for the ``wrap-entry-point-auto`` composite scaffold.

``wrap-entry-point-auto`` collapses the deterministic head of the
``hpc-wrap-entry-point`` skill — ``detect-entry-point`` → the pathway
decision table → ``decorate-entry-point`` → the frozen-YAML convention
scan → the fixed-params partition — into ONE call. The LLM makes one tool
call and only does work on the genuine judgment points: an entry-point /
entry-function tie, the human-owned intent fields, and the argv template
for a non-introspectable wrapper entry point.

The result is a discriminated union over FOUR terminal shapes. One is
terminal-success (``onboarded``); the other three are *named escalations*
— each states exactly which field is missing and why code must not invent
it (the contract-taught-by-refusal posture: never a generic refusal).

* ``{onboarded: true, ...}`` — every deterministic piece is composed and
  the ``interview_spec`` fragment is ready for the ``interview`` verb.
* ``{needs_pick: true, candidates, ...}`` — two or more entry-point files
  (or two or more candidate entry FUNCTIONS in the picked file) matched
  and no caller pick scoped it. Picking wrong here is non-recoverable
  without the user noticing, so the composite refuses to guess.
* ``{needs_intent: true, missing_fields, ...}`` — a human-owned field is
  absent (``goal`` / ``task_generator`` / ``task_count``, or a specific
  uncovered required param of the entry point's signature). These are
  ``REQUIRED_CALLER_FIELDS``-class values: code NEVER fabricates one.
* ``{needs_wrapper_argv: true, argv_kind, ...}`` — the pathway is wrapper
  materialization and the entry point's CLI surface is not introspectable
  by this verb, so the ``argv`` template + typed ``signature`` must come
  from the caller. It CARRIES the ``argv_extraction`` verdict +
  ``argv_params`` the in-process ``detect-entry-point`` already produced,
  so the caller composes the template from them instead of paying for a
  second detect call. When the entry point is an importable module this
  shape also DISCLOSES the ``python_module`` alternative SKILL.md:98
  offers for the same row, with the derived dotted target named.

All three InterviewSpec entry-point kinds are representable:
``register_run`` (the ``decorate`` pathway, the default), ``shell_command``
(the ``wrapper`` pathway), and ``python_module`` (the ``module`` pathway).
Only the first two are ever CHOSEN by code — ``python_module`` is reachable
by explicit ``entry_point_kind`` override, because what distinguishes it
from direct decoration is "may we edit this file", which is caller
judgment rather than a repo fact.

``task_generator`` reuses the interview wire's exact discriminated union
so a caller-supplied recipe is validated byte-identically to what the
``interview`` primitive enforces — the axis-param derivation this verb
does is then reading a shape the downstream materializer agrees with.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

# Reuse the exact interview recipe union + wrapper-signature type set so a
# caller-supplied task_generator / signature is validated identically to what
# the interview primitive enforces one verb downstream. ``_DataAxisHint`` and
# ``_PetscSolverHint`` are reused for the same reason: both are copied through
# VERBATIM onto the composed ``shell_command`` entry block, so validating them
# against any shape other than the interview's own would let a hint pass here
# and fail one verb later. Re-bound as module-level aliases below because the
# composite's implementation module constructs a ``_PetscSolverHint`` for the
# DETECTED solver — it reads its own wire module, never another verb's.
from hpc_agent._wire.actions.interview import (
    _DataAxisHint,
    _PetscSolverHint,
    _SignatureType,
    _TaskGenerator,
)

#: Public aliases for the two interview shapes this verb copies through. The
#: composite's implementation module composes a DETECTED solver hint
#: (``detect-entry-point`` reports ``solver: "petsc"`` on a candidate), so it
#: needs the class — and it reads THIS module for it rather than reaching into
#: another verb's private names across packages. The emitted ``$defs`` key is
#: still the defining class name, so the schema matches ``interview.input.json``
#: exactly.
DataAxisHint = _DataAxisHint
PetscSolverHint = _PetscSolverHint

# ── Input ────────────────────────────────────────────────────────────────


class WrapEntryPointAutoInput(BaseModel):
    """Inputs to the ``wrap-entry-point-auto`` composite verb.

    Every field is optional: a bare call against a repo with exactly one
    entry point and one candidate function gets as far as the first
    genuine judgment point and names it. Caller-supplied fields are
    OVERRIDES — they short-circuit the matching detection step, they never
    merely hint at it.
    """

    model_config = ConfigDict(extra="forbid", title="wrap-entry-point-auto input")

    # -- entry-point resolution overrides ---------------------------------
    entry_point_path: str | None = Field(
        default=None,
        description=(
            "The entry-point file, relative to experiment_dir. Overrides "
            "detection. When omitted the composite resolves it from "
            "detect-entry-point: an existing @register_run wins, else the "
            "single candidate; two or more candidates return needs_pick."
        ),
    )
    run_name: str | None = Field(
        default=None,
        description=(
            "The entry FUNCTION to decorate (direct-decoration pathway) or to "
            "name the wrapper after (wrapper pathway). Overrides the "
            "entry-function ladder. On the decoration pathway it must be a "
            "module-level def in the resolved entry-point file."
        ),
    )
    entry_point_kind: Literal["register_run", "shell_command", "python_module"] | None = Field(
        default=None,
        description=(
            "Force the pathway — all THREE InterviewSpec entry-point kinds are "
            "reachable. 'shell_command' is the last row of the pathway decision "
            "table: an explicit caller choice always routes to wrapper "
            "materialization, even when direct decoration would have been "
            "structurally possible. 'python_module' targets the function by "
            "dotted path WITHOUT editing the file (SKILL.md:98's second option "
            "for row 2) — code never selects it on its own, because 'may we edit "
            "this file' is caller judgment, not a repo fact. 'register_run' is "
            "already the default; forcing it is refused when the function "
            "carries a signature-rewriting decorator (decorating through one "
            "produces an executor the framework cannot introspect)."
        ),
    )

    # -- wrapper-pathway overrides ---------------------------------------
    argv: list[str] | None = Field(
        default=None,
        description=(
            "The wrapper's argv template with {placeholder} per kwarg. Supply "
            "it (with signature) to clear the needs_wrapper_argv escalation; "
            "this verb never derives it (framework-specific CLI extraction is "
            "a separate unit, and a wrong argv fails every task)."
        ),
    )
    signature: dict[str, _SignatureType] | None = Field(
        default=None,
        description=(
            "The wrapper's typed signature, {placeholder: type-name}. Types are "
            "the same narrow set the interview's shell_command entry accepts, so "
            "a bad type fails here rather than one verb later. Supply it with "
            "argv. Doubles as the param inventory the fixed-params partition "
            "reads on the wrapper pathway (no introspectable Python signature "
            "exists there)."
        ),
    )

    data_axis_hint: DataAxisHint | None = Field(
        default=None,
        description=(
            "Pre-declared series-axis classification, copied VERBATIM onto the "
            "composed shell_command entry block. WRAPPER PATHWAY ONLY (#260): a "
            "wrapper body is a subprocess call classify-axis cannot introspect, "
            "which is exactly why the hint is load-bearing there — and exactly "
            "why the interview's register_run / python_module shapes carry no "
            "such field (both are introspectable, so classify-axis reads the "
            "real function). Supplying it on an introspectable pathway is a "
            "NAMED caller error, never a silently dropped field."
        ),
    )
    solver: PetscSolverHint | None = Field(
        default=None,
        description=(
            "Checkpoint-instrumentation hint for a known solver library, copied "
            "VERBATIM onto the composed shell_command entry block (wrapper "
            "pathway only — the other two entry-point shapes carry no solver "
            "field). Overrides detection: when omitted and detect-entry-point "
            "recognized the library in the entry point's source (it reports "
            "``solver: 'petsc'`` on the candidate), the composite fills in that "
            "adapter's default hint rather than dropping the detection on the "
            "floor. Supply it explicitly to pick a non-default ``solver_object`` "
            "or declare the app's ``resume_flag``."
        ),
    )

    # -- human-owned intent (NEVER invented by code) ----------------------
    goal: str | None = Field(
        default=None,
        description=(
            "One-line free-text intent. A REQUIRED_CALLER_FIELDS member: its "
            "absence is an escalation (needs_intent), never an auto-fill."
        ),
    )
    task_generator: _TaskGenerator | None = Field(
        default=None,
        description=(
            "The sweep recipe. A REQUIRED_CALLER_FIELDS member the framework "
            "cannot fabricate. Also the source of the axis-param set the "
            "fixed-params partition subtracts, so its absence blocks the "
            "partition too."
        ),
    )
    task_count: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Expected number of tasks — the interview's off-by-one guard. "
            "Human-owned magnitude; absence is an escalation."
        ),
    )

    # -- deterministic-step overrides -------------------------------------
    frozen_configs: list[str] | None = Field(
        default=None,
        description=(
            "Override the convention YAML scan (configs/*.yaml, configs/*.yml, "
            "conf/*.yaml). An explicit empty list means 'no YAML is part of "
            "this experiment's identity' and is honored as such."
        ),
    )
    fixed_params: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Constants for entry-point params the task_generator does not vary. "
            "Keys here CLEAR the matching uncovered-required-param escalation; "
            "the composite copies them through verbatim and never invents one."
        ),
    )

    # -- chain carry (onboard chain, P2.c) ---------------------------------
    audited_source: dict[str, Any] | None = Field(
        default=None,
        description=(
            "OPAQUE CARRY, never read by this verb: the audit-handoff projection's "
            "audited_source block ({source, audit_id, template, roots}), threaded "
            "through so the `interview` two hops later can declare the audit "
            "provenance the chain already produced. Echoed verbatim on the "
            "onboarded result. Same posture as notebook-status's `review` field "
            "(P2.b): a chain that discards a product it built one hop earlier "
            "forces the next block to re-derive it."
        ),
    )


# ── Result: shared sub-shapes ────────────────────────────────────────────


class _BlockSurface(BaseModel):
    """The block-chain surface every ``wrap-entry-point-auto`` shape carries (P2.c).

    ``wrap-entry-point-auto`` became a BLOCK of the ``onboard`` chain, so each of
    its four discriminated shapes declares the same three fields every other block
    Result declares: which terminator it reached, whether an actor is owed an
    answer there, and the deterministic successor (null at every escalation — all
    of them are parks). Declared ONCE here and mixed in, so a shape cannot acquire
    a different vocabulary for the same question.
    """

    model_config = ConfigDict(extra="forbid")

    stage_reached: str = Field(
        description=(
            "The terminator this call stopped at: `onboarded`, `needs_pick`, "
            "`needs_intent`, `needs_wrapper_argv` (the AGENT park — params were "
            "mechanically extracted, so the argv template is transcription) or "
            "`needs_wrapper_argv_unsupported` (the HUMAN park — nothing was "
            "extractable, so the argv shape is the caller's own knowledge of "
            "their tool)."
        ),
    )
    needs_decision: bool = Field(
        description=(
            "True at every escalation (an actor — human or LLM — is owed an "
            "answer), False at `onboarded`."
        ),
    )
    brief: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The code-composed ONE ask a park relays: the candidates for a pick, "
            "the field list for missing intent, the extracted params + argv head "
            "for a wrapper argv. Assembled from this result's own fields — the "
            "park never asks for something the composite already derived. Empty "
            "at `onboarded`."
        ),
    )
    next_block: dict[str, Any] | None = Field(
        default=None,
        description=(
            "`{verb, why, spec_hint}` at `onboarded` (the spec_hint IS the "
            "`interview` verb's complete input spec — the onboard chain is "
            "ungated, so the driver passes it verbatim), null at every escalation."
        ),
    )


class _EntryCandidate(BaseModel):
    """One entry-point (or entry-function) candidate in a needs_pick tie."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="Entry-point path relative to experiment_dir.")
    argv_kind: str = Field(
        description=(
            "The candidate's classified CLI surface as detect-entry-point read "
            "it: argparse / click / typer / hydra / fire / __main__ / "
            "console_script / shell — or 'caller_supplied' for a path the "
            "detection scan never surfaced (the caller named it), which carries "
            "no file-level classification."
        ),
    )
    function: str | None = Field(
        default=None,
        description=(
            "The module-level function name, set only on an entry-FUNCTION tie "
            "(several candidate defs inside one resolved file)."
        ),
    )


class _PythonModuleTarget(BaseModel):
    """A ``python_module`` entry point: an importable dotted module + function."""

    model_config = ConfigDict(extra="forbid")

    module: str = Field(
        min_length=1,
        description=(
            "Dotted module path importable with the campaign dir on sys.path — "
            "derived from the file's location, and only ever offered when every "
            "parent directory carries an __init__.py (or the file sits at the "
            "repo root). A src-layout package is NOT importable from the repo "
            "root, so no dotted path is offered for one."
        ),
    )
    function: str = Field(
        min_length=1, description="The function inside `module` to treat as the entry point."
    )


class _ParamPartition(BaseModel):
    """The fixed-params partition over the entry point's declared params."""

    model_config = ConfigDict(extra="forbid")

    all_params: list[str] = Field(
        description="Every param the entry point declares, in declaration order."
    )
    axis_params: list[str] = Field(
        description=(
            "Params the task_generator produces per task (plus the "
            "<stem>_sha kwargs the framework threads for each frozen YAML). "
            "Handled per-task — deliberately absent from fixed_params."
        ),
    )
    defaulted_params: list[str] = Field(
        description=(
            "Params carrying a default in the entry point's own signature. "
            "Safe to omit; a caller MAY still pin one for reproducibility."
        ),
    )
    uncovered_params: list[str] = Field(
        description=(
            "Required (no default), not an axis, and not covered by the "
            "caller's fixed_params. Every task would fail on these, so a "
            "non-empty list is a needs_intent escalation, never a guess."
        ),
    )
    accepts_var_keyword: bool = Field(
        description=(
            "True when the entry point declares **kwargs, which absorbs any "
            "unmatched kwarg — no param can then be 'uncovered'."
        ),
    )


# ── Result: the four terminal shapes ─────────────────────────────────────


class _OnboardedResult(_BlockSurface):
    """Terminal success: every deterministic step composed."""

    onboarded: Literal[True] = Field(
        description=(
            "Discriminator: the entry point is resolved, the pathway decided, "
            "decoration applied (direct-decoration pathway), the frozen YAMLs "
            "scanned and the params partitioned. interview_spec is ready."
        ),
    )
    pathway: Literal["decorate", "wrapper", "module"] = Field(
        description=(
            "'decorate' = @register_run spliced onto the user's function "
            "(SKILL Step 3a, the default). 'wrapper' = the .hpc/wrappers "
            "shell_command shim (Step 3b, the rescue boat). 'module' = the "
            "python_module dotted-path target, reachable ONLY by explicit "
            "caller override (SKILL.md:98's second option for row 2) — it "
            "writes nothing at all."
        ),
    )
    pathway_rule: str = Field(
        min_length=1,
        description="The pathway-table row id that decided the pathway (auditable).",
    )
    entry_point_kind: Literal["register_run", "shell_command", "python_module"] = Field(
        description="The interview entry_point.kind implied by the pathway.",
    )
    entry_point_path: str = Field(
        min_length=1, description="Resolved entry-point path relative to experiment_dir."
    )
    entry_point_rule: str = Field(
        min_length=1,
        description="Which rung of the entry-point/function ladder resolved the pick.",
    )
    run_name: str = Field(min_length=1, description="The decorated function / the wrapper's name.")
    argv_kind: str = Field(description="The resolved entry point's classified CLI surface.")
    decorated: bool = Field(
        description="True when this call spliced @register_run onto the function."
    )
    already_decorated: bool = Field(
        description="True when the function already carried @register_run (no write)."
    )
    import_added: bool = Field(
        description="True when the `from hpc_agent import register_run` import was inserted."
    )
    frozen_configs: list[str] = Field(
        description="Frozen YAMLs (caller override, else the convention scan).",
    )
    frozen_sha_params: list[str] = Field(
        description=(
            "The <stem>_sha kwarg names the framework threads for each frozen "
            "YAML — counted as covered in the partition."
        ),
    )
    fixed_params: dict[str, Any] = Field(
        description=(
            "Copied VERBATIM from the caller. Empty when the caller supplied "
            "none — this verb never synthesizes a constant for an entry-point "
            "param."
        ),
    )
    partition: _ParamPartition = Field(description="The fixed-params partition (SKILL Step 5b).")
    audited_source: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The opaque `audited_source` carry echoed back verbatim (onboard "
            "chain, P2.c) — present only when the caller threaded one in. The "
            "chain's interview composer folds it into the InterviewSpec so the "
            "audit provenance survives the hop; this verb never reads it."
        ),
    )
    interview_spec: dict[str, Any] = Field(
        description=(
            "The composed InterviewSpec fragment — goal / task_count / "
            "task_generator / entry_point / produced_by — SUBMITTABLE as-is to "
            "the `interview` primitive (interview.input.json requires "
            "produced_by, so a fragment omitting it could never be handed on "
            "unedited). produced_by is the minimal {kind: 'human'} SUGGESTION: "
            "the interview's own P1.c composer fills .operator from git config "
            "and discloses it as a composed default, so attribution stays "
            "required — this verb declares WHO-CLASS, never the identity."
        ),
    )


class _NeedsPickResult(_BlockSurface):
    """Terminal escalation: an entry-point or entry-function tie."""

    needs_pick: Literal[True] = Field(
        description=(
            "Discriminator: two or more candidates matched with no caller pick. "
            "NOTHING was written. The skill refuses to silently pick across "
            "main.py / train.py / run.py — a wrong pick is non-recoverable "
            "without the user noticing."
        ),
    )
    reason: Literal["entry_point_tie", "entry_function_tie"] = Field(
        description=(
            "'entry_point_tie' = several entry-point FILES matched. "
            "'entry_function_tie' = one file, several candidate module-level "
            "defs and no convention (@register_run / `main`) to break it."
        ),
    )
    candidates: list[_EntryCandidate] = Field(
        min_length=2,
        description="Every tied candidate, in detect-entry-point's probe order.",
    )
    resolve_with: str = Field(
        min_length=1,
        description="The exact input field that resolves the tie (e.g. 'entry_point_path').",
    )
    ask: str = Field(
        min_length=1,
        description="The precise, named ask: which value is needed and why code must not pick it.",
    )
    entry_point_path: str | None = Field(
        default=None,
        description="The already-resolved entry-point file, set on an entry_function_tie.",
    )


class _NeedsIntentResult(_BlockSurface):
    """Terminal escalation: a human-owned field is missing."""

    needs_intent: Literal[True] = Field(
        description=(
            "Discriminator: one or more human-owned fields are absent. The "
            "deterministic context below is composed and echoed so the caller "
            "does not re-scan, but no intent value is invented."
        ),
    )
    missing_fields: list[str] = Field(
        min_length=1,
        description=(
            "The absent fields, dotted as the caller supplies them: 'goal', "
            "'task_generator', 'task_count', "
            "'entry_point.fixed_params.<param>'."
        ),
    )
    never_invented: list[str] = Field(
        description=(
            "The subset of missing_fields that code must NEVER fabricate under "
            "any 'safe default' rationale (the REQUIRED_CALLER_FIELDS class "
            "plus every uncovered entry-point param). Pinned so a future "
            "auto-fill has to delete this field to happen."
        ),
    )
    ask: str = Field(
        min_length=1,
        description="The precise, named ask — every missing field and why it is caller-owned.",
    )
    pathway: Literal["decorate", "wrapper", "module"] = Field(
        description="The already-decided pathway, echoed so the caller need not re-derive it.",
    )
    entry_point_path: str = Field(min_length=1, description="The resolved entry-point path.")
    run_name: str = Field(min_length=1, description="The resolved entry function / wrapper name.")
    argv_kind: str = Field(description="The resolved entry point's classified CLI surface.")
    partition: _ParamPartition | None = Field(
        default=None,
        description=(
            "The fixed-params partition, present only when it could be "
            "computed — i.e. the escalation is an uncovered required param "
            "rather than an absent task_generator."
        ),
    )


class _NeedsWrapperArgvResult(_BlockSurface):
    """Terminal escalation: the wrapper pathway needs a caller argv + signature."""

    needs_wrapper_argv: Literal[True] = Field(
        description=(
            "Discriminator: direct decoration is structurally blocked and the "
            "entry point's CLI surface is not introspectable by this verb, so "
            "the argv template + typed signature must come from the caller. "
            "NOTHING was written."
        ),
    )
    argv_kind: str = Field(
        min_length=1,
        description=(
            "Why the surface is not introspectable, named: shell / "
            "console_script (no Python signature at all), hydra / click / "
            "typer (a decorator rewrote the signature), argparse / __main__ "
            "(the flags live in a parser this verb does not read)."
        ),
    )
    pathway_rule: str = Field(
        min_length=1, description="The pathway-table row id that routed here (auditable)."
    )
    entry_point_path: str = Field(min_length=1, description="The resolved entry-point path.")
    run_name: str = Field(min_length=1, description="The name the wrapper will be materialized as.")
    argv_head: list[str] = Field(
        description=(
            "The leading argv elements code CAN compose from the entry-point "
            "shape (['python3', 'train.py'] / ['python3', '-m', 'pkg'] / "
            "['mytool'] / ['./run.sh']). The caller appends the "
            "{placeholder} flags."
        ),
    )
    argv_extraction: Literal["extracted", "unsupported"] = Field(
        description=(
            "detect-entry-point's mechanical-parameter verdict for this entry "
            "point, CARRIED THROUGH rather than re-derived. The composite ran "
            "the scan in-process to get here, so dropping the verdict would "
            "force the caller into a second detect-entry-point call — the "
            "produce→consume seam this composite exists to close. "
            "'unsupported' also covers an entry point the caller NAMED that the "
            "scan never surfaced (no classified surface to extract from)."
        ),
    )
    argv_params: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "The mechanically extracted CLI parameters when argv_extraction == "
            "'extracted', else null. Same shape detect-entry-point emits — see "
            "the ArgvParam $def in schemas/detect_entry_point.output.json and "
            "the two-honesty-levels rule in hpc_agent/ops/argv_extract.py; not "
            "restated here, because a second copy of that shape would drift "
            "from the extractor. A param carrying an 'unextracted' marker still "
            "needs a source read for the argument it names."
        ),
    )
    missing_fields: list[str] = Field(
        min_length=1, description="The absent wrapper fields: 'argv' and/or 'signature'."
    )
    missing_intent_fields: list[str] = Field(
        description=(
            "Human-owned fields ALSO still absent — disclosed here so one "
            "round trip gathers everything instead of two sequential "
            "escalations."
        ),
    )
    python_module_alternative: _PythonModuleTarget | None = Field(
        default=None,
        description=(
            "DISCLOSURE, not a recommendation: SKILL.md:98 offers python_module "
            "as the OTHER option for this row, and when the entry point is an "
            "importable module the derived {module, function} is named here so "
            "the caller can pass entry_point_kind='python_module' instead of an "
            "argv template. Code does not choose it, because the discriminator "
            "is 'may we edit / must we not edit this file' — caller judgment, "
            "not a repo fact. Absent when no dotted path is importable from the "
            "campaign dir (a src-layout package, or a non-Python entry point)."
        ),
    )
    ask: str = Field(
        min_length=1,
        description="The precise, named ask: the argv shape needed and why it is not derivable.",
    )


# Discriminated union over the four terminal shapes. RootModel so a
# ``*Result``-suffixed BaseModel is discovered by build_schemas.py and
# emits an ``anyOf`` top-level schema in
# ``wrap_entry_point_auto.output.json``. The CLI dispatcher emits
# whichever shape the composite returns as the envelope ``data`` block.
class WrapEntryPointAutoResult(
    RootModel[_OnboardedResult | _NeedsPickResult | _NeedsIntentResult | _NeedsWrapperArgvResult]
):
    """Discriminated result: onboarded, or one of three named escalations."""
