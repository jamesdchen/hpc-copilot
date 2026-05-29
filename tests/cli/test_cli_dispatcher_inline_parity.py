"""Parity between ``hpc_agent.executor_cli`` and the inlined ``.hpc/cli.py``.

The cluster runs a stdlib-only ``.hpc/cli.py`` copied verbatim from
``templates/scaffolds/cli_dispatcher.py``; it inlines its own ``Flag`` +
``build_parser_from_flags`` because ``hpc_agent`` is not installed on the
compute node. But the auto-generated ``.hpc/tasks.py`` builds its FLAGS via
``from hpc_agent.executor_cli import flag`` — so the FLAGS entries are
``hpc_agent.executor_cli.Flag`` instances, a *different type* from the
inlined ``Flag``. ``isinstance`` is ``False`` across that class-identity
gap, which is exactly the ``TypeError: ... got Flag`` that broke the
``python -m cli`` dispatch path on the cluster (#177).

These tests pin two things:

1. **Behavioral** — the inlined dispatcher accepts a foreign (executor_cli)
   ``Flag`` and vice-versa (the #177 regression).
2. **Structural** — the shared ``Flag`` / ``_coerce_flag`` /
   ``build_parser_from_flags`` definitions stay identical (AST compare,
   docstrings normalized away) so the two copies cannot silently drift.
   This is the AST-compare the scaffold docstring promised.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import sys
from pathlib import Path

import pytest

import hpc_agent.executor_cli as canonical
from tests._paths import TEMPLATES_DIR

_DISPATCHER_PATH = TEMPLATES_DIR / "scaffolds" / "cli_dispatcher.py"


def _load_module_from(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the inlined ``@dataclass(frozen=True) Flag`` looks
    # itself up via ``sys.modules[cls.__module__]`` during class creation, so a
    # module absent from sys.modules raises AttributeError mid-exec.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# The main block is ``if __name__ == "__main__"``-guarded, so importing the
# scaffold is side-effect-free.
inlined = _load_module_from(_DISPATCHER_PATH, "cli_dispatcher_under_test")


# ─── behavioral / cross-class round-trip (the #177 repro) ──────────────────


def test_inlined_dispatcher_accepts_foreign_executor_cli_flag() -> None:
    """A real ``hpc_agent.executor_cli.Flag`` must parse through the inlined
    ``build_parser_from_flags`` — the exact #177 failure (TypeError: got Flag)."""
    flags = [
        *canonical.generic_args(),
        canonical.flag("horizon", int, default=1),
        canonical.flag("segment", str, choices=("am", "pm")),
    ]
    parser = inlined.build_parser_from_flags(flags, description="src.ml_ridge")
    args = parser.parse_args(["--output-file", "o.csv", "--horizon", "5", "--segment", "pm"])
    assert args.horizon == 5
    assert args.segment == "pm"
    assert args.output_file == "o.csv"


def test_canonical_accepts_inlined_flag() -> None:
    """Symmetric: an inlined ``Flag`` parses through the canonical builder."""
    parser = canonical.build_parser_from_flags([inlined.Flag(name="horizon", type=int, default=1)])
    assert parser.parse_args(["--horizon", "7"]).horizon == 7


def test_get_parser_dispatch_path_with_real_flag(tmp_path) -> None:
    """End-to-end: a tasks.FLAGS built from ``executor_cli.flag`` flows through
    the dispatcher's ``get_parser`` — the ``python -m cli`` lookup path (#177)."""
    (tmp_path / "tasks.py").write_text(
        "from hpc_agent.executor_cli import flag, generic_args\n"
        "FLAGS = {'src.m': [*generic_args(), flag('horizon', int, default=1)]}\n"
        "def total(): return 1\n"
        "def resolve(i): return {'horizon': 1}\n"
    )
    (tmp_path / "cli.py").write_text(_DISPATCHER_PATH.read_text(encoding="utf-8"))
    # Load the COPY so its sibling tasks.py (the one above) is what it reads.
    mod = _load_module_from(tmp_path / "cli.py", "cli_copy_under_test")
    parser = mod.get_parser("src.m")
    args = parser.parse_args(["--output-file", "o.csv", "--horizon", "3"])
    assert args.horizon == 3


def test_inlined_still_accepts_dicts_and_rejects_garbage() -> None:
    p = inlined.build_parser_from_flags([{"name": "k", "type": int, "default": 0}])
    assert p.parse_args(["--k", "9"]).k == 9
    with pytest.raises(TypeError, match="must be Flag instances or dicts"):
        inlined.build_parser_from_flags(["nope"])  # type: ignore[list-item]


# ─── structural parity (the AST compare the scaffold docstring promised) ───


def _top_level_node(tree: ast.Module, name: str) -> ast.AST:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name:
            return node
    raise AssertionError(f"{name!r} not found at module top level")


def _strip_docstrings(node: ast.AST) -> ast.AST:
    """Drop the docstring from every function/class body in the subtree so an
    ``ast.dump`` compares logic and signatures, not prose."""
    for n in list(ast.walk(node)):
        body = getattr(n, "body", None)
        if (
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            n.body = body[1:]
    return node


@pytest.mark.parametrize("name", ["Flag", "_coerce_flag", "build_parser_from_flags"])
def test_inlined_node_matches_canonical_ast(name: str) -> None:
    canonical_tree = ast.parse(Path(canonical.__file__).read_text(encoding="utf-8"))
    inlined_tree = ast.parse(_DISPATCHER_PATH.read_text(encoding="utf-8"))
    a = _strip_docstrings(_top_level_node(canonical_tree, name))
    b = _strip_docstrings(_top_level_node(inlined_tree, name))
    assert ast.dump(a) == ast.dump(b), (
        f"{name} drifted between hpc_agent.executor_cli and the inlined "
        "cli_dispatcher scaffold; keep them identical modulo docstrings (#177)."
    )


def test_flag_dataclass_fields_match() -> None:
    """The two Flag dataclasses must share the same field names + defaults."""
    cf = [(f.name, f.default) for f in dataclasses.fields(canonical.Flag)]
    inf = [(f.name, f.default) for f in dataclasses.fields(inlined.Flag)]
    assert cf == inf
