"""Signature → :class:`~hpc_agent.executor_cli.Flag` synthesis.

Two entry points share one rule set:

- :func:`flags_from_signature` introspects a live function object via
  :mod:`inspect`. Used by :func:`hpc_agent.incorporation.template.register_run`.
- :func:`flags_from_ast` reads an :class:`ast.FunctionDef` without
  importing the module. Used by :func:`hpc_agent.incorporation.template.discover_runs`,
  which must run in a stdlib-only environment that lacks the
  experiment's heavy dependencies (``torch`` / ``pandas`` / …).

Mapping rules — one parameter annotation → one :class:`Flag`:

==========================  =========================================
annotation                  Flag
==========================  =========================================
``str``                     ``type=str``
``int`` / ``float``         ``type=int`` / ``type=float``
``bool``                    ``action="store_true"`` (``store_false``
                            when the default is ``True``)
``X | None`` / ``Optional``  unwrap to ``X``; never ``required``
``list[T]``                 ``type=T``, ``nargs="+"``
``Literal[a, b, ...]``      ``choices=(a, b, ...)``
missing annotation          ``type=str`` plus a :func:`warnings.warn`
==========================  =========================================

A parameter with no default becomes ``required=True`` unless it is
optional or a store-true flag.
"""

from __future__ import annotations

import ast
import inspect
import types
import typing
import warnings
from typing import Any

from hpc_agent.executor_cli import Flag, flag, generic_args, gpu_args

__all__ = ["flags_from_signature", "flags_from_ast", "flags_for_run"]

# Annotations we know how to turn into an argparse ``type=`` callable.
_SCALARS: dict[Any, type] = {str: str, int: int, float: float}
_SCALAR_NAMES: dict[str, type] = {"str": str, "int": int, "float": float}


# ─── runtime (inspect-based) ────────────────────────────────────────────────


def flags_from_signature(func: Any) -> list[Flag]:
    """Return the per-parameter :class:`Flag` list for *func*'s signature."""
    try:
        # ``eval_str`` resolves the string annotations left behind by a
        # ``from __future__ import annotations`` in the experiment module.
        sig = inspect.signature(func, eval_str=True)
    except (NameError, TypeError, SyntaxError, AttributeError):
        sig = inspect.signature(func)
    out: list[Flag] = []
    for p in sig.parameters.values():
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if p.name in ("self", "cls"):
            continue
        has_default = p.default is not inspect.Parameter.empty
        default = p.default if has_default else None
        out.append(_runtime_flag(p.name, p.annotation, has_default, default))
    return out


def flags_for_run(func: Any, *, gpu: bool = False) -> list[Flag]:
    """Full FLAGS list for an executor wrapping *func*.

    Combines :func:`generic_args` (and :func:`gpu_args` when *gpu*), the
    planner's ``--halo`` flag, and the signature-derived flags. On a name
    collision the signature wins — the framework flag is dropped.
    """
    sig_flags = flags_from_signature(func)
    sig_names = {f.name for f in sig_flags}
    base = [f for f in generic_args() if f.name not in sig_names]
    if gpu:
        base += [f for f in gpu_args() if f.name not in sig_names]
    if "halo" not in sig_names:
        base.append(
            flag(
                "halo",
                int,
                default=0,
                help="Warm-up rows replayed before the emit range "
                "(set by the parallelization planner; 0 for a whole-series run).",
            )
        )
    return base + sig_flags


def _runtime_flag(name: str, annotation: Any, has_default: bool, default: Any) -> Flag:
    if isinstance(annotation, str):
        # A string annotation: ``eval_str=True`` could not resolve it
        # (e.g. it references a name only imported under TYPE_CHECKING),
        # or it was authored as a string. Route through the AST
        # classifier rather than silently degrading to ``str`` — it
        # reads int / bool / list[T] / X | None / Literal[...] from the
        # annotation text just as well.
        try:
            node: ast.expr | None = ast.parse(annotation, mode="eval").body
        except SyntaxError:
            node = None
        return _ast_flag(name, node, has_default, default)

    required = not has_default
    ftype: type | None = str
    nargs: str | None = None
    choices: tuple[Any, ...] | None = None
    action: str | None = None

    if annotation is inspect.Parameter.empty:
        warnings.warn(
            f"@register_run parameter {name!r} has no type annotation; "
            "treating it as a string flag",
            stacklevel=3,
        )
    else:
        ann, optional = _strip_optional(annotation)
        if optional:
            required = False
        origin = typing.get_origin(ann)
        args = typing.get_args(ann)
        if origin is typing.Literal:
            choices = tuple(args)
            ftype = _choice_type(args)
        elif origin is list:
            elem = args[0] if args else str
            ftype = _SCALARS.get(elem, str)
            nargs = "+"
        elif ann is bool:
            action = "store_false" if (has_default and default is True) else "store_true"
            ftype = None
        elif ann in _SCALARS:
            ftype = _SCALARS[ann]
        else:
            ftype = str

    if action is not None:
        required = False
        if not has_default:
            default = action == "store_false"
    return Flag(
        name=name,
        type=ftype,
        default=default,
        required=required,
        choices=choices,
        nargs=nargs,
        action=action,
    )


def _strip_optional(ann: Any) -> tuple[Any, bool]:
    """Unwrap ``Optional[X]`` / ``X | None``; return ``(inner, was_optional)``.

    An ambiguous union with more than one non-``None`` member collapses
    to ``str`` — the cluster-side argparse downcasts via the ``type``
    ctor anyway, and ``str`` is the safe lossless carrier.
    """
    origin = typing.get_origin(ann)
    if origin is typing.Union or origin is types.UnionType:
        args = typing.get_args(ann)
        non_none = tuple(a for a in args if a is not type(None))
        was_optional = len(non_none) != len(args)
        if len(non_none) == 1:
            return non_none[0], was_optional
        return str, was_optional
    return ann, False


def _choice_type(args: tuple[Any, ...]) -> type:
    t = type(args[0]) if args else str
    return t if (t in _SCALARS or t is bool) else str


# ─── AST (no-import) ────────────────────────────────────────────────────────


def flags_from_ast(funcdef: ast.FunctionDef | ast.AsyncFunctionDef) -> list[Flag]:
    """Return the per-parameter :class:`Flag` list for an AST function def.

    Mirrors :func:`flags_from_signature` but reads :class:`ast.arg`
    nodes, so it never imports the experiment module.
    """
    a = funcdef.args
    posargs = list(a.posonlyargs) + list(a.args)
    pos_defaults = list(a.defaults)
    n_required = len(posargs) - len(pos_defaults)

    out: list[Flag] = []
    for i, arg in enumerate(posargs):
        if arg.arg in ("self", "cls"):
            continue
        if i >= n_required:
            out.append(
                _ast_flag(arg.arg, arg.annotation, True, _literal(pos_defaults[i - n_required]))
            )
        else:
            out.append(_ast_flag(arg.arg, arg.annotation, False, None))
    for arg, dflt in zip(a.kwonlyargs, a.kw_defaults, strict=False):
        has_default = dflt is not None
        # ``_literal(dflt) if dflt`` (truthy check) used to convert
        # falsy literal defaults — 0, 0.0, "", False, [] — to ``None``,
        # which then leaked into the synthesized Flag's default and the
        # run_signature_sha. Test for presence explicitly.
        out.append(
            _ast_flag(
                arg.arg,
                arg.annotation,
                has_default,
                _literal(dflt) if dflt is not None else None,
            )
        )
    return out


def _literal(node: ast.expr | None) -> Any:
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None


def _ast_flag(name: str, ann: ast.expr | None, has_default: bool, default: Any) -> Flag:
    required = not has_default
    ftype: type | None = str
    nargs: str | None = None
    choices: tuple[Any, ...] | None = None
    action: str | None = None

    if ann is not None:
        node, optional = _ast_strip_optional(ann)
        if optional:
            required = False
        kind, payload = _ast_classify(node)
        if kind == "literal":
            choices = tuple(payload)
            ftype = _choice_type(choices)
        elif kind == "list":
            ftype = payload or str
            nargs = "+"
        elif kind == "bool":
            action = "store_false" if (has_default and default is True) else "store_true"
            ftype = None
        elif kind == "scalar":
            ftype = payload
        else:
            ftype = str

    if action is not None:
        required = False
        if not has_default:
            default = action == "store_false"
    return Flag(
        name=name,
        type=ftype,
        default=default,
        required=required,
        choices=choices,
        nargs=nargs,
        action=action,
    )


def _ast_name(node: ast.expr) -> str | None:
    """Last component of a Name / dotted Attribute (``typing.Optional`` → ``Optional``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _ast_strip_optional(node: ast.expr) -> tuple[ast.expr, bool]:
    # X | None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left_none = _is_none(node.left)
        right_none = _is_none(node.right)
        if right_none and not left_none:
            return node.left, True
        if left_none and not right_none:
            return node.right, True
        return node, False
    if isinstance(node, ast.Subscript):
        name = _ast_name(node.value)
        if name == "Optional":
            return node.slice, True
        if name == "Union":
            elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            non_none = [e for e in elts if not _is_none(e)]
            was_optional = len(non_none) != len(elts)
            if len(non_none) == 1:
                return non_none[0], was_optional
            return node, was_optional
    return node, False


def _is_none(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    return isinstance(node, ast.Name) and node.id in ("None", "NoneType")


def _ast_classify(node: ast.expr) -> tuple[str, Any]:
    if isinstance(node, ast.Subscript):
        name = _ast_name(node.value)
        if name == "Literal":
            elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            return "literal", [_literal(e) for e in elts]
        if name in ("list", "List"):
            elem = node.slice
            return "list", _SCALAR_NAMES.get(_ast_name(elem) or "", str)
        return "other", None
    name = _ast_name(node)
    if name == "bool":
        return "bool", None
    if name in _SCALAR_NAMES:
        return "scalar", _SCALAR_NAMES[name]
    return "other", None
