"""``decorate-entry-point`` — deterministic ``@register_run`` decoration.

Replaces the free-form ``Edit`` the ``hpc-wrap-entry-point`` skill used to apply
in Step 3a. The prose said "two-line edit", but the ``Edit`` tool let the LLM
rewrite the whole function — and one autonomous run authored a Monte-Carlo body
into a scaffold stub instead of just decorating it. This verb is a bounded AST
line-splice: it inserts exactly ``from hpc_agent import register_run`` (when
absent) plus ``@register_run`` on the named function, and **cannot touch the
body** — that is the whole point. The affordance to author code is removed, not
forbidden by prose.

The ``@register_run`` contract is exactly those two textual lines; every
registration side effect happens at import time off the function's own signature
(:func:`hpc_agent.experiment_kit._runtime.register_run`), so a textual insert is
sufficient — no boilerplate to author.

Refuses (``spec_invalid``) when the file doesn't parse, the named function is not
a module-level ``def``, or it carries a signature-rewriting decorator
(``@hydra.main``, a consuming ``@click.command`` / ``@app.command``) that
``@register_run`` can't see through — those route to the wrapper fallback (3b) or
the ``python_module`` executor path instead.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.experiment_kit.axis_matcher._ast_utils import _called_name, _find_function

__all__ = ["decorate_entry_point"]

_IMPORT_MODULES = ("hpc_agent", "hpc_agent.experiment_kit")
# Decorators that rewrite the wrapped callable's signature so @register_run can't
# introspect the real kwargs. Route these to the 3b wrapper / python_module path.
_REWRITER_DOTTED = frozenset({"hydra.main"})
_REWRITER_LAST_ATTR = frozenset({"command", "group"})  # click / typer consuming forms


def _deco_dotted(deco: ast.expr) -> str:
    """Flatten a decorator expression to a dotted name.

    Handles both the bare form (``@click.command`` → ``Attribute``) and the
    called form (``@click.command()`` → ``Call`` whose ``.func`` is the
    ``Attribute``). Returns ``""`` for shapes :func:`_called_name` can't flatten.
    """
    target = deco.func if isinstance(deco, ast.Call) else deco
    return _called_name(target)


def _is_signature_rewriter(dotted: str) -> bool:
    if dotted in _REWRITER_DOTTED:
        return True
    return dotted.rsplit(".", 1)[-1] in _REWRITER_LAST_ATTR


def _is_register_run(dotted: str) -> bool:
    return dotted.rsplit(".", 1)[-1] == "register_run"


@primitive(
    name="decorate-entry-point",
    verb="mutate",
    side_effects=[SideEffect("filesystem", "<path> (in-place: import + @register_run)")],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="path",
    cli=CliShape(
        help=(
            "Insert `from hpc_agent import register_run` + `@register_run` onto a "
            "named module-level function via a structure-preserving AST line-splice "
            "(body untouched). Refuses signature-rewriting decorators "
            "(hydra/click/typer) — route those to the wrapper fallback."
        ),
        verb="decorate-entry-point",
        args=(
            CliArg("--path", required=True, help="Python file containing the entry function."),
            CliArg(
                "--function-name",
                required=True,
                help="Name of the module-level function to decorate.",
            ),
        ),
    ),
    agent_facing=True,
)
def decorate_entry_point(*, path: str, function_name: str) -> dict[str, Any]:
    """Decorate *function_name* in *path* with ``@register_run`` (idempotent).

    Returns ``{path, function_name, decorated, already_decorated, import_added,
    lines_changed}``. ``decorated`` is ``False`` with ``already_decorated=True``
    when the function already carries ``@register_run`` (no write).
    """
    src_path = Path(path)
    try:
        # Decode bytes directly — Path.read_text() applies universal-newline
        # translation (\r\n -> \n) which would erase the file's real line endings
        # before we can preserve them; read_text(newline=...) is 3.13-only.
        text = src_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise errors.SpecInvalid(f"cannot read entry-point file {path!r}: {e}") from e

    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise errors.SpecInvalid(f"entry-point file {path!r} does not parse: {e}") from e

    node = _find_function(tree, function_name)
    if node is None:
        raise errors.SpecInvalid(
            f"function {function_name!r} is not a module-level def in {path!r}; "
            "route to the wrapper fallback (3b) / python_module path"
        )

    decos = [_deco_dotted(d) for d in node.decorator_list]
    if any(_is_register_run(d) for d in decos):
        return {
            "path": str(src_path),
            "function_name": function_name,
            "decorated": False,
            "already_decorated": True,
            "import_added": False,
            "lines_changed": 0,
        }
    rewriter = next((d for d in decos if _is_signature_rewriter(d)), None)
    if rewriter is not None:
        raise errors.SpecInvalid(
            f"function {function_name!r} carries a signature-rewriting decorator "
            f"@{rewriter}; @register_run cannot see through it — route to the "
            "wrapper fallback (3b) / python_module path"
        )

    lines = text.splitlines(keepends=True)
    nl = "\r\n" if "\r\n" in text else "\n"
    indent = " " * node.col_offset

    deco_idx = (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1

    import_idx: int | None = None
    has_import = any(
        isinstance(n, ast.ImportFrom)
        and n.module in _IMPORT_MODULES
        and any(a.name == "register_run" for a in n.names)
        for n in tree.body
    )
    if not has_import:
        anchor = 0  # 1-based line after which to insert; 0 == very top of file
        if (
            tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ):
            anchor = max(anchor, tree.body[0].end_lineno or 0)
        for n in tree.body:
            if isinstance(n, ast.ImportFrom) and n.module == "__future__":
                anchor = max(anchor, n.end_lineno or 0)
        import_idx = anchor

    # Splice in descending index order so the earlier (import) index stays valid
    # after the later (decorator) insertion. The decorator is always at or below
    # the import anchor, so insert it first.
    lines.insert(deco_idx, f"{indent}@register_run{nl}")
    lines_changed = 1
    import_added = False
    if import_idx is not None:
        lines.insert(import_idx, f"from hpc_agent import register_run{nl}")
        lines_changed += 1
        import_added = True

    new_text = "".join(lines)
    tmp = src_path.with_name(src_path.name + ".hpc-decorate-tmp")
    tmp.write_bytes(new_text.encode("utf-8"))  # exact bytes, no newline translation
    os.replace(tmp, src_path)

    return {
        "path": str(src_path),
        "function_name": function_name,
        "decorated": True,
        "already_decorated": False,
        "import_added": import_added,
        "lines_changed": lines_changed,
    }
