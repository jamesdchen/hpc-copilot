"""``export_notebook`` — lift a ``.ipynb`` into a ``.py`` executor module.

hpc-agent has no notebook path of its own; a researcher with a notebook
otherwise hand-translates it into an executor. :func:`export_notebook`
does that mechanically, with a deliberately strict rule so the result is
predictable:

**Emit a top-level statement iff it is one of**

- ``import`` / ``from ... import``
- a function definition (``def`` / ``async def``)
- a class definition
- an assignment whose every target is an UPPERCASE name (module
  constants, e.g. ``WINDOW = 48``)

Everything else — smoke-test calls, plots, ``df = pd.read_csv(...)``
scratch lines, expression statements — is skipped **silently**. No cell
tags, no magic markers: the AST shape *is* the contract. A researcher
keeps the importable surface (the ``@register_run`` function, its
imports, its constants) and leaves the exploratory code in the notebook.

Statements are emitted in source order (cell order, then within a cell).
A cell that does not parse is skipped whole.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

__all__ = ["export_notebook"]

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_IMPORT_NODES = (ast.Import, ast.ImportFrom)


def export_notebook(ipynb: str | Path, out_py: str | Path) -> Path:
    """Extract the importable surface of *ipynb* into *out_py*.

    Returns the path written. The output is a plain ``.py`` module: when
    it contains a ``@register_run`` function it is a ready hpc-agent
    executor (``compute`` is injected at import time).
    """
    data = json.loads(Path(ipynb).read_text(encoding="utf-8"))
    chunks: list[str] = []

    for cell in data.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        src = cell.get("source", "")
        if isinstance(src, list):
            src = "".join(src)
        if not src.strip():
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for node in tree.body:
            if _is_exportable(node):
                segment = _segment(lines, node)
                if segment:
                    chunks.append(segment)

    body = "\n\n\n".join(chunks)
    text = f"{body}\n" if body else ""

    out_path = Path(out_py)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return out_path


def _segment(lines: list[str], node: ast.stmt) -> str:
    """Return *node*'s exact source, decorators included.

    ``ast.get_source_segment`` reports a decorated ``def``/``class`` from
    the ``def``/``class`` keyword — it omits the ``@decorator`` lines. We
    extend the start back to the first decorator so an exported
    ``@register_run`` function keeps its decorator.
    """
    start = node.lineno
    if isinstance(node, _DEF_NODES) and node.decorator_list:
        start = node.decorator_list[0].lineno
    end = node.end_lineno or start
    return "\n".join(lines[start - 1 : end])


def _is_exportable(node: ast.stmt) -> bool:
    if isinstance(node, (_IMPORT_NODES, _DEF_NODES)):
        return True
    if isinstance(node, ast.Assign):
        return bool(node.targets) and all(_is_upper_name(t) for t in node.targets)
    if isinstance(node, ast.AnnAssign):
        return _is_upper_name(node.target)
    return False


def _is_upper_name(target: ast.expr) -> bool:
    """True for a bare ``NAME`` target whose identifier is uppercase.

    ``str.isupper()`` ignores underscores and digits, so ``WINDOW``,
    ``TRAIN_WINDOW`` and ``_TASKS`` all qualify while ``df`` / ``model``
    do not. Tuple / subscript / attribute targets never qualify.
    """
    return isinstance(target, ast.Name) and target.id.isupper()
