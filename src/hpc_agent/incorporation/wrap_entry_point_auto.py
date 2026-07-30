"""``wrap-entry-point-auto``: one composite verb for the deterministic head of
the ``hpc-wrap-entry-point`` skill (prelude mechanization P1.a).

The bug class this kills: the skill's Steps 0–5b are a strict
producer→consumer chain — the detection scan produces the candidate list
the pathway table consumes, the pathway table decides which verb runs
next, the picked entry point's signature is what the fixed-params
partition partitions — but three of those steps existed only as PROSE
TABLES (SKILL.md lines 93-104, 147-159, 180-193). A prose table is
re-derived by hand on every run, so an agent can (and did, in the
sibling ``classify-axis-auto`` incident) mis-sequence it or silently
resolve a row it should have escalated. Deterministic sequencing belongs
in code; only the genuine judgment stays with the caller.

This primitive collapses the chain into ONE call. It imports and calls
the sub-verbs DIRECTLY (no subprocess fan-out):

* :func:`hpc_agent.ops.detect_entry_point.detect_entry_point` — the
  six-probe repo scan (candidates + ``argv_kind`` + ``@register_run``
  decoration on disk).
* :func:`hpc_agent.incorporation.decorate_entry_point.decorate_entry_point`
  — the bounded AST line-splice that inserts the import + decorator.

and promotes the three prose tables to code:

* the **pathway decision table** (SKILL.md:93-104) →
  :func:`_decide_pathway`;
* the **frozen-YAML convention scan** (SKILL.md:147-159) →
  :func:`_scan_frozen_configs`;
* the **fixed-params partition** (SKILL.md:180-193) →
  :func:`_partition_params`.

Internal sequence
-----------------

1. **detect** → resolve the entry-point FILE off
   ``detect-entry-point``'s ``candidates`` / ``decoration_found``
   (caller ``entry_point_path`` wins; an existing ``@register_run``
   next; then the sole candidate). A greenfield repo raises
   ``SpecInvalid`` naming ``build-template``.
2. **resolve the entry FUNCTION** (Python entry points only) off a
   named ladder: caller ``run_name`` → the already-``@register_run``'d
   def → a def named ``main`` → the sole public module-level def.
3. **pathway table** → ``decorate`` (SKILL Step 3a, the default),
   ``wrapper`` (Step 3b, the rescue boat), or ``module``
   (SKILL.md:98's ``python_module`` — caller override only), with the
   deciding row id recorded.
4. **frozen-YAML scan** by convention (caller override wins).
5. **fixed-params partition** — axis params (from the ``task_generator``
   plus the framework's ``<stem>_sha`` kwargs) vs. defaulted vs.
   uncovered-required.
6. **decorate** — the ONLY write, and it happens LAST, after every
   escalation branch has been ruled out. Every non-``onboarded`` return
   therefore leaves the repo byte-identical.

Discriminated return
--------------------

``{onboarded}`` plus three NAMED escalations. Each escalation states
exactly which value is needed and why code must not produce it — the
contract-taught-by-refusal posture, never a generic refusal:

* ``{needs_pick}`` — an entry-point-file or entry-function tie. Picking
  wrong across ``main.py`` / ``train.py`` / ``run.py`` is not recoverable
  without the user noticing, so the composite refuses to guess.
* ``{needs_intent}`` — ``goal`` / ``task_generator`` / ``task_count``, or
  a specific uncovered required param. These are the
  ``REQUIRED_CALLER_FIELDS`` class (``ops/submit/field_partition.py``):
  code NEVER fabricates one under any "safe default" rationale.
* ``{needs_wrapper_argv}`` — the wrapper pathway needs the ``argv``
  template + typed ``signature``, and this verb does not read a
  framework's parser to derive them. It DOES compose the deterministic
  ``argv_head`` (``["python3", "train.py"]`` / ``["python3", "-m",
  "pkg"]`` / ``["mytool"]`` / ``["./run.sh"]``) so the caller only
  supplies the flags; it CARRIES the ``argv_extraction`` verdict +
  ``argv_params`` the step-1 scan already produced (for argparse / click
  the flag names, types and defaults are read straight off the AST, so
  the caller composes the template from them instead of paying for a
  second ``detect-entry-point`` call); and it NAMES the ``python_module``
  alternative (see below) when one is importable.

All three ``InterviewSpec`` entry-point kinds are REPRESENTABLE
-------------------------------------------------------------

``register_run`` (the ``decorate`` pathway), ``shell_command`` (the
``wrapper`` pathway), and ``python_module`` (the ``module`` pathway —
``{kind, module, function}``, no file edit, the framework introspects
the undecorated function by dotted path).

Only the first two are ever CHOSEN by code. ``python_module`` is
reachable by explicit ``entry_point_kind`` override, and never selected
autonomously, because what separates it from direct decoration on the
SAME kwarg'd function is "may we edit this file" (vendor code, a
read-only checkout) — caller judgment, not a repo fact. SKILL.md:98
offers it as the second option for row 2, so ``needs_wrapper_argv``
DISCLOSES the derived ``{module, function}`` target whenever one is
importable from the campaign dir; the gap is named, never silent.

The two wrapper-ONLY interview fields
-------------------------------------

``data_axis_hint`` (#260) and ``solver`` live on ``interview``'s
``_ShellCommandEntry`` and on neither introspectable shape. Both are
accepted as optional inputs and copied VERBATIM onto the composed
``shell_command`` entry block; ``solver`` additionally falls back to the
adapter ``detect-entry-point`` recognized in the source. Supplying either
on the ``decorate`` / ``module`` pathway is a NAMED ``SpecInvalid``
(``_refuse_wrapper_only_fields``), not a silent drop: a wrapper body is a
``subprocess.check_call`` ``classify-axis`` cannot introspect — which is
precisely why the hint is load-bearing there and a caller error anywhere
else.

What this verb deliberately does NOT do: it never calls the
``interview`` primitive (it emits the ready-to-hand ``interview_spec``
fragment instead), never walks the data-axis tree (that is
``classify-axis-auto``'s seat — this verb only carries a hint the caller
already holds), never names an OPERATOR (the fragment's ``produced_by``
is the bare ``{kind: "human"}`` the interview schema requires; the
interview's own composer fills ``.operator`` from git config), and never
materializes the wrapper file (the ``interview`` verb owns that write).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.wrap_entry_point_auto import (
    PetscSolverHint,
    WrapEntryPointAutoInput,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.block_chain import next_block_hint

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = ["wrap_entry_point_auto"]

# The signature-rewriting-decorator predicate, imported from the decoration
# verb so the pathway table routes to the wrapper EXACTLY when
# decorate-entry-point would have refused. Two copies of that predicate would
# drift into a call that always fails. Same package — no cross-package
# private reach.
from hpc_agent.incorporation.decorate_entry_point import (
    _deco_dotted,
    _is_register_run,
    _is_signature_rewriter,
)

# The frozen-config kwarg-name derivation, imported from the wrapper
# materializer for the same reason: this scan and the hasher must never drift
# on what a frozen YAML's tasks-kwarg is called.
from hpc_agent.incorporation.wrap_entry_point import _sha_kwarg_name

# ── The frozen-YAML convention scan (SKILL.md:147-159) ────────────────────
#
# Promoted VERBATIM from the prose's shell probe:
#
#     ls configs/*.yaml configs/*.yml conf/*.yaml 2>/dev/null
#
# The asymmetry is the prose's, faithfully preserved: ``conf/*.yml`` is
# NOT probed. Promoting the table means promoting what it actually said —
# widening the glob here would silently change which files become part of
# an experiment's identity (every one of them lands in ``cmd_sha``), and
# that is a contract change, not a transcription fix. A repo using
# ``conf/*.yml`` passes ``frozen_configs`` explicitly.
_FROZEN_CONFIG_GLOBS: tuple[str, ...] = ("configs/*.yaml", "configs/*.yml", "conf/*.yaml")

# ``argv_kind``s with no decoratable Python function at all: an installed
# console script's ``path`` is a command NAME, and a shell script / binary
# has no Python surface. Both are SKILL row 3 (non-Python entry point).
_NON_PYTHON_ARGV_KINDS: frozenset[str] = frozenset({"shell", "console_script"})

# ``argv_kind``s whose CLI library consumes/rewrites the decorated callable,
# so ``@register_run`` cannot see the real kwargs: SKILL rows 4 + 5.
_SIGNATURE_REWRITING_ARGV_KINDS: frozenset[str] = frozenset({"hydra", "click", "typer"})

# ``argv_kind`` emitted for an entry point the CALLER named that the
# detection scan did not surface (so it carries no classified surface).
# The pathway table still decides it correctly: the substantive rows are
# the AST tests on the resolved function, not the file-level label.
_CALLER_SUPPLIED_ARGV_KIND = "caller_supplied"

# The argparse/parser entry points whose flags live in a parser body this
# verb does not read. They are still ``decorate`` candidates — SKILL row 1
# vs. row 2 is decided by whether the RESOLVED FUNCTION parses argv, not by
# the file-level import.
_ARGV_PARSE_METHODS: frozenset[str] = frozenset({"parse_args", "parse_known_args"})

# A valid Python identifier — the pattern ``_ShellCommandEntry.run_name``
# enforces on the wire, mirrored here so a DERIVED run_name is refused
# before it reaches the interview rather than after.
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# The human-owned intent fields, in the order they are reported. ``goal``
# and ``task_generator`` are literal REQUIRED_CALLER_FIELDS members;
# ``task_count`` is the sweep magnitude the same authorship gate locks.
_INTENT_FIELDS: tuple[str, ...] = ("goal", "task_generator", "task_count")


@dataclass(frozen=True)
class _EntryPoint:
    """The resolved entry point: which file, which function, how classified.

    ``argv_extraction`` / ``argv_params`` / ``detected_solver`` are carried
    from the SAME ``detect-entry-point`` block every other step reads. The
    composite already paid for that scan, so re-deriving (or dropping) any of
    the three would re-open the produce→consume seam this verb exists to close.
    """

    path: str
    argv_kind: str
    rule: str
    function: str | None
    func_node: ast.FunctionDef | ast.AsyncFunctionDef | None
    already_decorated: bool
    argv_extraction: str
    argv_params: list[dict[str, Any]] | None
    detected_solver: str | None


@dataclass(frozen=True)
class _Partition:
    """The fixed-params partition over an entry point's declared params."""

    all_params: tuple[str, ...]
    axis_params: tuple[str, ...]
    defaulted_params: tuple[str, ...]
    uncovered_params: tuple[str, ...]
    accepts_var_keyword: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "all_params": list(self.all_params),
            "axis_params": list(self.axis_params),
            "defaulted_params": list(self.defaulted_params),
            "uncovered_params": list(self.uncovered_params),
            "accepts_var_keyword": self.accepts_var_keyword,
        }


# ── entry-point resolution ───────────────────────────────────────────────


def _candidate_row(candidates: Sequence[dict[str, Any]], path: str) -> dict[str, Any] | None:
    """Return the detection row for *path*, or ``None`` when it wasn't scanned."""
    for row in candidates:
        if row.get("path") == path:
            return row
    return None


def _candidate_surface(
    detected: dict[str, Any], path: str
) -> tuple[str, list[dict[str, Any]] | None, str | None]:
    """The scanned surface for *path*: ``(argv_extraction, argv_params, solver)``.

    Read off the SAME ``detect-entry-point`` block step 1 produced — the whole
    point of running that scan in-process. Three carries, one lookup:

    * ``argv_extraction`` / ``argv_params`` — the mechanical parameter read
      (``ops/argv_extract.py``). Carrying them onto ``needs_wrapper_argv`` is
      what lets the caller compose the argv template WITHOUT a second
      ``detect-entry-point`` call.
    * ``solver`` — the detected checkpoint adapter (``"petsc"``), which the
      wrapper pathway turns into an ``entry_point.solver`` hint.

    A path the scan never surfaced (the caller named it) has no classified
    surface, so it reports ``unsupported`` / ``None`` — the same honest verdict
    the extractor gives a typer file, never an absent field the consumer has to
    read as "unknown". An ``extracted`` verdict with no list is normalized to
    ``unsupported`` for the same reason.
    """
    # Deferred like the ``detect_entry_point`` import below it, and for the
    # same reason: ``incorporation`` is a substrate role that must not import
    # UP into ``ops`` at module level (scripts/lint_subject_imports.py).
    from hpc_agent.ops.argv_extract import EXTRACTION_EXTRACTED, EXTRACTION_UNSUPPORTED

    row = _candidate_row(list(detected.get("candidates") or []), path) or {}
    solver = row.get("solver")
    params = row.get("argv_params")
    if row.get("argv_extraction") != EXTRACTION_EXTRACTED or params is None:
        return EXTRACTION_UNSUPPORTED, None, solver
    return EXTRACTION_EXTRACTED, list(params), solver


def _resolve_entry_file(
    detected: dict[str, Any], *, caller_path: str | None
) -> tuple[str, str, str] | dict[str, Any]:
    """Resolve the entry-point FILE, or return a ``needs_pick`` payload.

    Returns ``(path, argv_kind, rule)`` on a clean resolution. Ladder, in
    order (each rung named in the returned ``rule`` so the pick is
    auditable):

    1. ``caller_entry_point_path`` — the caller named it; detection is a
       lookup for the ``argv_kind`` only.
    2. ``existing_register_run`` — exactly one file on disk already
       carries ``@register_run``; the repo is already onboarded and that
       file IS the entry point (SKILL Step 0 treats decoration as a
       non-greenfield match).
    3. ``sole_candidate`` — exactly one entry-point candidate matched.

    Raises ``SpecInvalid`` on a greenfield repo (nothing to onboard —
    ``build-template`` scaffolds a seed first). Returns a ``needs_pick``
    dict on a tie at rung 2 or 3.
    """
    candidates: list[dict[str, Any]] = list(detected.get("candidates") or [])
    decoration: list[str] = list(detected.get("decoration_found") or [])

    if caller_path is not None:
        row = _candidate_row(candidates, caller_path)
        argv_kind = str(row.get("argv_kind")) if row else _CALLER_SUPPLIED_ARGV_KIND
        return caller_path, argv_kind, "caller_entry_point_path"

    if len(decoration) == 1:
        path = decoration[0]
        row = _candidate_row(candidates, path)
        argv_kind = str(row.get("argv_kind")) if row else _CALLER_SUPPLIED_ARGV_KIND
        return path, argv_kind, "existing_register_run"
    if len(decoration) > 1:
        return _needs_pick(
            reason="entry_point_tie",
            candidates=[
                {
                    "path": path,
                    "argv_kind": (
                        str((_candidate_row(candidates, path) or {}).get("argv_kind"))
                        if _candidate_row(candidates, path)
                        else _CALLER_SUPPLIED_ARGV_KIND
                    ),
                    "function": None,
                }
                for path in decoration
            ],
            resolve_with="entry_point_path",
            ask=(
                f"{len(decoration)} files already carry @register_run "
                f"({', '.join(decoration)}) — which one is THIS experiment's entry "
                "point? Set entry_point_path. Code cannot pick: every one of them "
                "is a valid registered run, and onboarding the wrong one submits "
                "the wrong experiment with no error anywhere."
            ),
        )

    if len(candidates) == 1:
        row = candidates[0]
        return str(row["path"]), str(row.get("argv_kind", "")), "sole_candidate"
    if len(candidates) > 1:
        return _needs_pick(
            reason="entry_point_tie",
            candidates=[
                {
                    "path": str(row["path"]),
                    "argv_kind": str(row.get("argv_kind", "")),
                    "function": None,
                }
                for row in candidates
            ],
            resolve_with="entry_point_path",
            ask=(
                f"{len(candidates)} entry-point candidates matched "
                f"({', '.join(str(r['path']) for r in candidates)}) and none carries "
                "@register_run — which one is the experiment's entry point? Set "
                "entry_point_path. Code does not tie-break on probe order: the "
                "candidates are ranked by NAME convention, which carries no "
                "information about which file the scientist means to run."
            ),
        )

    raise errors.SpecInvalid(
        "greenfield_repo: no entry-point candidate and no @register_run "
        "anywhere under the experiment dir — there is nothing to onboard",
        remediation=(
            "Scaffold a seed entry point first: "
            "`hpc-agent build-template --repo-dir <dir> --shape script` "
            "(or `--shape notebook`), then re-run wrap-entry-point-auto. "
            "This verb onboards an EXISTING entry point; it never authors one, "
            "because the seed's shape is the scientist's choice."
        ),
    )


def _public_module_defs(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Module-level, non-underscore-prefixed ``def``s, in source order."""
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("_")
    ]


def _decorated_defs(
    defs: Iterable[ast.FunctionDef | ast.AsyncFunctionDef],
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The subset of *defs* already carrying ``@register_run``."""
    return [
        node for node in defs if any(_is_register_run(_deco_dotted(d)) for d in node.decorator_list)
    ]


def _resolve_entry_function(
    tree: ast.Module, *, path: str, caller_run_name: str | None
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef | None, str, bool] | dict[str, Any]:
    """Resolve the entry FUNCTION in a parsed Python entry point.

    Returns ``(func_node, rule, already_decorated)``; ``func_node`` is
    ``None`` only for the ``no_decoratable_function`` rule (a script that
    is all top-level code — the wrapper pathway's job). Returns a
    ``needs_pick`` dict on an entry-function tie.

    Ladder, in order:

    1. ``caller_run_name`` — the caller named it (must be a module-level
       ``def``; anything else is ``SpecInvalid``, matching
       ``decorate-entry-point``'s own refusal).
    2. ``existing_register_run_function`` — a def already carrying
       ``@register_run``. Re-running is then a pure read.
    3. ``conventional_main`` — a def named ``main``. This is substrate
       convention (how Python entry points are spelled), not experiment
       semantics, so code may apply it.
    4. ``sole_public_def`` — exactly one public module-level def.
    5. ``no_decoratable_function`` — none at all.

    Anything else is a tie the caller breaks with ``run_name``.
    """
    defs = _public_module_defs(tree)
    decorated = _decorated_defs(defs)

    if caller_run_name is not None:
        for node in tree.body:
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == caller_run_name
            ):
                return node, "caller_run_name", node in decorated
        raise errors.SpecInvalid(
            f"run_name {caller_run_name!r} is not a module-level def in {path!r}",
            remediation=(
                "Name a module-level function that exists in the entry point, or "
                "drop run_name and let the entry-function ladder resolve it. "
                "@register_run decorates a module-level def; it cannot reach a "
                "nested or class-bound function."
            ),
        )

    if len(decorated) == 1:
        return decorated[0], "existing_register_run_function", True
    if len(decorated) > 1:
        return _needs_pick(
            reason="entry_function_tie",
            candidates=[
                {"path": path, "argv_kind": _CALLER_SUPPLIED_ARGV_KIND, "function": node.name}
                for node in decorated
            ],
            resolve_with="run_name",
            ask=(
                f"{path} carries @register_run on {len(decorated)} functions "
                f"({', '.join(n.name for n in decorated)}) — which one is THIS "
                "experiment's run? Set run_name. Code cannot pick: both are "
                "registered runs and the wrong one submits the wrong experiment."
            ),
            entry_point_path=path,
        )

    for node in defs:
        if node.name == "main":
            return node, "conventional_main", False

    if len(defs) == 1:
        return defs[0], "sole_public_def", False
    if len(defs) > 1:
        return _needs_pick(
            reason="entry_function_tie",
            candidates=[
                {"path": path, "argv_kind": _CALLER_SUPPLIED_ARGV_KIND, "function": node.name}
                for node in defs
            ],
            resolve_with="run_name",
            ask=(
                f"{path} declares {len(defs)} public module-level functions "
                f"({', '.join(n.name for n in defs)}), none decorated and none "
                "named 'main' — which one is the experiment's entry function? Set "
                "run_name. Code cannot pick: which of several equally-public "
                "functions represents one task is experiment semantics."
            ),
            entry_point_path=path,
        )

    return None, "no_decoratable_function", False


# ── the pathway decision table (SKILL.md:93-104) ─────────────────────────


def _parses_argv(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when *func*'s body reads ``sys.argv`` or drives an argv parser.

    SKILL row 2: "a Python function whose body parses ``sys.argv`` (an
    argparse ``main()``)" routes to the wrapper — ``decorate-entry-point``
    decorates a function whose params are ALREADY real kwargs and
    explicitly does not refactor a ``main()``. Detected three ways:
    ``sys.argv`` / a bare ``argv`` name, an ``ArgumentParser(...)``
    construction, or a ``.parse_args()`` / ``.parse_known_args()`` call.
    """
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and node.attr == "argv":
            return True
        if isinstance(node, ast.Name) and node.id == "argv":
            return True
        if isinstance(node, ast.Call):
            dotted = _deco_dotted(node)
            if dotted.rsplit(".", 1)[-1] in _ARGV_PARSE_METHODS:
                return True
            if dotted.rsplit(".", 1)[-1] == "ArgumentParser":
                return True
    return False


def _refuse_unsafe_forced_register_run(entry: _EntryPoint) -> None:
    """Refuse ``entry_point_kind='register_run'`` when decoration cannot work.

    SKILL.md:104's invariant: over-refusal into the wrapper is safe, but
    decorating through a signature-rewriting decorator "silently produces
    an executor the framework can't introspect". A caller override must not
    be able to buy that outcome — and must not be silently rerouted either
    (that would break override-first). So the contradiction is named, with
    both remedies, and the caller decides.
    """
    if entry.argv_kind in _NON_PYTHON_ARGV_KINDS or not entry.path.endswith(".py"):
        raise errors.SpecInvalid(
            f"entry_point_kind='register_run' cannot apply to {entry.path!r} "
            f"(argv_kind={entry.argv_kind}): there is no Python function to "
            "decorate",
            remediation=(
                "Drop entry_point_kind and let the pathway table route this to "
                "the wrapper (entry_point_kind='shell_command'), which is the "
                "only shape a non-Python entry point has."
            ),
        )
    if entry.func_node is None:
        raise errors.SpecInvalid(
            f"entry_point_kind='register_run' cannot apply to {entry.path!r}: no "
            "module-level def resolved, so there is nothing to decorate",
            remediation=(
                "Drop entry_point_kind (the table routes this to the wrapper), or "
                "name the function with run_name if the file does declare one."
            ),
        )
    rewriter = next(
        (
            dotted
            for dotted in (_deco_dotted(d) for d in entry.func_node.decorator_list)
            if _is_signature_rewriter(dotted)
        ),
        None,
    )
    if rewriter is not None:
        raise errors.SpecInvalid(
            f"entry_point_kind='register_run' is unsafe on {entry.function!r} in "
            f"{entry.path!r}: it carries @{rewriter}, which rewrites the "
            "signature, so @register_run cannot see the real kwargs and the "
            "materialized executor would be un-introspectable",
            remediation=(
                "Either drop entry_point_kind and let the table route this to the "
                "wrapper (the always-safe fallback for a signature-rewriting "
                "decorator), or state that explicitly with "
                "entry_point_kind='shell_command'. This verb refuses rather than "
                "rerouting, because silently overriding your override is worse "
                "than asking."
            ),
        )


def _refuse_wrapper_only_fields(spec: WrapEntryPointAutoInput, pathway: str) -> None:
    """Refuse ``data_axis_hint`` / ``solver`` outside the wrapper pathway.

    Both fields exist on ``interview``'s ``_ShellCommandEntry`` and on NEITHER
    of the two introspectable shapes (``_RegisterRunEntry`` /
    ``_PythonModuleEntry`` declare them nowhere and are ``extra="forbid"``), so
    a hint supplied on the ``decorate`` / ``module`` pathway could only be
    silently dropped on the way to the fragment. That is the exact class this
    module already refuses for ``fixed_params`` on ``python_module``: a dropped
    caller value changes nothing visibly and everything semantically.

    ``data_axis_hint`` is the load-bearing case (#260). A wrapper body is a
    ``subprocess.check_call`` ``classify-axis`` cannot introspect, so on the
    wrapper pathway the hint is the ONLY way the classification reaches
    ``axes.yaml`` without an interactive tree. On an introspectable pathway
    ``classify-axis`` reads the real function — so the hint is not a missing
    input there, it is a caller error, and it is NAMED as one.
    """
    if pathway == "wrapper":
        return
    kind = "register_run" if pathway == "decorate" else "python_module"
    if spec.data_axis_hint is not None:
        raise errors.SpecInvalid(
            f"data_axis_hint is not representable on the {pathway!r} pathway "
            f"(entry_point.kind={kind!r}): the interview accepts it only on a "
            "shell_command entry point (#260)",
            remediation=(
                "The hint exists because a wrapper's subprocess body is "
                "uninspectable — classify-axis cannot read a "
                "subprocess.check_call, so the experimenter declares the axis. "
                f"A {kind} entry point IS introspectable: classify-axis reads "
                "the real function, so drop data_axis_hint and let it classify "
                "(or run classify-axis-auto, whose seat that is). If the entry "
                "point genuinely must be shelled out to, say so with "
                "entry_point_kind='shell_command' and the hint becomes valid."
            ),
        )
    if spec.solver is not None:
        raise errors.SpecInvalid(
            f"solver is not representable on the {pathway!r} pathway "
            f"(entry_point.kind={kind!r}); the wire shape carries no solver field",
            remediation=(
                "The solver hint instruments a MATERIALIZED wrapper (it injects "
                "the library's checkpoint hooks around the argv), and only the "
                "shell_command entry point has one. Drop solver, or route "
                "through the wrapper with entry_point_kind='shell_command' if "
                "the solve loop needs checkpoint instrumentation."
            ),
        )


def _decide_pathway(
    *,
    entry: _EntryPoint,
    caller_entry_point_kind: str | None,
) -> tuple[str, str]:
    """The SKILL.md:93-104 decision table, promoted to code.

    Returns ``(pathway, rule)``. ``pathway`` is ``"decorate"`` (Step 3a —
    the DEFAULT), ``"wrapper"`` (Step 3b — the fallback), or ``"module"``
    (the ``python_module`` dotted-path target, SKILL.md:98's second option
    for row 2 — reachable ONLY by explicit caller override). Rows are
    evaluated in this order:

    =====================================  ========  ==============================
    Rule id                                Pathway   SKILL row
    =====================================  ========  ==============================
    ``caller_forced_shell_command``         wrapper   row 6 (explicit caller choice)
    ``caller_forced_python_module``         module    row 2's ``/ python_module``
    ``caller_forced_register_run``          decorate  (the same override, row 1's
                                                     kind) — REFUSES when the
                                                     function carries a
                                                     signature-rewriting
                                                     decorator or is not a
                                                     decoratable Python def
    ``non_python_entry_point``              wrapper   row 3 (shell script / binary)
    ``signature_rewriting_decorator``       wrapper   rows 4 + 5 (hydra/click/typer)
    ``no_decoratable_function``             wrapper   (all top-level code)
    ``body_parses_argv``                    wrapper   row 2 (an argparse ``main()``)
    ``kwargs_signature``                    decorate  row 1 (the default)
    =====================================  ========  ==============================

    The caller override rows are evaluated FIRST rather than last (the
    prose lists the override last). That is deliberate: an override that
    loses to a detected row is not an override. Pinned by
    ``test_override_first_beats_a_detected_row`` — relocating these rows
    below the detected ones turns that test red.

    Over-refusal into the wrapper is SAFE by design (the wrapper always
    works); decorating through a signature-rewriting decorator silently
    produces an executor the framework cannot introspect. So each
    wrapper row is checked before the decorate default.

    ``caller_forced_register_run`` is the one override that cannot simply
    win, because the outcome it asks for is the unsafe one the ordering
    invariant exists to prevent: ``@register_run`` cannot see through
    ``@hydra.main`` / a consuming ``@click.command``. Silently rerouting it
    to the wrapper would violate override-first; letting it through would
    ship an un-introspectable executor. So it is a NAMED REFUSAL
    (``SpecInvalid``) stating both remedies — the caller decides, which is
    what an override is for.
    """
    if caller_entry_point_kind == "shell_command":
        return "wrapper", "caller_forced_shell_command"
    if caller_entry_point_kind == "python_module":
        return "module", "caller_forced_python_module"
    if caller_entry_point_kind == "register_run":
        _refuse_unsafe_forced_register_run(entry)
        return "decorate", "caller_forced_register_run"
    if entry.argv_kind in _NON_PYTHON_ARGV_KINDS or not entry.path.endswith(".py"):
        return "wrapper", "non_python_entry_point"
    if entry.argv_kind in _SIGNATURE_REWRITING_ARGV_KINDS:
        return "wrapper", "signature_rewriting_decorator"
    if entry.func_node is None:
        return "wrapper", "no_decoratable_function"
    if any(_is_signature_rewriter(_deco_dotted(d)) for d in entry.func_node.decorator_list):
        return "wrapper", "signature_rewriting_decorator"
    if _parses_argv(entry.func_node):
        return "wrapper", "body_parses_argv"
    return "decorate", "kwargs_signature"


# ── the frozen-YAML convention scan (SKILL.md:147-159) ───────────────────


def _scan_frozen_configs(root: Path) -> list[str]:
    """Reproduce ``ls configs/*.yaml configs/*.yml conf/*.yaml``.

    Every match becomes a ``frozen_configs`` entry: the convention is
    *one YAML = one frozen experiment*, and the framework hashes each
    file's bytes into a ``<stem>_sha`` task kwarg so ``cmd_sha`` covers
    the YAML's content. Paths are relative to *root*, POSIX-separated,
    sorted within each glob and de-duplicated across globs (glob order
    preserved, matching the shell probe's argument order).
    """
    out: list[str] = []
    for pattern in _FROZEN_CONFIG_GLOBS:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel not in out:
                out.append(rel)
    return out


def _frozen_sha_params(frozen_configs: Sequence[str]) -> list[str]:
    """The ``<stem>_sha`` kwarg names the framework threads per frozen YAML."""
    out: list[str] = []
    for rel in frozen_configs:
        name = _sha_kwarg_name(rel)
        if name not in out:
            out.append(name)
    return out


# ── the fixed-params partition (SKILL.md:180-193) ─────────────────────────


def _axis_params(task_generator: Any) -> list[str]:
    """The param names *task_generator* produces per task, first-seen order.

    Reads the interview wire's own discriminated recipe shapes, so this
    derivation and the downstream ``tasks.py`` materializer agree by
    construction:

    * ``enumerated`` — the union of every item dict's keys;
    * ``cartesian_product`` — the declared axis names;
    * ``items_x_seeds`` — every item key plus ``seed``;
    * ``numeric_linspace`` / ``numeric_logspace`` — the swept ``param``;
    * ``chunked_series`` — the chunk bounds plus any ``extra_axes``.
    """
    kind = task_generator.kind
    params = task_generator.params
    out: list[str] = []

    def _add(name: str) -> None:
        if name not in out:
            out.append(name)

    if kind == "enumerated":
        for item in params.items:
            for key in item:
                _add(str(key))
    elif kind == "cartesian_product":
        for key in params.axes:
            _add(str(key))
    elif kind == "items_x_seeds":
        for item in params.items:
            for key in item:
                _add(str(key))
        _add("seed")
    elif kind in ("numeric_linspace", "numeric_logspace"):
        _add(params.param)
    elif kind == "chunked_series":
        for key in ("chunk_start", "chunk_end", "halo"):
            _add(key)
        for key in params.extra_axes or {}:
            _add(str(key))
    return out


def _declared_params(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[list[str], set[str], bool]:
    """Extract ``(names, defaulted, accepts_var_keyword)`` from *func*'s signature.

    ``*args`` is ignored (it is not a kwarg the framework can supply);
    ``**kwargs`` sets ``accepts_var_keyword``, which makes "uncovered"
    impossible — an unmatched kwarg is absorbed rather than a TypeError.
    """
    args = func.args
    positional = [*args.posonlyargs, *args.args]
    names = [a.arg for a in positional]
    defaulted: set[str] = set()
    if args.defaults:
        for arg in positional[len(positional) - len(args.defaults) :]:
            defaulted.add(arg.arg)
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        names.append(arg.arg)
        if default is not None:
            defaulted.add(arg.arg)
    return names, defaulted, args.kwarg is not None


def _partition_params(
    *,
    declared: Sequence[str],
    defaulted: set[str],
    accepts_var_keyword: bool,
    axis_params: Sequence[str],
    frozen_sha_params: Sequence[str],
    fixed_params: dict[str, Any],
) -> _Partition:
    """The SKILL.md:180-193 partition, promoted to code.

    Every declared param lands in exactly one class:

    * **axis** — the ``task_generator`` produces it per task (or the
      framework threads it as a ``<stem>_sha``). Deliberately left OUT of
      ``fixed_params``: a per-task value must not be pinned to a constant.
    * **defaulted** — the entry point's own signature supplies a value.
      Safe to omit; a caller MAY still pin one for reproducibility.
    * **uncovered** — required, not an axis, not in the caller's
      ``fixed_params``. Every task would fail on it (#195), so it is an
      escalation. This function NEVER manufactures a value: a signature
      default is what makes a param ``defaulted``, and there is no other
      source code is entitled to read.

    ``**kwargs`` on the entry point empties ``uncovered`` — an unmatched
    kwarg is absorbed, so no task can fail on one.
    """
    covered = {*axis_params, *frozen_sha_params}
    axis = [name for name in declared if name in covered]
    defaults = [name for name in declared if name not in covered and name in defaulted]
    uncovered = [
        name
        for name in declared
        if name not in covered and name not in defaulted and name not in fixed_params
    ]
    if accepts_var_keyword:
        uncovered = []
    return _Partition(
        all_params=tuple(declared),
        axis_params=tuple(axis),
        defaulted_params=tuple(defaults),
        uncovered_params=tuple(uncovered),
        accepts_var_keyword=accepts_var_keyword,
    )


# ── wrapper-pathway argv head (SKILL.md:134-141) ──────────────────────────


def _derive_run_name(path: str) -> str:
    """Derive a wrapper ``run_name`` from the entry-point path's stem.

    A package ``__main__.py`` uses its PACKAGE name (``src/mypkg/__main__.py``
    → ``mypkg``); everything else uses the file stem with non-identifier
    characters folded to ``_`` (``run.sh`` → ``run``, ``my-tool`` →
    ``my_tool``). Raises ``SpecInvalid`` when no identifier falls out —
    naming the run is then a caller decision, not a code guess.
    """
    p = Path(path)
    stem = p.parent.name if p.stem == "__main__" else p.stem
    candidate = re.sub(r"[^A-Za-z0-9_]", "_", stem)
    if not _IDENTIFIER_RE.match(candidate):
        raise errors.SpecInvalid(
            f"cannot derive a valid Python identifier for run_name from entry "
            f"point {path!r} (derived {candidate!r})",
            remediation=(
                "Pass run_name explicitly. It names the materialized wrapper "
                "(.hpc/wrappers/<run_name>.py) and its @register_run function, so "
                "it must be a valid Python identifier — and choosing the run's "
                "name is the caller's call, not a mangling this verb invents."
            ),
        )
    return candidate


def _dotted_module(root: Path, path: str) -> str | None:
    """The dotted module name for *path*, or ``None`` when it isn't importable.

    The ``python_module`` entry point is imported with the CAMPAIGN DIR on
    ``sys.path`` (``interview._validate_python_module_entry`` prepends it,
    matching the cluster's ``$REPO_DIR`` on ``PYTHONPATH``). So a dotted
    name only resolves when the file is reachable as a package chain FROM
    THE REPO ROOT: either a top-level module (``train.py`` → ``train``) or
    inside directories that each carry an ``__init__.py``
    (``pkg/cli/train.py`` → ``pkg.cli.train``).

    Returns ``None`` — so no ``python_module`` target is offered — for a
    ``src``-layout package (``src/mypkg/train.py`` is importable as
    ``mypkg.train`` only once installed or with ``src/`` on the path, which
    the campaign dir is not), for a non-``.py`` path, and for any segment
    that is not a Python identifier. Offering an unimportable dotted name
    would just move the failure to the interview's own validator.
    """
    p = Path(path)
    if p.suffix != ".py" or p.name == "__init__.py":
        return None
    parts = [*p.parent.parts, p.stem]
    if not all(_IDENTIFIER_RE.match(part) for part in parts):
        return None
    # Every intermediate directory must be a real package from the root.
    current = root
    for part in p.parent.parts:
        current = current / part
        if not (current / "__init__.py").is_file():
            return None
    return ".".join(parts)


def _python_module_alternative(root: Path, entry: _EntryPoint) -> dict[str, str] | None:
    """The ``python_module`` target SKILL.md:98 offers for the same row, or None.

    A DISCLOSURE only. Code never selects ``python_module`` on its own: what
    separates it from direct decoration is "may we edit this file" (vendor
    code, a read-only checkout), which is caller judgment and not a repo
    fact. Naming the derived target here is the difference between an
    undisclosed gap and an explicit ask.
    """
    if entry.function is None:
        return None
    module = _dotted_module(root, entry.path)
    if module is None:
        return None
    return {"module": module, "function": entry.function}


def _resolve_module_target(root: Path, entry: _EntryPoint, path: str) -> dict[str, str]:
    """Resolve the ``python_module`` dotted target, or refuse by name.

    The forced-``python_module`` counterpart of
    :func:`_python_module_alternative`: same derivation, but an
    unimportable result is a named ``SpecInvalid`` rather than a silent
    absence, because the caller explicitly asked for this pathway.
    """
    target = _python_module_alternative(root, entry)
    if target is not None:
        return target
    raise errors.SpecInvalid(
        f"entry_point_kind='python_module' cannot apply to {path!r}: no dotted "
        "module name is importable with the campaign dir on sys.path (a "
        "src-layout package, a directory without __init__.py, a non-Python "
        "path, or no resolved entry function)",
        remediation=(
            "A python_module entry point must import as "
            "`<dotted.module>:<function>` from the repo root — the same path the "
            "cluster puts on PYTHONPATH. Add the missing __init__.py, move the "
            "module under an importable package, or drop entry_point_kind and "
            "let the table route this to direct decoration / the wrapper."
        ),
    )


def _argv_head(path: str, argv_kind: str) -> list[str]:
    """The leading argv elements code CAN compose, per SKILL.md:136-140.

    * installed console script → ``["mytool"]`` (``path`` IS the command);
    * package ``__main__.py`` → ``["python3", "-m", "mypkg"]``;
    * any other ``.py`` file → ``["python3", "train.py"]``;
    * shell script / binary → ``["./run.sh"]``.

    A leading ``src`` path segment is dropped from the ``-m`` target (a
    ``src``-layout package is importable as ``mypkg``, not ``src.mypkg``).
    """
    if argv_kind == "console_script":
        return [path]
    p = Path(path)
    if p.suffix != ".py":
        return [f"./{path}"]
    if p.name == "__main__.py":
        parts = [part for part in p.parent.parts if part != "src"]
        if parts:
            return ["python3", "-m", ".".join(parts)]
    return ["python3", path]


# ── escalation builders ──────────────────────────────────────────────────


def _needs_pick(
    *,
    reason: str,
    candidates: list[dict[str, Any]],
    resolve_with: str,
    ask: str,
    entry_point_path: str | None = None,
) -> dict[str, Any]:
    """Build the ``needs_pick`` terminal shape. Nothing has been written."""
    out: dict[str, Any] = {
        "needs_pick": True,
        "reason": reason,
        "candidates": candidates,
        "resolve_with": resolve_with,
        "ask": ask,
    }
    if entry_point_path is not None:
        out["entry_point_path"] = entry_point_path
    return out


def _missing_intent_fields(spec: WrapEntryPointAutoInput) -> list[str]:
    """The absent human-owned intent fields, in :data:`_INTENT_FIELDS` order."""
    return [name for name in _INTENT_FIELDS if getattr(spec, name) is None]


def _intent_ask(missing: Sequence[str]) -> str:
    """The precise, named ask for each missing intent field."""
    why = {
        "goal": (
            "goal — the one-line intent, in the scientist's own words. No repo "
            "scan produces it and no default is safe: it is what the run's "
            "evidence is later read against"
        ),
        "task_generator": (
            "task_generator — the sweep recipe (shape + params). The entry point "
            "handles ONE task; which N tasks to fan out is not in the code. An "
            "agent once invented one under a 'safe defaults' rationale and ran "
            "the wrong experiment; the field partition now forbids a default here"
        ),
        "task_count": (
            "task_count — the expected number of tasks, which the interview "
            "cross-checks against the generated tasks.py to catch off-by-one. A "
            "count code derived from its own recipe would check nothing"
        ),
    }
    named = [why.get(field, field) for field in missing]
    return (
        "Caller-owned intent is missing: "
        + "; ".join(named)
        + ". Ask the scientist for these as free text (never a pre-filled option "
        "they click — the downstream authorship gate verifies each value against "
        "their own words) and re-run with them set."
    )


def _uncovered_ask(uncovered: Sequence[str], run_name: str, pathway: str) -> str:
    """The precise, named ask for each uncovered required entry-point param.

    The remedy is pathway-dependent: ``register_run`` / ``shell_command``
    both carry ``fixed_params`` on the wire, ``python_module`` does not (its
    shape is only ``{kind, module, function}``), so naming ``fixed_params``
    there would be an unsatisfiable ask.
    """
    if pathway == "module":
        remedy = (
            "A python_module entry point carries no fixed_params on the wire "
            "(its shape is {kind, module, function}), so give each param a "
            "default in the function's own signature — python_module "
            "introspects the real signature — or switch to "
            "entry_point_kind='register_run' / 'shell_command', which do carry "
            "fixed_params."
        )
    else:
        remedy = "Set each as a constant in fixed_params."
    return (
        f"{run_name} requires {len(uncovered)} param(s) the task_generator does "
        f"not vary and the signature does not default: {', '.join(uncovered)}. "
        "Every task would fail on them (validate-executor-signatures refuses "
        f"uncovered_required_param at submit). {remedy} Code will not invent "
        "one: a param with no default and no caller value is a real ambiguity, "
        "and a fabricated constant silently changes what the experiment "
        "computes."
    )


def _needs_intent(
    *,
    missing: Sequence[str],
    ask: str,
    pathway: str,
    entry: _EntryPoint,
    run_name: str,
    partition: _Partition | None,
) -> dict[str, Any]:
    """Build the ``needs_intent`` terminal shape. Nothing has been written."""
    return {
        "needs_intent": True,
        "missing_fields": list(missing),
        # Every field this verb escalates here is caller-owned by contract,
        # so never_invented equals missing_fields exactly. It is a SEPARATE
        # field on purpose: a future "just default it" change has to delete a
        # named pin rather than quietly widen a code path.
        "never_invented": list(missing),
        "ask": ask,
        "pathway": pathway,
        "entry_point_path": entry.path,
        "run_name": run_name,
        "argv_kind": entry.argv_kind,
        "partition": partition.as_dict() if partition is not None else None,
    }


def _needs_wrapper_argv(
    *,
    entry: _EntryPoint,
    pathway_rule: str,
    run_name: str,
    missing: Sequence[str],
    missing_intent: Sequence[str],
    python_module_alternative: dict[str, str] | None,
) -> dict[str, Any]:
    """Build the ``needs_wrapper_argv`` terminal shape. Nothing has been written."""
    why = {
        "shell": "a shell script / binary has no Python signature to read",
        "console_script": "an installed console script's target is opaque to a file scan",
        "hydra": "@hydra.main rewrote the signature, so the real flags live in a config tree",
        "click": "the click decorator consumed the callable; the flags are decorator arguments",
        "typer": "the typer decorator consumed the callable; the flags are annotations on it",
    }.get(
        entry.argv_kind,
        "the flags live in a parser body this verb deliberately does not interpret",
    )
    head = _argv_head(entry.path, entry.argv_kind)
    ask = (
        f"The wrapper pathway needs {', '.join(missing)} for {entry.path} "
        f"(argv_kind={entry.argv_kind}): {why}. The leading argv is already "
        f"composed — {head} — so supply the flag template with one "
        "{placeholder} per swept kwarg plus the matching typed signature "
        "(str/int/float/bool). A guessed flag name fails every task, and the "
        "canary would only catch it after the submit round-trip."
    )
    if python_module_alternative is not None:
        target = f"{python_module_alternative['module']}:{python_module_alternative['function']}"
        ask += (
            " ALTERNATIVE (SKILL.md:98 offers it for this row): the entry point is "
            f"importable as {target}, so entry_point_kind='python_module' targets "
            "the function by dotted path with NO argv template and NO edit to the "
            "file. Code does not choose it for you — whether the file may be "
            "edited is caller judgment, not a repo fact."
        )
    # ``_candidate_surface`` already normalized the pair, so a non-None list IS
    # the ``extracted`` verdict — no second read of the verdict string (and no
    # module-level reach up into ``ops`` for its constant) is needed here.
    if entry.argv_params is not None:
        named = ", ".join(str(param.get("dest")) for param in entry.argv_params)
        ask += (
            f" The CLI parameters were ALREADY read mechanically off the AST "
            f"({len(entry.argv_params)}: {named or 'none declared'}) and are "
            "carried on argv_params below — compose the template from those "
            "rather than re-running detect-entry-point or re-reading the file. "
            "A param carrying an 'unextracted' marker still needs a source read "
            "for the argument it names."
        )
    if missing_intent:
        ask += (
            " Also still missing (gather in the same exchange): " + ", ".join(missing_intent) + "."
        )
    return {
        "needs_wrapper_argv": True,
        "argv_kind": entry.argv_kind,
        "pathway_rule": pathway_rule,
        "entry_point_path": entry.path,
        "run_name": run_name,
        "argv_head": head,
        # The extraction the in-process detect ALREADY produced. Dropping it
        # here is what forced the caller into a second detect-entry-point call.
        "argv_extraction": entry.argv_extraction,
        "argv_params": entry.argv_params,
        "missing_fields": list(missing),
        "missing_intent_fields": list(missing_intent),
        "python_module_alternative": python_module_alternative,
        "ask": ask,
    }


# ── the onboard chain's block surface (P2.c) ─────────────────────────────


def _block_surface(
    *,
    stage_reached: str,
    needs_decision: bool,
    brief: dict[str, Any],
    next_block: dict[str, Any] | None,
) -> dict[str, Any]:
    """The four block-surface keys, built in ONE place.

    Every caller below passes ``stage_reached`` / ``needs_decision`` as EXPLICIT
    literal kwargs — never a ``**kwargs`` splat and never a computed name — because
    the bare-``y`` boundary census (``tests/contracts/test_bare_y_coverage.py``)
    reads those two keywords off the call site by AST. A splat here would make the
    census silently blind to this block's parks, which is the exact
    "a census that under-counts stops guarding" failure it exists to prevent.
    """
    return {
        "stage_reached": stage_reached,
        "needs_decision": needs_decision,
        "brief": brief,
        "next_block": next_block,
    }


def _escalation_brief(shape: dict[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """Project *shape*'s own escalation fields into the park's ONE composed ask.

    The park brief is a PROJECTION, never a re-derivation: every value here was
    already computed by the composite (the tie candidates, the missing field list,
    the mechanically extracted argv params, the composed ``ask`` sentence). A park
    that re-asked for something the block already holds is precisely the
    contract-taught-by-refusal defect this chain closes.
    """
    return {key: shape[key] for key in keys if shape.get(key) is not None}


#: Per-escalation, the fields of the terminal shape that make up its ask. Listed
#: rather than "everything except the discriminator" so a new field has to be
#: classified into (or deliberately out of) the human-facing ask.
_PICK_BRIEF_KEYS = ("reason", "candidates", "resolve_with", "ask", "entry_point_path")
_INTENT_BRIEF_KEYS = (
    "missing_fields",
    "never_invented",
    "ask",
    "pathway",
    "entry_point_path",
    "run_name",
    "partition",
)
_ARGV_BRIEF_KEYS = (
    "argv_kind",
    "entry_point_path",
    "run_name",
    "argv_head",
    "argv_extraction",
    "argv_params",
    "missing_fields",
    "missing_intent_fields",
    "python_module_alternative",
    "ask",
)


def _attach_block_surface(
    shape: dict[str, Any], *, spec: WrapEntryPointAutoInput | None
) -> dict[str, Any]:
    """Stamp the block surface onto whichever terminal shape the composite returned.

    ONE seat, four (really five) outcomes:

    * ``onboarded`` → no decision, and the successor is the ``interview`` verb
      whose COMPLETE input spec is composed from the fragment this call built plus
      the opaque ``audited_source`` carry (:func:`block_chain.compose_successor_spec`).
    * ``needs_pick`` / ``needs_intent`` → HUMAN parks. Both are judgments the recon
      named: a wrong entry-point pick is non-recoverable without the user noticing,
      and the intent fields are ``REQUIRED_CALLER_FIELDS``.
    * ``needs_wrapper_argv`` → split by the extraction verdict the composite already
      carries. ``extracted`` (the params were read mechanically off the AST) is the
      AGENT park: what is owed is TRANSCRIPTION of a code-produced list into an
      argv template, which is authorship, not authorization. Anything else is
      ``needs_wrapper_argv_unsupported``, a HUMAN park — with nothing extracted, the
      flag names are the caller's knowledge of their own tool and a guess fails
      every task.

    The split is computed from ``argv_params`` (``_candidate_surface`` normalizes a
    non-``extracted`` verdict to ``None``), so it reads the SAME evidence the ask
    sentence quotes — the two can never disagree about whether params were read.
    """
    audited_source = getattr(spec, "audited_source", None) if spec is not None else None
    if shape.get("onboarded"):
        out = dict(shape)
        if isinstance(audited_source, dict) and audited_source:
            out["audited_source"] = dict(audited_source)
        return {
            **out,
            **_block_surface(
                stage_reached="onboarded",
                needs_decision=False,
                brief={},
                next_block=next_block_hint(
                    "wrap-entry-point-auto",
                    "onboarded",
                    why=(
                        "the entry point, pathway and params are resolved — persist "
                        "the intent and materialize tasks.py."
                    ),
                    interview_spec=out.get("interview_spec"),
                    audited_source=out.get("audited_source"),
                ),
            ),
        }
    if shape.get("needs_pick"):
        return {
            **shape,
            **_block_surface(
                stage_reached="needs_pick",
                needs_decision=True,
                brief=_escalation_brief(shape, _PICK_BRIEF_KEYS),
                next_block=None,
            ),
        }
    if shape.get("needs_intent"):
        return {
            **shape,
            **_block_surface(
                stage_reached="needs_intent",
                needs_decision=True,
                brief=_escalation_brief(shape, _INTENT_BRIEF_KEYS),
                next_block=None,
            ),
        }
    brief = _escalation_brief(shape, _ARGV_BRIEF_KEYS)
    if shape.get("argv_params"):
        return {
            **shape,
            **_block_surface(
                stage_reached="needs_wrapper_argv",
                needs_decision=True,
                brief=brief,
                next_block=None,
            ),
        }
    return {
        **shape,
        **_block_surface(
            stage_reached="needs_wrapper_argv_unsupported",
            needs_decision=True,
            brief=brief,
            next_block=None,
        ),
    }


# ── the composite ────────────────────────────────────────────────────────


@primitive(
    name="wrap-entry-point-auto",
    verb="workflow",
    composes=["detect-entry-point", "decorate-entry-point"],
    side_effects=[
        SideEffect(
            "filesystem",
            "<entry point> (in-place: import + @register_run) — direct-decoration pathway only",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="experiment_dir",
    cli=CliShape(
        help=(
            "Composite head of hpc-wrap-entry-point: detect-entry-point -> the "
            "pathway decision table -> decorate-entry-point -> the frozen-YAML "
            "convention scan -> the fixed-params partition, in ONE call. "
            "Resolves the entry-point file and function off named ladders, then "
            "returns one of four discriminated shapes: {onboarded} with the "
            "composed interview_spec fragment; {needs_pick} on an entry-point / "
            "entry-function tie; {needs_intent} when goal / task_generator / "
            "task_count or a specific uncovered required param is missing (code "
            "never invents these); {needs_wrapper_argv} when the wrapper pathway "
            "needs an argv template this verb will not guess. Every escalation "
            "names the exact field and why it is caller-owned, and leaves the "
            "repo byte-identical — decoration is the last step."
        ),
        spec_arg=True,
        spec_required=False,
        schema_ref=SchemaRef(input="wrap_entry_point_auto"),
        spec_model=WrapEntryPointAutoInput,
        experiment_dir_arg=True,
    ),
    agent_facing=True,
)
def wrap_entry_point_auto(
    experiment_dir: Path,
    *,
    spec: WrapEntryPointAutoInput | None = None,
) -> dict[str, Any]:
    """Run detect → pathway → partition → decorate, and stamp the block surface.

    The ``onboard`` chain's middle block (P2.c). The composite body is unchanged
    (:func:`_wrap_entry_point_auto_impl`); this seat adds the ONE block surface
    every shape carries — ``stage_reached`` / ``needs_decision`` / the composed
    park ``brief`` / ``next_block`` — so the driver can sequence it. Keeping the
    stamp in ONE place (rather than in each of the four terminal builders) is what
    makes "which actor answers this shape" a single auditable table.
    """
    return _attach_block_surface(
        _wrap_entry_point_auto_impl(experiment_dir, spec=spec),
        spec=spec,
    )


def _wrap_entry_point_auto_impl(
    experiment_dir: Path,
    *,
    spec: WrapEntryPointAutoInput | None = None,
) -> dict[str, Any]:
    """Run detect → pathway → partition → decorate, in one call.

    *experiment_dir* is the framework-context kwarg (the repo root every
    detected path is relative to); *spec* carries the optional overrides
    and the human-owned intent (a bare call with no ``--spec`` is valid —
    it gets as far as the first genuine judgment point and names it).

    Returns a discriminated dict: ``{onboarded, ...}`` on success, else
    ``{needs_pick, ...}`` / ``{needs_intent, ...}`` /
    ``{needs_wrapper_argv, ...}``. Every escalation branch returns BEFORE
    the single write (the ``@register_run`` splice), so a non-onboarded
    return leaves the repo byte-identical.

    Raises ``errors.SpecInvalid`` for the structurally unonboardable
    cases: a greenfield repo (nothing to onboard —
    ``build-template`` first), an unparseable Python entry point, a
    caller ``run_name`` that is not a module-level def, and a wrapper
    ``run_name`` that cannot be derived as an identifier.
    """
    from hpc_agent.ops.detect_entry_point import detect_entry_point

    if spec is None:
        spec = WrapEntryPointAutoInput()

    root = Path(experiment_dir)

    # 1. detect — the six-probe scan, once. Every later step reads THIS
    #    block; nothing re-scans (the sequencing invariant the prose lost).
    detected = detect_entry_point(experiment_dir=root)

    # 2. resolve the entry-point FILE.
    resolved = _resolve_entry_file(detected, caller_path=spec.entry_point_path)
    if isinstance(resolved, dict):
        return resolved
    path, argv_kind, entry_rule = resolved

    # 3. resolve the entry FUNCTION (Python entry points only; a shell
    #    script / console script has none to resolve).
    func_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    already_decorated = False
    if path.endswith(".py") and argv_kind not in _NON_PYTHON_ARGV_KINDS:
        source_path = root / path
        try:
            text = source_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise errors.SpecInvalid(
                f"cannot read entry-point file {path!r}: {exc}",
                remediation=(
                    "The detection scan saw this path but it is not readable now. "
                    "Check the file still exists and re-run."
                ),
            ) from exc
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            raise errors.SpecInvalid(
                f"entry-point file {path!r} does not parse: {exc}",
                remediation=(
                    "Fix the syntax error, or pass entry_point_kind='shell_command' "
                    "to onboard the file through a wrapper without parsing it."
                ),
            ) from exc
        func = _resolve_entry_function(tree, path=path, caller_run_name=spec.run_name)
        if isinstance(func, dict):
            return func
        func_node, func_rule, already_decorated = func
        entry_rule = f"{entry_rule}+{func_rule}"

    # The mechanical-parameter verdict + the detected solver adapter, read off
    # the SAME detect block — carried onto the escalation / the fragment so no
    # consumer has to run detect-entry-point a second time.
    argv_extraction, argv_params, detected_solver = _candidate_surface(detected, path)

    entry = _EntryPoint(
        path=path,
        argv_kind=argv_kind,
        rule=entry_rule,
        function=func_node.name if func_node is not None else None,
        func_node=func_node,
        already_decorated=already_decorated,
        argv_extraction=argv_extraction,
        argv_params=argv_params,
        detected_solver=detected_solver,
    )

    # 4. the pathway decision table.
    pathway, pathway_rule = _decide_pathway(
        entry=entry, caller_entry_point_kind=spec.entry_point_kind
    )

    # 4a. the two wrapper-ONLY interview fields. Refused here — before the
    #     wrapper escalation and long before the decoration write — so a hint
    #     aimed at an introspectable pathway is named rather than dropped.
    _refuse_wrapper_only_fields(spec, pathway)

    run_name = (
        spec.run_name
        if spec.run_name is not None
        else (entry.function if entry.function is not None else _derive_run_name(path))
    )
    if pathway == "wrapper" and not _IDENTIFIER_RE.match(run_name):
        run_name = _derive_run_name(run_name)

    missing_intent = _missing_intent_fields(spec)

    # 4b. the python_module pathway (caller override only) resolves its dotted
    #     target now, so an unimportable one is refused before anything else.
    module_target: dict[str, str] | None = None
    if pathway == "module":
        module_target = _resolve_module_target(root, entry, path)
        # ``_PythonModuleEntry`` declares only {kind, module, function} with
        # extra="forbid" — it has NO fixed_params field, so a constant supplied
        # here could only be silently dropped. Refuse instead of dropping.
        if spec.fixed_params:
            raise errors.SpecInvalid(
                "fixed_params is not representable on a python_module entry "
                f"point (supplied: {sorted(spec.fixed_params)}); the wire shape "
                "carries only {kind, module, function}",
                remediation=(
                    "Give the parameter a default in the function's own "
                    "signature (python_module introspects the real signature), "
                    "or use entry_point_kind='register_run' / 'shell_command', "
                    "both of which carry fixed_params."
                ),
            )

    # 5. the wrapper pathway needs a caller argv + typed signature. Escalate
    #    with the intent gap DISCLOSED so one exchange gathers everything —
    #    and with the python_module alternative SKILL.md:98 offers for the
    #    same row NAMED, so its non-derivability is disclosed, not absent.
    if pathway == "wrapper":
        missing_wrapper = [
            field
            for field, value in (("argv", spec.argv), ("signature", spec.signature))
            if not value
        ]
        if missing_wrapper:
            return _needs_wrapper_argv(
                entry=entry,
                pathway_rule=pathway_rule,
                run_name=run_name,
                missing=missing_wrapper,
                missing_intent=missing_intent,
                python_module_alternative=_python_module_alternative(root, entry),
            )

    # 6. the frozen-YAML convention scan (a pure read; caller override wins,
    #    including an explicit empty list meaning "no YAML is identity here").
    frozen_configs = (
        list(spec.frozen_configs) if spec.frozen_configs is not None else _scan_frozen_configs(root)
    )
    sha_params = _frozen_sha_params(frozen_configs)

    # 7. human-owned intent. task_generator gates the partition too (it is
    #    the axis-param set), so escalate before partitioning. Spelled as the
    #    three explicit None-checks (rather than `if missing_intent:`) so the
    #    binding below is statically non-optional — the escalation and the
    #    narrowing are then the SAME check and cannot drift apart.
    goal, task_generator, task_count = spec.goal, spec.task_generator, spec.task_count
    if goal is None or task_generator is None or task_count is None:
        return _needs_intent(
            missing=missing_intent,
            ask=_intent_ask(missing_intent),
            pathway=pathway,
            entry=entry,
            run_name=run_name,
            partition=None,
        )

    # 8. the fixed-params partition. On the decoration AND python_module
    #    pathways the params come from the function's real signature (that is
    #    exactly what python_module exists for — the framework introspects the
    #    undecorated function); on the wrapper pathway the caller's typed
    #    `signature` IS the param inventory (a wrapper param has no default —
    #    the wrapper always passes what it is given).
    fixed_params = dict(spec.fixed_params or {})
    axis = _axis_params(task_generator)
    if entry.func_node is not None and pathway in ("decorate", "module"):
        declared, defaulted, var_kw = _declared_params(entry.func_node)
    else:
        declared, defaulted, var_kw = list(spec.signature or {}), set(), False
    partition = _partition_params(
        declared=declared,
        defaulted=defaulted,
        accepts_var_keyword=var_kw,
        axis_params=axis,
        frozen_sha_params=sha_params,
        fixed_params=fixed_params,
    )
    if partition.uncovered_params:
        return _needs_intent(
            missing=[f"entry_point.fixed_params.{name}" for name in partition.uncovered_params],
            ask=_uncovered_ask(partition.uncovered_params, run_name, pathway),
            pathway=pathway,
            entry=entry,
            run_name=run_name,
            partition=partition,
        )

    # 9. decorate — the ONLY write, and the last step, so every escalation
    #    above left the repo byte-identical.
    decorated = False
    import_added = False
    if pathway == "decorate":
        from hpc_agent.incorporation.decorate_entry_point import decorate_entry_point

        result = decorate_entry_point(path=str(root / path), function_name=run_name)
        decorated = bool(result["decorated"])
        already_decorated = bool(result["already_decorated"])
        import_added = bool(result["import_added"])

    # 10. compose the interview_spec fragment. All THREE InterviewSpec
    #     entry-point kinds are reachable; python_module carries {module,
    #     function} instead of a run_name (its wire shape has no run_name —
    #     the dotted target IS the identity).
    entry_point_kind = {"decorate": "register_run", "wrapper": "shell_command"}.get(
        pathway, "python_module"
    )
    entry_block: dict[str, Any]
    if entry_point_kind == "python_module":
        # Re-derive rather than assert: the derivation is pure, and step 4b
        # already refused every input for which it returns None.
        target = module_target or _resolve_module_target(root, entry, path)
        entry_block = {
            "kind": "python_module",
            "module": target["module"],
            "function": target["function"],
        }
    else:
        entry_block = {"kind": entry_point_kind, "run_name": run_name}
        if entry_point_kind == "shell_command":
            entry_block["argv"] = list(spec.argv or [])
            entry_block["signature"] = dict(spec.signature or {})
            entry_block["frozen_configs"] = frozen_configs
            # The two wrapper-only hints. `data_axis_hint` is copied through
            # VERBATIM — it is the experimenter's classification of an
            # uninspectable subprocess body, so nothing here re-derives or
            # normalizes it (step 4a already refused it on the other pathways).
            if spec.data_axis_hint is not None:
                entry_block["data_axis_hint"] = spec.data_axis_hint.model_dump(
                    exclude_none=True, mode="json"
                )
            # `solver`: the caller's override wins; otherwise the DETECTED
            # adapter becomes that adapter's default hint. Detection reported
            # the library and the wrapper can instrument it, so dropping it
            # here would silently cost a long solve its preemption-safety.
            solver_hint = spec.solver
            if solver_hint is None and detected_solver == "petsc":
                solver_hint = PetscSolverHint(kind="petsc")
            if solver_hint is not None:
                entry_block["solver"] = solver_hint.model_dump(exclude_none=True, mode="json")
        if fixed_params:
            entry_block["fixed_params"] = fixed_params

    return {
        "onboarded": True,
        "pathway": pathway,
        "pathway_rule": pathway_rule,
        "entry_point_kind": entry_point_kind,
        "entry_point_path": path,
        "entry_point_rule": entry.rule,
        "run_name": run_name,
        "argv_kind": entry.argv_kind,
        "decorated": decorated,
        "already_decorated": already_decorated,
        "import_added": import_added,
        "frozen_configs": frozen_configs,
        "frozen_sha_params": sha_params,
        "fixed_params": fixed_params,
        "partition": partition.as_dict(),
        # The InterviewSpec fragment — SUBMITTABLE as-is. ``produced_by`` is
        # REQUIRED by interview.input.json, so a fragment omitting it could
        # never be handed on unedited; what is emitted is the minimal
        # who-CLASS SUGGESTION ({kind: "human"}), and the interview's own
        # P1.c composer fills ``.operator`` from git config and discloses it
        # as a composed default. Attribution requiredness is untouched: this
        # verb declares that a human owns the intent (it just refused to
        # invent goal / task_generator / task_count on their behalf), never
        # who that human is.
        "interview_spec": {
            "goal": goal,
            "task_count": task_count,
            "task_generator": task_generator.model_dump(exclude_none=True, mode="json"),
            "entry_point": entry_block,
            "produced_by": {"kind": "human"},
        },
    }
