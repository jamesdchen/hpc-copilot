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

**Honesty bails** — extraction reports ``unsupported`` rather than a
partial list whenever the file's declaration is not a flat, complete,
locally-readable surface:

* argparse with no ``ArgumentParser(...)`` construction in *this* file
  (the parser is built elsewhere — the flags we can see are not the
  whole surface);
* argparse with ``add_subparsers(...)`` (subcommand-scoped flags do not
  flatten into one parameter list);
* click where more than one function carries option/argument decorators
  (a group's several commands, likewise not one flat surface).

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


def _argparse_param(call: ast.Call) -> dict[str, Any] | None:
    """One ``add_argument(...)`` call → a parameter dict (``None`` if unreadable)."""
    names = [
        value for value in (_literal_str(arg) for arg in call.args) if value is not None and value
    ]
    if not names:
        return None
    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
    positional = not names[0].startswith("-")

    explicit_dest = _literal_str(kwargs["dest"]) if "dest" in kwargs else None
    param: dict[str, Any] = {
        "names": names,
        "dest": explicit_dest or _argparse_dest(names),
        "positional": positional,
    }
    if "type" in kwargs:
        param["type"] = _unparse(kwargs["type"])
    _apply_default(param, kwargs.get("default"))
    required = _literal_bool(kwargs.get("required"))
    if required is not None:
        param["required"] = required
    action = _literal_str(kwargs.get("action")) if "action" in kwargs else None
    if action in ("store_true", "store_false", "count"):
        # A value-less flag: the wrapper argv appends the flag itself, never
        # ``<flag> <value>``. Reported uniformly with click's ``is_flag``.
        param["is_flag"] = True
    return param


def _argparse_dest(names: list[str]) -> str:
    """argparse's own dest rule: first long option, else first short, else the name."""
    for name in names:
        if name.startswith("--"):
            return name[2:].replace("-", "_")
    for name in names:
        if name.startswith("-"):
            return name[1:].replace("-", "_")
    return names[0].replace("-", "_")


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
    """One ``@click.option`` / ``@click.argument`` decorator → a parameter dict."""
    names = [
        value for value in (_literal_str(arg) for arg in call.args) if value is not None and value
    ]
    if not names:
        return None
    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
    param: dict[str, Any] = {
        "names": names,
        "dest": _click_dest(names),
        "positional": is_argument,
    }
    if "type" in kwargs:
        param["type"] = _unparse(kwargs["type"])
    _apply_default(param, kwargs.get("default"))
    required = _literal_bool(kwargs.get("required"))
    if required is not None:
        param["required"] = required
    if _literal_bool(kwargs.get("is_flag")) is True:
        param["is_flag"] = True
    return param


def _click_dest(names: list[str]) -> str:
    """click's own name rule: the LONGEST declared name, dashes stripped."""
    longest = max(names, key=len)
    return longest.lstrip("-").replace("-", "_")


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
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
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
