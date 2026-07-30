"""AST parameter EXTRACTION for the two mechanical CLI frameworks (P1.d).

:mod:`hpc_agent.ops.detect_entry_point` already *classifies* a candidate's
CLI surface (``argv_kind``: argparse / click / typer / hydra / fire /
``__main__``). Classification alone still left the wrapper path — where the
onboarding agent must hand-author ``entry_point.argv`` + ``signature`` — to
the LLM reading the file and typing out flag names, i.e. the exact
hand-authoring the prelude mechanization removes ("mechanize all parts of
the chain that don't need a decision", ``docs/plans/prelude-chain-2026-07-30.md``).

Two frameworks declare their parameters *mechanically* — as literal
arguments to a call the AST can read without importing or executing the
user's code:

* **argparse** — ``parser.add_argument("--seed", type=int, default=0)``;
* **click** — ``@click.option("--seed", type=int)`` /
  ``@click.argument("input")`` decorators.

For those two this module returns a structured parameter list. For
**typer / hydra / fire / ``__main__`` / console scripts / shell** entry
points it returns ``(unsupported, None)`` — honestly, never a guess: typer
derives its CLI from Python type hints, hydra from a composed YAML tree,
fire from a live signature, and a shell script has no Python surface at
all. Guessing there would produce a wrapper argv that fails on the
cluster, so the LLM keeps that leg (the determinism-boundary rule cuts
both ways: mechanize what is rule-fixed, and refuse to mechanize what is
not).

**Two honesty levels, one rule** ("``extracted`` is never a guess"). The
level is chosen by the SCOPE of what is unknowable, and nothing is ever
silently dropped:

1. **Verdict degradation** — reserved for a *whole-surface* unknown, where
   even the parameter COUNT would be a claim we cannot make. The verdict
   becomes ``unsupported`` and ``argv_params`` is ``None``:

   * argparse with no ``ArgumentParser(...)`` construction in *this* file
     (the parser is built elsewhere — the flags we can see are not the
     whole surface);
   * argparse with ``add_subparsers(...)`` (subcommand-scoped flags do not
     flatten into one parameter list);
   * click with no ``@click.command`` / ``@click.group`` in this file, or
     with more than one function carrying parameter decorators (a group's
     several commands, likewise not one flat surface);
   * an unparseable file, or a non-extractable ``argv_kind``.

2. **Per-param ``unextracted`` marker** — for a *single parameter* whose
   names are readable but one written argument is not modeled (a custom
   ``action=`` class, a non-literal ``nargs=`` / ``choices=``, click's
   repeat-``count=``, a ``dest`` that does not sanitize to an attribute
   name). The param is still emitted — its names, dest and the arguments
   that ARE literal are real information — and ``unextracted`` NAMES every
   argument the consumer must read out of the source itself. The verdict
   stays ``extracted``: the parameter list is complete in count and names,
   and the one gap is labelled rather than papered over.

   This is deliberately the ``compose_audit_template`` posture (a
   candidate whose manifest fails to load is NAMED in the disclosure's
   ``skipped`` key, never silently dropped) applied one level down. A
   consumer that composes an argv MUST treat a param carrying
   ``unextracted`` as an LLM leg.

Pure AST: :func:`ast.parse` + literal reads only. Nothing in this module
imports, executes, or evaluates user code, so a repo with unavailable
third-party dependencies still extracts (the Q4 boundary rule — core CI
verifies this without argparse-consuming or click installed, because
neither is ever imported).
"""

from __future__ import annotations

import ast
import keyword
from typing import Any

__all__ = [
    "EXTRACTABLE_ARGV_KINDS",
    "EXTRACTION_EXTRACTED",
    "EXTRACTION_UNSUPPORTED",
    "extract_argv_params",
    "sanitize_identifier",
]

#: ``argv_extraction`` values. ``extracted`` — ``argv_params`` is the
#: mechanically read parameter list (possibly empty: a parser that declares
#: no flags). ``unsupported`` — ``argv_params`` is ``None`` and the caller
#: keeps the LLM leg.
EXTRACTION_EXTRACTED = "extracted"
EXTRACTION_UNSUPPORTED = "unsupported"

#: The only ``argv_kind`` values whose parameters are declared mechanically.
EXTRACTABLE_ARGV_KINDS: frozenset[str] = frozenset({"argparse", "click"})


def extract_argv_params(source: str, *, argv_kind: str) -> tuple[str, list[dict[str, Any]] | None]:
    """Extract *source*'s CLI parameters for a mechanical framework.

    Returns ``(EXTRACTION_EXTRACTED, params)`` for argparse / click when the
    declaration is a flat locally-readable surface, else
    ``(EXTRACTION_UNSUPPORTED, None)`` — see the module docstring for every
    bail condition. Never raises: an unparseable file is ``unsupported``
    (the candidate still stands; only its parameter list is unknown).
    """
    if argv_kind not in EXTRACTABLE_ARGV_KINDS:
        return (EXTRACTION_UNSUPPORTED, None)
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return (EXTRACTION_UNSUPPORTED, None)
    params = _extract_argparse(tree) if argv_kind == "argparse" else _extract_click(tree)
    if params is None:
        return (EXTRACTION_UNSUPPORTED, None)
    return (EXTRACTION_EXTRACTED, params)


def sanitize_identifier(text: str, *, fallback: str = "run") -> str | None:
    """Coerce *text* into a valid, non-keyword Python identifier, or ``None``.

    Used by the interview's ``entry_point.run_name`` composer (the wrapper
    file + its ``@register_run`` function are named after the detected
    candidate's stem, which may legally carry ``-`` / ``.`` / a leading
    digit). Non-identifier characters collapse to ``_``; a result that
    cannot start an identifier — or that is a Python keyword — is prefixed
    with ``<fallback>_``. Returns ``None`` when nothing usable survives
    (e.g. an empty or all-separator stem), so the caller refuses instead of
    inventing a name.
    """
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in text)
    cleaned = cleaned.strip("_")
    if not cleaned:
        return None
    if not (cleaned[0].isalpha() or cleaned[0] == "_") or keyword.iskeyword(cleaned):
        cleaned = f"{fallback}_{cleaned}"
    return cleaned if cleaned.isidentifier() and not keyword.iskeyword(cleaned) else None


# ─── argparse ──────────────────────────────────────────────────────────────


def _extract_argparse(tree: ast.Module) -> list[dict[str, Any]] | None:
    """Every ``*.add_argument(...)`` in *tree*, or ``None`` to bail honestly.

    The receiver is deliberately unchecked: parsers, argument groups,
    mutually-exclusive groups and sub-parsers all expose ``add_argument``
    under whatever local name the author chose, and a name-based filter
    would silently drop the group-scoped flags. The bails that DO apply are
    structural (module docstring): no local ``ArgumentParser(...)``, or any
    ``add_subparsers(...)``.
    """
    saw_parser = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = _called_name(node.func)
        if called == "ArgumentParser":
            saw_parser = True
        elif called == "add_subparsers":
            # Subcommand-scoped flags are not one flat parameter list.
            return None
    if not saw_parser:
        # The parser is constructed elsewhere; whatever add_argument calls
        # live here are not provably the whole surface.
        return None

    params: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _called_name(node.func) != "add_argument":
            continue
        param = _argparse_param(node)
        if param is not None:
            params.append(param)
    return params


#: argparse actions that consume NO value (the wrapper appends the flag alone).
_ARGPARSE_VALUELESS_ACTIONS = frozenset({"store_true", "store_false", "count"})
#: argparse actions whose arity/repeat semantics this module fully models.
#: Anything else (``append``, ``extend``, ``store_const``, ``version``, a
#: custom Action class) is recorded verbatim AND marked ``unextracted``.
_ARGPARSE_MODELED_ACTIONS = _ARGPARSE_VALUELESS_ACTIONS | {"store"}


def _argparse_param(call: ast.Call) -> dict[str, Any] | None:
    """One ``add_argument(...)`` call → a parameter dict (``None`` if unreadable)."""
    names = [
        value for value in (_literal_str(arg) for arg in call.args) if value is not None and value
    ]
    if not names:
        return None
    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
    unextracted: list[str] = []
    positional = not names[0].startswith("-")

    explicit_dest = _literal_str(kwargs["dest"]) if "dest" in kwargs else None
    if "dest" in kwargs and explicit_dest is None:
        # ``dest=`` written as an expression — the derived fallback below is
        # NOT what argparse will bind, so say so.
        unextracted.append("dest")
    param: dict[str, Any] = {
        "names": names,
        "dest": explicit_dest or _option_dest(names).replace("-", "_"),
        "positional": positional,
    }
    if "type" in kwargs:
        param["type"] = _unparse(kwargs["type"])
    _apply_default(param, kwargs.get("default"))
    _apply_required(param, kwargs, unextracted)
    if "action" in kwargs:
        action = _literal_str(kwargs["action"])
        if action is None:
            # A custom Action class / expression: arity unknowable.
            unextracted.append("action")
        else:
            param["action"] = action
            if action in _ARGPARSE_VALUELESS_ACTIONS:
                # Value-less: the wrapper argv appends the flag itself, never
                # ``<flag> <value>``. Reported uniformly with click's is_flag.
                param["is_flag"] = True
            if action not in _ARGPARSE_MODELED_ACTIONS:
                unextracted.append("action")
    _apply_nargs(param, kwargs, unextracted)
    _apply_choices(param, kwargs, unextracted)
    _finish_param(param, unextracted)
    return param


def _option_dest(opts: list[str]) -> str:
    """First long option, else first short, else the bare name — dashes intact.

    argparse and click agree on this selection: argparse takes the first
    long option string, and click's ``max(possible_names, key=len(prefix))``
    resolves to the same one (a ``--`` prefix outranks ``-``, and ``max``
    keeps the first of equals). Callers apply their own post-processing —
    argparse converts dashes to underscores, click also lowercases.
    """
    for opt in opts:
        if opt.startswith("--"):
            return opt[2:]
    for opt in opts:
        if opt.startswith("-"):
            return opt[1:]
    return opts[0]


# ─── click ─────────────────────────────────────────────────────────────────

_CLICK_PARAM_DECORATORS = frozenset({"option", "argument"})
_CLICK_COMMAND_DECORATORS = frozenset({"command", "group"})


def _extract_click(tree: ast.Module) -> list[dict[str, Any]] | None:
    """Option/argument decorators of the ONE decorated function, or ``None``.

    Both spellings are read: ``@click.option(...)`` (attribute form) and the
    ``from click import option`` bare-name form. Two structural bails, the
    same shape as argparse's: no ``@click.command`` / ``@click.group``
    declared in THIS file (the command is assembled elsewhere, so the
    decorators we can see are not provably the whole surface — an EMPTY
    param list would read as "this command takes no flags", a claim we
    cannot make), and two-or-more functions carrying parameter decorators
    (a group of several commands does not flatten into one list).
    """
    if not _declares_click_command(tree):
        return None
    decorated: list[list[dict[str, Any]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        params: list[dict[str, Any]] = []
        # Source order (top-to-bottom) IS click's final parameter order:
        # each decorator appends to ``__click_params__`` bottom-up and
        # ``Command`` reverses that list, so the two coincide.
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            called = _called_name(decorator.func)
            if called not in _CLICK_PARAM_DECORATORS:
                continue
            param = _click_param(decorator, is_argument=called == "argument")
            if param is not None:
                params.append(param)
        if params:
            decorated.append(params)
    if len(decorated) > 1:
        return None
    return decorated[0] if decorated else []


def _declares_click_command(tree: ast.Module) -> bool:
    """``True`` when a ``@click.command`` / ``@click.group`` is declared here."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if _called_name(target) in _CLICK_COMMAND_DECORATORS:
                return True
    return False


def _click_param(call: ast.Call, *, is_argument: bool) -> dict[str, Any] | None:
    """One ``@click.option`` / ``@click.argument`` decorator → a parameter dict.

    click's own ``_parse_decls`` rules are reproduced rather than approximated:

    * a declaration NOT starting with ``-`` is the EXPLICIT parameter name and
      outranks every derivation (``@click.option("--a-very-long-flag", "x")``
      binds ``x``) — the analogue of argparse's ``dest=``;
    * a declaration containing ``/`` is a boolean on/off PAIR
      (``"--shout/--no-shout"``): the left side is the primary option, the
      right the secondary, the parameter consumes NO value, and the name comes
      from the primary. Reading such a decl as one opaque name is what
      produced the ``seed/__no_seed`` non-identifier;
    * otherwise the name is the first long option, else the first short one,
      lowercased with dashes converted (an argument lowercases too).
    """
    decls = [
        value for value in (_literal_str(arg) for arg in call.args) if value is not None and value
    ]
    if not decls:
        return None
    explicit_name, opts, secondary, saw_pair = _click_decls(decls)
    if opts:
        names = opts
    elif explicit_name is not None:
        names = [explicit_name]
    else:
        return None

    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
    unextracted: list[str] = []
    if explicit_name is not None:
        # Arguments lowercase + underscore their single decl; an option's
        # explicit name is taken verbatim.
        dest = explicit_name.replace("-", "_").lower() if is_argument else explicit_name
    else:
        dest = _option_dest(opts).replace("-", "_").lower()
    param: dict[str, Any] = {
        "names": names,
        "dest": dest,
        "positional": is_argument,
    }
    if secondary:
        param["secondary_names"] = secondary
    if saw_pair:
        # A boolean pair is implicitly a flag in click — without this a
        # consumer would emit ``--shout/--no-shout <value>``.
        param["is_flag"] = True
    if "type" in kwargs:
        param["type"] = _unparse(kwargs["type"])
    _apply_default(param, kwargs.get("default"))
    _apply_required(param, kwargs, unextracted)
    if "is_flag" in kwargs:
        is_flag = _literal_bool(kwargs["is_flag"])
        if is_flag is None:
            unextracted.append("is_flag")
        elif is_flag:
            param["is_flag"] = True
    if "multiple" in kwargs:
        multiple = _literal_bool(kwargs["multiple"])
        if multiple is None:
            unextracted.append("multiple")
        elif multiple:
            param["multiple"] = True
    if "count" in kwargs:
        # A counting option is value-less AND repeatable; the repeat-to-integer
        # collection is not modeled, so the flag half is reported and the
        # counting half is NAMED.
        if _literal_bool(kwargs["count"]) is True:
            param["is_flag"] = True
        unextracted.append("count")
    _apply_nargs(param, kwargs, unextracted)
    _apply_choices(param, kwargs, unextracted)
    _finish_param(param, unextracted)
    return param


def _click_decls(decls: list[str]) -> tuple[str | None, list[str], list[str], bool]:
    """Split click declaration strings into ``(name, opts, secondary, saw_pair)``."""
    explicit_name: str | None = None
    opts: list[str] = []
    secondary: list[str] = []
    saw_pair = False
    for decl in decls:
        if not decl.startswith("-"):
            explicit_name = decl
            continue
        if "/" in decl:
            saw_pair = True
            primary, _, off = decl.partition("/")
            if primary:
                opts.append(primary)
            if off:
                secondary.append(off)
            continue
        opts.append(decl)
    return explicit_name, opts, secondary, saw_pair


# ─── shared literal reads ──────────────────────────────────────────────────


def _called_name(func: ast.expr) -> str | None:
    """The bare callee name for ``f(...)`` / ``a.b.f(...)``, else ``None``."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _literal_str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_bool(node: ast.expr | None) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _apply_required(
    param: dict[str, Any], kwargs: dict[str, ast.expr], unextracted: list[str]
) -> None:
    """Record a written ``required=`` bool; mark it when it is an expression."""
    if "required" not in kwargs:
        return
    required = _literal_bool(kwargs["required"])
    if required is None:
        unextracted.append("required")
        return
    param["required"] = required


def _apply_nargs(
    param: dict[str, Any], kwargs: dict[str, ast.expr], unextracted: list[str]
) -> None:
    """Record a written ``nargs=`` literal — an int (incl. click's ``-1``) or a
    ``"+"`` / ``"*"`` / ``"?"`` string.

    ``nargs`` changes a parameter's ARITY, so dropping it silently would let a
    consumer emit ``--tags <one-value>`` for a flag that consumes many. A
    literal is represented verbatim; anything else is marked.
    """
    if "nargs" not in kwargs:
        return
    value = _literal_value(kwargs["nargs"])
    if isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
        param["nargs"] = value
        return
    unextracted.append("nargs")


def _apply_choices(
    param: dict[str, Any], kwargs: dict[str, ast.expr], unextracted: list[str]
) -> None:
    """Record a written ``choices=`` literal sequence; mark anything else.

    ``choices`` bounds the value DOMAIN — a sweep that steps outside it fails
    at parse time on the cluster, so the set is worth carrying when it is
    literal (and worth naming when it is a ``range`` / a module constant).
    """
    if "choices" not in kwargs:
        return
    value = _literal_value(kwargs["choices"])
    if isinstance(value, list | tuple):
        jsonable = _as_jsonable(list(value))
        if jsonable is not _UNJSONABLE:
            param["choices"] = jsonable
            return
    unextracted.append("choices")


def _finish_param(param: dict[str, Any], unextracted: list[str]) -> None:
    """Attach the ``unextracted`` marker (de-duplicated, stable order), if any.

    Also the last-resort net for the ``dest`` contract: a derived dest that is
    not a usable attribute name means this module misread the declaration, so
    it is NAMED rather than emitted as if authoritative (the class the click
    ``--x/--no-x`` pair fell into before the pair split existed).
    """
    dest = param.get("dest")
    if not (isinstance(dest, str) and dest.isidentifier()):
        unextracted.append("dest")
    if unextracted:
        param["unextracted"] = sorted(set(unextracted))


_MISSING = object()


def _literal_value(node: ast.expr) -> Any:
    """``ast.literal_eval(node)`` or :data:`_MISSING` — never raises, never executes."""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return _MISSING


def _unparse(node: ast.expr) -> str:
    """Source text of a non-literal argument (e.g. ``type=pathlib.Path``).

    Reported as a STRING, never resolved: the framework must not import the
    user's module to learn what ``type=`` names.
    """
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):  # pragma: no cover — 3.9-era fallback
        return "<unreadable>"


def _apply_default(param: dict[str, Any], node: ast.expr | None) -> None:
    """Record a written ``default=`` as a JSON literal, else as its source text.

    ``default`` carries the value only when it round-trips to JSON;
    otherwise ``default_source`` carries the expression verbatim
    (``os.cpu_count()``, a module-level constant, …) so the caller sees
    that a default EXISTS without the framework pretending to know it.
    """
    if node is None:
        return
    value = _literal_value(node)
    if value is _MISSING:
        param["default_source"] = _unparse(node)
        return
    jsonable = _as_jsonable(value)
    if jsonable is _UNJSONABLE:
        param["default_source"] = _unparse(node)
        return
    param["default"] = jsonable


_UNJSONABLE = object()


def _as_jsonable(value: Any) -> Any:
    """*value* as JSON-safe data, or :data:`_UNJSONABLE`.

    ``ast.literal_eval`` also yields tuples / sets / bytes / complex, none of
    which survive a JSON round-trip; a tuple is converted (it renders as a
    list either way), the rest are refused so the disclosure never carries a
    value the schema would reject.
    """
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        # NaN / inf are not JSON — refuse rather than emit invalid JSON.
        finite = value == value and value not in (float("inf"), float("-inf"))
        return value if finite else _UNJSONABLE
    if isinstance(value, list | tuple):
        items = [_as_jsonable(item) for item in value]
        return _UNJSONABLE if any(item is _UNJSONABLE for item in items) else items
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                return _UNJSONABLE
            converted = _as_jsonable(item)
            if converted is _UNJSONABLE:
                return _UNJSONABLE
            out[key] = converted
        return out
    return _UNJSONABLE
