"""``discover_runs`` — find ``@register_run`` functions without importing.

The agent-facing counterpart to :func:`hpc_agent.experiment_kit.register_run`.
A repo's executors may import ``torch`` / ``pandas`` / a private CUDA
build; importing them just to enumerate experiment entry points is slow
and fragile. :func:`discover_runs` instead walks each ``.py`` file —
and each ``.ipynb`` notebook — with :mod:`ast`, so it runs in a
stdlib-only environment. Notebooks are scanned natively because an
*exported* executor inlines the runtime and no longer carries the
``hpc_agent.experiment_kit`` import the decorator-alias resolver keys off; the
notebook is the source of truth for "what experiments exist".

It resolves every spelling of the decorator:

- bare — ``from hpc_agent.experiment_kit import register_run`` → ``@register_run``
- top-level — ``from hpc_agent import register_run`` (the form SKILL.md
  documents as canonical; ``register_run`` is lazily re-exported from
  ``hpc_agent.__init__``) → ``@register_run``
- aliased — ``... import register_run as rr`` → ``@rr``
- attribute — ``import hpc_agent.experiment_kit`` →
  ``@hpc_agent.experiment_kit.register_run``
- module-aliased — ``from hpc_agent import template`` → ``@template.register_run``

and the parameterised call form ``@register_run(gpu=True)``.
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from hpc_agent.executor_cli import Flag
from hpc_agent.experiment_kit.signature import ast_has_var_keyword, flags_from_ast

__all__ = ["RunInfo", "discover_runs", "run_signature_sha"]

# Vendored / VCS / cache dirs never holding user run-source. Kept in lock-step
# with ``state.discover._SKIP_DIRS`` and the ``state.discover_cache`` fingerprint
# skip set (a run vendored inside one of these is not discoverable, so the cache
# may prune it).
_SKIP_DIRS = frozenset(
    {".hpc", ".git", "__pycache__", ".mypy_cache", ".venv", "venv", "node_modules", ".claude"}
)
_DECORATOR_NAME = "register_run"
# Module paths a `register_run` import may come from.
_SOURCE_MODULES = ("hpc_agent.experiment_kit", "hpc_agent.experiment_kit.register")


@dataclass(frozen=True)
class RunInfo:
    """One ``@register_run`` function found by an AST walk.

    Attributes
    ----------
    path:
        Absolute path of the file the run was found in.
    name:
        The decorated function's name.
    gpu:
        Whether the decorator was ``@register_run(gpu=True)``.
    mpi:
        Whether the decorator was ``@register_run(mpi=True)`` (#293) — a
        multi-rank entry point whose ``rank`` / ``world_size`` params the
        launcher fills, so they are excluded from :attr:`flags`.
    flags:
        The CLI :class:`~hpc_agent.executor_cli.Flag` list synthesised
        from the function signature (see
        :func:`hpc_agent.experiment_kit.flags_from_ast`).
    has_var_keyword:
        Whether the run declares a ``**kwargs`` catch-all (AST-visible; not
        reflected in :attr:`flags`, which only synthesises named parameters).
        A run with ``**kwargs`` absorbs any surplus kwarg, so a swept-flag
        cross-check downgrades a name mismatch from refuse to warn.
    run_signature_sha:
        A stable SHA-256 over the synthesised :attr:`flags` — the
        run's *parallelization-relevant* fingerprint. A stored
        ``DataAxis`` classification (``axes.yaml``'s
        ``executors.<name>``) is reused only while this hash is
        unchanged; a signature edit invalidates the classification and
        triggers a fresh interview.
    """

    path: Path
    name: str
    gpu: bool
    mpi: bool
    flags: tuple[Flag, ...]
    run_signature_sha: str
    # Defaulted so every existing RunInfo(...) call site (the discover-cache
    # reconstructor, tests) stays valid; discover_runs always sets it explicitly.
    has_var_keyword: bool = False


def discover_runs(src_dir: str | Path) -> list[RunInfo]:
    """Scan *src_dir* for ``@register_run`` functions.

    Returns a list of :class:`RunInfo` sorted by ``(path, name)``. A
    file or directory may be passed; directories are walked recursively,
    skipping ``.hpc`` / VCS / cache directories.
    """
    root = Path(src_dir)
    if root.is_file():
        files = [root]
    elif root.is_dir():
        files = sorted([*root.rglob("*.py"), *root.rglob("*.ipynb")])
    else:
        files = []

    found: list[RunInfo] = []
    for path in files:
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(_read_source(path))
        except (OSError, SyntaxError, ValueError):
            # ValueError covers json.JSONDecodeError for a malformed .ipynb.
            continue
        bare, modules = _decorator_aliases(tree)
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                decoded = _decorator_flags(dec, bare, modules)
                if decoded is None:
                    continue
                gpu, mpi = decoded
                flags = tuple(flags_from_ast(node, mpi=mpi))
                found.append(
                    RunInfo(
                        path=path.resolve(),
                        name=node.name,
                        gpu=gpu,
                        mpi=mpi,
                        flags=flags,
                        run_signature_sha=run_signature_sha(flags),
                        has_var_keyword=ast_has_var_keyword(node),
                    )
                )
                break

    return sorted(found, key=lambda r: (str(r.path), r.name))


def run_signature_sha(flags: tuple[Flag, ...]) -> str:
    """Return a stable SHA-256 fingerprint of a run's synthesised flags.

    The hash is order-sensitive (signature order is meaningful) and
    canonicalises each :class:`~hpc_agent.executor_cli.Flag` to a plain
    dict — the ``type`` field, an argparse callable, is reduced to its
    ``__name__`` so two structurally-identical signatures hash equal
    regardless of object identity. Any non-JSON default falls back to
    ``repr`` so the hash never raises.
    """
    canon = [
        {
            "name": f.name,
            "type": getattr(f.type, "__name__", None) if f.type is not None else None,
            "default": f.default,
            "required": f.required,
            "choices": list(f.choices) if f.choices is not None else None,
            "nargs": f.nargs,
            "action": f.action,
        }
        for f in flags
    ]
    blob = json.dumps(canon, sort_keys=True, default=repr)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read_source(path: Path) -> str:
    """Return Python source for *path* — a ``.py`` file or ``.ipynb`` notebook.

    For a notebook the code cells are concatenated in order, so the
    ``@register_run`` decorator and its ``hpc_agent.experiment_kit`` import
    appear as ordinary top-level nodes for the AST walk.
    """
    if path.suffix == ".ipynb":
        data = json.loads(path.read_text(encoding="utf-8"))
        parts: list[str] = []
        for cell in data.get("cells", []):
            if cell.get("cell_type") != "code":
                continue
            src = cell.get("source", "")
            if isinstance(src, list):
                src = "".join(src)
            parts.append(src)
        return "\n".join(parts)
    return path.read_text(encoding="utf-8")


def _decorator_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Return ``(bare_names, module_names)`` for the ``register_run`` decorator.

    ``bare_names`` are local names bound directly to ``register_run``;
    ``module_names`` are dotted names that resolve to a module from
    which ``register_run`` is an attribute.
    """
    bare: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _SOURCE_MODULES:
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module in _SOURCE_MODULES:
                for alias in node.names:
                    if alias.name == _DECORATOR_NAME:
                        bare.add(alias.asname or alias.name)
            elif node.module == "hpc_agent":
                # ``from hpc_agent import register_run`` — lazily re-exported
                # from the top-level package (the form SKILL.md documents).
                # ``from hpc_agent import template`` — the template package.
                for alias in node.names:
                    if alias.name == _DECORATOR_NAME:
                        bare.add(alias.asname or alias.name)
                    elif alias.name == "template":
                        modules.add(alias.asname or alias.name)
    return bare, modules


def _decorator_flags(dec: ast.expr, bare: set[str], modules: set[str]) -> tuple[bool, bool] | None:
    """Return ``(gpu, mpi)`` if *dec* is a ``register_run`` decorator, else ``None``.

    Reads the ``@register_run(gpu=..., mpi=...)`` keyword constants; a bare
    ``@register_run`` yields ``(False, False)``.
    """
    gpu = False
    mpi = False
    target = dec
    if isinstance(dec, ast.Call):
        target = dec.func
        for kw in dec.keywords:
            if kw.arg == "gpu" and isinstance(kw.value, ast.Constant):
                gpu = bool(kw.value.value)
            elif kw.arg == "mpi" and isinstance(kw.value, ast.Constant):
                mpi = bool(kw.value.value)

    if isinstance(target, ast.Name):
        return (gpu, mpi) if target.id in bare else None
    if isinstance(target, ast.Attribute) and target.attr == _DECORATOR_NAME:
        return (gpu, mpi) if _dotted(target.value) in modules else None
    return None


def _dotted(node: ast.expr) -> str | None:
    """Flatten a ``Name`` / dotted ``Attribute`` into ``"a.b.c"``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None
