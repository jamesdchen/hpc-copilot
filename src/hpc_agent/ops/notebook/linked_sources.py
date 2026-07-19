"""Import → source-file resolution: the ONE definition shared by two verbs.

Extracted verbatim from ``notebook-lint``'s rule-3 machinery (2026-07-07, the
draft-context plan's "one resolution definition" requirement). ``notebook-lint``
reports imports that resolve to a file under a caller ``source_root``; the
``notebook-draft-context`` projection resolves the SAME way to point the drafting
agent at each engine's defining file. Both must resolve identically — so the
resolution lives here once and both import it, rather than forking a second copy.

Pure, stdlib-only (``ast`` + the shared hashing primitive): judges import ORIGIN
IDENTITY only, never import content/semantics (the Q1 boundary flag). Relative
imports (``level > 0``) are skipped — a relative origin is inside the same
package, not a cross-``source_root`` link. An import that resolves to nothing is
stdlib / site-packages, never a link (returned as unresolved, never a finding).
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from collections import deque
from collections.abc import Callable, Iterable, Set
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from hpc_agent._wire.actions.notebook_lint import LinkedSource
from hpc_agent.state.audit_source import sha256_normalized

__all__ = [
    "imported_modules",
    "resolve_module_file",
    "resolve_linked_sources",
    "module_sha_map",
    "LinkedEngine",
    "resolve_section_engines",
    "AuditNetTier",
    "AuditNetEntry",
    "resolve_audit_net",
    "_CLOSURE_MAX_MODULES",
]


def resolve_module_file(module: str, root_dirs: list[Path]) -> Path | None:
    """Resolve a dotted *module* name to a file under one of *root_dirs*.

    ``foo.bar`` → ``foo/bar.py`` or ``foo/bar/__init__.py`` under each root; the
    first hit (roots in declared order) wins. ``None`` when nothing resolves —
    an unresolvable import is stdlib / site-packages, never a link.

    When the module's FIRST component names the root itself (``src.data.loading``
    under root ``src`` — the repo-root-relative import style), the root-prefixed
    probe would double the prefix (``src/src/data/loading.py``); the leading
    component is also tried stripped. Every candidate stays under a declared
    root, so the lint boundary (links resolve UNDER a source_root) is unchanged.
    """
    parts = module.split(".")
    rel = Path(*parts)
    for root in root_dirs:
        candidates = [root / rel.with_suffix(".py"), root / rel / "__init__.py"]
        if parts[0] == root.name:
            if len(parts) == 1:
                candidates.append(root / "__init__.py")
            else:
                stripped = Path(*parts[1:])
                candidates.extend(
                    (root / stripped.with_suffix(".py"), root / stripped / "__init__.py")
                )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None


def imported_modules(tree: ast.Module) -> list[str]:
    """Dotted module names an ``import`` / ``from`` statement brings in.

    For ``from pkg import name`` both ``pkg`` and ``pkg.name`` are candidates
    (``name`` may be a submodule file); the resolver keeps whichever exists.
    Relative imports (``level > 0``) are skipped — a relative origin is inside
    the same package, not a cross-``source_root`` link this rule reports.
    """
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0 or not node.module:
                continue
            modules.append(node.module)
            modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def resolve_linked_sources(
    tree: ast.Module,
    experiment_dir: Path,
    root_dirs: list[Path],
) -> list[LinkedSource]:
    """Report imports resolving to a file under a declared ``source_root``.

    Deduped by resolved file (two import forms can name one origin). ``module_sha``
    is the shared hashing primitive over the file text — the exact value T9
    recomputes to drift-check the link.
    """
    seen_files: set[Path] = set()
    linked: list[LinkedSource] = []
    for module in imported_modules(tree):
        resolved = resolve_module_file(module, root_dirs)
        if resolved is None:
            continue
        resolved = resolved.resolve()
        if resolved in seen_files:
            continue
        seen_files.add(resolved)
        try:
            rel = str(resolved.relative_to(experiment_dir.resolve()))
        except ValueError:
            rel = str(resolved)
        linked.append(
            LinkedSource(
                module=module,
                file=rel,
                module_sha=sha256_normalized(resolved.read_text(encoding="utf-8")),
            )
        )
    return linked


def module_sha_map(tree: ast.Module, root_dirs: list[Path]) -> dict[str, str]:
    """Map every dotted module *tree* imports that resolves under *root_dirs* → its ``module_sha``.

    The ONE definition behind the executor↔notebook drift lint (both sides call
    THIS): a module name is resolved through :func:`resolve_module_file` (never a
    re-inlined ``<pkg>/__init__.py`` probe) and hashed with the shared
    :func:`~hpc_agent.state.audit_source.sha256_normalized`. A name that resolves
    to nothing (stdlib / site-packages) is simply absent — never an entry. The
    two callers pass DIFFERENT ``root_dirs`` (the audited source resolves under the
    declared ``source_roots``; the executor resolves under its own directory plus
    those roots — Python's runtime posture) so a module that resolves to a
    SHADOWING local copy on one side diverges in sha from the shared one on the
    other; identical resolution yields an identical sha and no drift.
    """
    out: dict[str, str] = {}
    for module in imported_modules(tree):
        if module in out:
            continue
        resolved = resolve_module_file(module, root_dirs)
        if resolved is None:
            continue
        out[module] = sha256_normalized(resolved.read_text(encoding="utf-8"))
    return out


@dataclass(frozen=True)
class LinkedEngine:
    """One import in a SECTION that resolves to an engine file under a ``source_root``.

    The presentation atom the sign-off render's src-digest block is built from
    (notebook-audit interactivity, slice 1): the human signs knowing WHICH source
    version bound. ``module`` is the display name (``M`` for ``import M``, ``M.f``
    for ``from M import f``); ``file`` is the resolved engine's experiment-relative
    POSIX path; ``lineno`` / ``signature`` locate + describe the imported SYMBOL
    (``None`` for a whole-module import); ``module_sha12`` is the first 12 chars of
    the file's :func:`~hpc_agent.state.audit_source.sha256_normalized` — the same
    hash ``notebook-lint``'s ``linked_sources`` and the graduation gate use.
    """

    module: str
    file: str
    lineno: int | None
    signature: str | None
    module_sha12: str


def _section_imports(tree: ast.Module) -> list[tuple[str, str | None]]:
    """``(module, symbol|None)`` for every import in *tree*, in document order.

    ``import M`` / ``import M.sub`` → ``(name, None)`` (the whole module is the
    engine); ``from M import f`` → ``(M, f)``. Relative imports (``level > 0``) and
    ``import *`` are skipped — the ``linked_sources`` boundary (a relative origin
    is inside the same package, not a cross-``source_root`` link).
    """
    out: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, None) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if (node.level and node.level > 0) or not node.module:
                continue
            out.extend((node.module, alias.name) for alias in node.names if alias.name != "*")
    return out


def _locate_symbol(tree: ast.Module, symbol: str) -> tuple[int, str | None] | None:
    """``(lineno, signature|None)`` for a top-level ``def``/``class`` named *symbol*.

    Signature is ``ast.unparse`` of a function's argument list; a class has none
    (``None``). ``None`` when *symbol* is not a top-level definition in *tree*. The
    same both-shapes location ``notebook-draft-context`` does — never executing an
    import.
    """
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and stmt.name == symbol:
            return stmt.lineno, ast.unparse(stmt.args)
        if isinstance(stmt, ast.ClassDef) and stmt.name == symbol:
            return stmt.lineno, None
    return None


def resolve_section_engines(
    section_source: str, experiment_dir: Path, root_dirs: list[Path]
) -> list[LinkedEngine]:
    """Resolve a section's imports to :class:`LinkedEngine` digests under *root_dirs*.

    Reuses the ONE resolver (:func:`resolve_module_file`) — a ``from M import f``
    tries ``M`` first (locating ``f`` inside it for ``lineno``/``signature``) then
    ``M.f`` as a submodule; anything that resolves to nothing is stdlib /
    site-packages and yields no engine (never a finding). Deduped by resolved file
    (two import forms can name one origin). Pure of trust — the module_sha reflects
    the file on disk NOW, so a re-render moves only when the bound source moved.
    """
    try:
        tree = ast.parse(section_source)
    except SyntaxError:
        return []
    engines: list[LinkedEngine] = []
    seen_files: set[Path] = set()
    for module, symbol in _section_imports(tree):
        resolved = resolve_module_file(module, root_dirs)
        display = module
        located: tuple[int, str | None] | None = None
        if resolved is not None and symbol is not None:
            display = f"{module}.{symbol}"
            engine_tree = _parse_tolerant(resolved.read_text(encoding="utf-8"))
            if engine_tree is not None:
                located = _locate_symbol(engine_tree, symbol)
        elif resolved is None and symbol is not None:
            # `from M import f` where `f` is a SUBMODULE, not a name in M.
            resolved = resolve_module_file(f"{module}.{symbol}", root_dirs)
            display = f"{module}.{symbol}"
        if resolved is None:
            continue
        resolved = resolved.resolve()
        if resolved in seen_files:
            continue
        seen_files.add(resolved)
        try:
            rel = resolved.relative_to(experiment_dir.resolve()).as_posix()
        except ValueError:
            rel = resolved.as_posix()
        text = resolved.read_text(encoding="utf-8")
        engines.append(
            LinkedEngine(
                module=display,
                file=rel,
                lineno=located[0] if located is not None else None,
                signature=located[1] if located is not None else None,
                module_sha12=sha256_normalized(text)[:12],
            )
        )
    return engines


def _parse_tolerant(text: str) -> ast.Module | None:
    """``ast.parse`` returning ``None`` on a SyntaxError (a file mid-edit contributes nothing)."""
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


# ── the audit net (notebook-audit 6a): transitive closure with per-module tiers ──
#
# ``resolve_linked_sources`` (above) resolves only a module's DIRECT imports —
# the first hop. The audit net extends that to the FULL transitive closure:
# every module the seeds import, and everything THOSE import, recursively — so
# the graduation gate can bind (and the human sign against) the whole dependency
# cone, not just its surface. Each module lands in exactly one of four tiers:
#
#   * INHERITED   — resolves under a source root AND is template-identical (the
#                   template imports the same module) OR ledger-attested (its
#                   current sha is human-signed, ``module_sha_signed``);
#   * EXTERNAL    — stdlib or installed site-packages (never under a source
#                   root); bound by the submit-time env_hash, which the gate
#                   discloses (this module only CARRIES the tier);
#   * UNRESOLVED  — resolves nowhere (not a source root, not stdlib, not
#                   installed): a real finding, never silent;
#   * NEW_DRIFTED — resolves under a source root but is neither template-
#                   identical nor ledger-attested (a new / drifted dependency).
#
# Resolution reuses the ONE ``resolve_module_file`` definition and the ONE
# ``sha256_normalized`` hashing primitive — no re-forked probe, no second hash.
# Environment authority is the LOCAL env (``sys.stdlib_module_names`` +
# ``importlib.util.find_spec``): the net NEVER executes a module under test.

#: Closure cap. A transitive cone can be unbounded (a hub module that imports
#: half of site-packages); the BFS stops characterizing at this many modules and
#: DISCLOSES the cap (a marker finding upstream) — never a silent truncation.
_CLOSURE_MAX_MODULES = 256


class AuditNetTier(Enum):
    """The four audit-net tiers a closure module classifies into (6a)."""

    INHERITED = "inherited"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"
    NEW_DRIFTED = "new_drifted"


@dataclass(frozen=True)
class AuditNetEntry:
    """One module in the audit net's transitive closure.

    * ``module`` — the dotted import name.
    * ``file`` — the resolved file's path (experiment-relative when under the
      experiment dir, else absolute); ``None`` for EXTERNAL / UNRESOLVED modules
      (nothing resolves under a source root for them).
    * ``module_sha`` — :func:`~hpc_agent.state.audit_source.sha256_normalized`
      over the file text; ``None`` for EXTERNAL / UNRESOLVED (no local file).
    * ``tier`` — the :class:`AuditNetTier` the module classifies into.
    * ``via`` — the import chain from the seed that FIRST reached this module,
      inclusive: ``via[0]`` is a seed and ``via[-1] == module``.
    """

    module: str
    file: str | None
    module_sha: str | None
    tier: AuditNetTier
    via: tuple[str, ...]


def _is_stdlib_module(module: str) -> bool:
    """True iff *module*'s top-level package is a stdlib module name."""
    return module.split(".")[0] in sys.stdlib_module_names


def _is_installed_module(module: str) -> bool:
    """True iff *module* is importable from the local env (site-packages).

    ``importlib.util.find_spec`` WITHOUT importing (the 6a never-exec boundary).
    Any resolution error (a missing parent package, an invalid name) reads as
    not-installed — the conservative default that keeps a genuinely local module
    out of the EXTERNAL tier.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _relative_to_experiment(resolved: Path, experiment_dir: Path) -> str:
    """*resolved* relative to *experiment_dir* when under it, else absolute.

    The SAME convention ``resolve_linked_sources`` uses for ``LinkedSource.file``
    (one path posture across both surfaces).
    """
    try:
        return str(resolved.relative_to(experiment_dir.resolve()))
    except ValueError:
        return str(resolved)


def _classify_audit_module(
    module: str,
    resolved: Path | None,
    module_sha: str | None,
    template_modules: Set[str],
    sha_is_signed: Callable[[str], bool] | None,
) -> AuditNetTier:
    """The tier one closure module lands in (the 6a classification, one place).

    A module resolving under a source root is INHERITED when it is template-
    identical (the template imports it too — same resolver + same roots ⇒ the
    same file, so the same sha) OR ledger-attested (its current sha is signed);
    otherwise NEW_DRIFTED. A module resolving to nothing is EXTERNAL when the
    local env claims it (stdlib or installed) and UNRESOLVED otherwise.
    """
    if resolved is not None:
        if module in template_modules:
            return AuditNetTier.INHERITED
        if module_sha is not None and sha_is_signed is not None and sha_is_signed(module_sha):
            return AuditNetTier.INHERITED
        return AuditNetTier.NEW_DRIFTED
    if _is_stdlib_module(module) or _is_installed_module(module):
        return AuditNetTier.EXTERNAL
    return AuditNetTier.UNRESOLVED


def resolve_audit_net(
    seed_modules: Iterable[str],
    experiment_dir: Path,
    root_dirs: list[Path],
    *,
    template_modules: Set[str] = frozenset(),
    sha_is_signed: Callable[[str], bool] | None = None,
    max_modules: int = _CLOSURE_MAX_MODULES,
) -> tuple[list[AuditNetEntry], bool]:
    """BFS the transitive import closure from *seed_modules*; tier every module.

    The seeds (a module's direct imports) are expanded iteratively: each module
    that RESOLVES under *root_dirs* is parsed (never executed) and its own
    imports enqueue, so the net walks the whole dependency cone. The visited set
    is keyed on the RESOLVED Path (a module name for the unresolvable / external
    case), so a diamond collapses to one entry per file and a cycle (``A <-> B``,
    a self-import) terminates — the first discovery of a file wins its ``via``
    chain. Returns ``(entries, cap_hit)``: ``entries`` sorted by module name
    (deterministic — two runs over the same tree are byte-identical), and
    ``cap_hit`` True iff the closure reached *max_modules* (the net may then be
    incomplete; the caller DISCLOSES this, never silently).

    *template_modules* (the template's own direct imports) drives the
    template-identical leg of INHERITED; *sha_is_signed* (the ledger predicate,
    ``module_sha_signed``) drives the ledger-attested leg — a resolved module
    matching neither is NEW_DRIFTED. Both default to empty (no leg fires).

    A package NAMESPACE a resolved submodule answers for is not itself UNRESOLVED:
    ``from pkg import name`` offers BOTH ``pkg`` and ``pkg.name`` as candidates,
    and when ``pkg.name`` resolves under a root, ``pkg`` is merely its namespace
    prefix (the same permissive posture ``resolve_linked_sources`` keeps) — so it
    is filtered out of the UNRESOLVED set rather than flagged.
    """
    experiment_dir = Path(experiment_dir)
    entries: dict[tuple[str, str], AuditNetEntry] = {}
    queue: deque[tuple[str, tuple[str, ...]]] = deque()
    for module in sorted(set(seed_modules)):
        queue.append((module, (module,)))

    cap_hit = False
    while queue:
        module, via = queue.popleft()
        resolved = resolve_module_file(module, root_dirs)
        key = ("path", str(resolved.resolve())) if resolved is not None else ("name", module)
        if key in entries:
            continue  # already characterized (cycle / diamond) — first discovery wins.
        if len(entries) >= max_modules:
            cap_hit = True
            break  # the disclosed cap: stop characterizing, never truncate silently.

        module_sha: str | None = None
        text: str | None = None
        if resolved is not None:
            text = resolved.read_text(encoding="utf-8")
            module_sha = sha256_normalized(text)
        tier = _classify_audit_module(module, resolved, module_sha, template_modules, sha_is_signed)
        entries[key] = AuditNetEntry(
            module=module,
            file=_relative_to_experiment(resolved, experiment_dir)
            if resolved is not None
            else None,
            module_sha=module_sha,
            tier=tier,
            via=via,
        )
        if text is not None:
            children = _parse_tolerant(text)
            if children is not None:
                for child in sorted(set(imported_modules(children))):
                    queue.append((child, (*via, child)))

    ordered = sorted(entries.values(), key=lambda entry: entry.module)
    resolved_names = {entry.module for entry in ordered if entry.file is not None}
    ordered = [
        entry
        for entry in ordered
        if not (
            entry.tier is AuditNetTier.UNRESOLVED
            and any(name.startswith(entry.module + ".") for name in resolved_names)
        )
    ]
    return ordered, cap_hit
