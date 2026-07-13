"""CI lint: a durable artifact must not be written with a truncating open.

Generator G12 of ``docs/plans/upstream-fixes-2026-07.md``
("bare-writes-vs-one-atomic-discipline"): four independent torn-write bugs
(#42 dossier zip, #52 pack manifest, #57 external ``settings.json`` /
``.claude.json``, #61 ``experiment_meta.json`` pin) all rewrote a *durable*
artifact in place with a truncating ``Path.write_text`` / ``ZipFile(path, "w")``
even though the repo owns the atomic recipe. A truncating open destroys the
previously-good file the instant it opens; a crash / kill / power loss mid-write
then leaves a torn or empty file that every later reader misreads as absent or
partial — and for the content-addressed pack manifest, a torn write spuriously
REVOKES every clearance signed under the intact bytes.

The one discipline (``infra/io.py``): route durable writes through
:func:`~hpc_agent.infra.io.atomic_write_json` (serialized JSON),
:func:`~hpc_agent.infra.io.atomic_write_text` (a pre-serialized string whose
exact bytes are load-bearing), or :func:`~hpc_agent.infra.io.atomic_replace_path`
(a temp-sibling context manager for an artifact a third-party writer builds by
path, e.g. ``ZipFile``). Each does tmp + fsync + ``os.replace`` + parent-dir
fsync, so a torn durable file is impossible. This lint is that discipline's
enforcement row: a NEW truncating write to a durable artifact fails CI instead
of landing silently.

What it flags
-------------

A truncating write form —

* ``<target>.write_text(...)`` / ``<target>.write_bytes(...)``, or
* ``zipfile.ZipFile(<target>, "w"/"x", ...)`` (append mode ``"a"`` and read mode
  are left alone),

— whose ``<target>`` is a DURABLE artifact. Durability is evidenced two ways,
both LOCAL to the write's own function (no cross-module dataflow is attempted):

* the target is a bare name in :data:`DURABLE_VARNAMES` (``manifest_path``,
  ``settings_path``, ``config_path``, ``archive_path`` — the conventional
  durable-artifact path variables), or
* the target name was assigned, anywhere in the same function, from an
  expression containing a :data:`DURABLE_BASENAMES` string literal (e.g.
  ``p = p / "experiment_meta.json"`` taints ``p``).

Deliberately NOT flagged
------------------------

* A write whose target is a ``Path(...)`` / ``dir / "name"`` expression built
  inline (receiver is not a bare Name) — the coarse rule attributes durability
  only to a *named* target; inline throwaway writes are out of scope.
* ``conformance/`` and ``execution/mapreduce/templates/`` — those trees
  materialize test fixtures and generated user code (a scaffold's
  ``settings.json`` fixture, a rendered ``tasks.py``), never the framework's own
  durable state. The whole subtree is skipped.
* The already-atomic durable writers (``state/runs.py``, ``block_terminal``,
  ``axes.py``, …): they call the ``infra/io`` helpers, not ``write_text``, so
  there is no truncating call to match.

ALLOWLIST escape valve
----------------------

A genuinely non-durable write that happens to trip the heuristic (a durable-named
variable that is really a temp / regenerable cache) adds a cited entry to
:data:`ALLOWLIST` (scan-root-relative ``path::function``) — the same escape valve
``lint_remote_read_ack.py`` / ``lint_no_raw_ssh.py`` use. It is empty today: the
rule fires on exactly the four G12 members, and each is fixed to route through
``infra/io``, so HEAD is clean.

Every violation surfaces a ``path:lineno: durable write not atomic: ...`` line
and the script exits 1. The fire path is pinned by
``tests/scripts/test_lint_atomic_durable_writes.py``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOT = REPO / "src"

# Subtrees that materialize fixtures / generated user code, never framework
# durable state — skipped whole (scan-root-relative posix prefixes).
_SKIP_PREFIXES: tuple[str, ...] = (
    "hpc_agent/conformance/",
    "hpc_agent/execution/mapreduce/templates/",
)

# Conventional variable names for a durable-artifact path. A truncating write to
# a bare name in this set is a durable write.
DURABLE_VARNAMES: frozenset[str] = frozenset(
    {
        "manifest_path",
        "settings_path",
        "config_path",
        "archive_path",
    }
)

# String-literal basenames that identify a framework durable artifact. A variable
# assigned from an expression containing one of these (``p / "experiment_meta.json"``)
# is durable-tainted for the rest of its function.
DURABLE_BASENAMES: frozenset[str] = frozenset(
    {
        "experiment_meta.json",
        "settings.json",
        ".claude.json",
        ".deploy_state.json",
    }
)

# Cited exemptions: scan-root-relative ``path::function`` of a durable-shaped
# write that is actually non-durable (a temp / regenerable cache). Empty by
# construction — add an entry only as a reviewed decision.
ALLOWLIST: frozenset[str] = frozenset()

_TRUNCATING_WRITE_ATTRS = frozenset({"write_text", "write_bytes"})


def _receiver_name(call: ast.Call) -> str | None:
    """Return the bare-Name receiver of ``<name>.write_text(...)``, else None."""
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return None


def _is_zipfile_write(call: ast.Call) -> str | None:
    """Return the bare-Name first arg of a ``ZipFile(<name>, "w"/"x")`` call.

    Read mode (default / ``"r"``) and append mode (``"a"``) are not truncating,
    so they return None. A non-literal or non-Name target also returns None.
    """
    func = call.func
    name = None
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    if name != "ZipFile":
        return None
    # mode is positional arg 1, or keyword ``mode=``.
    mode: str | None = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        val = call.args[1].value
        mode = val if isinstance(val, str) else None
    else:
        for kw in call.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                v = kw.value.value
                mode = v if isinstance(v, str) else None
    if mode is None or ("w" not in mode and "x" not in mode):
        return None
    if not call.args or not isinstance(call.args[0], ast.Name):
        return None
    return call.args[0].id


def _assignment_taints(node: ast.Assign) -> str | None:
    """Return the assigned bare-Name target iff the RHS embeds a durable-basename
    string literal (``p = p / "experiment_meta.json"``), else None."""
    for sub in ast.walk(node.value):
        if isinstance(sub, ast.Constant) and sub.value in DURABLE_BASENAMES:
            break
    else:
        return None
    # Only single bare-Name targets are tracked (``p = ...``); tuple / attribute
    # / subscript targets are out of the coarse rule's scope.
    if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    return None


class _Finding:
    __slots__ = ("lineno", "target", "kind")

    def __init__(self, lineno: int, target: str, kind: str) -> None:
        self.lineno = lineno
        self.target = target
        self.kind = kind


def _scan_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[_Finding]:
    """Return durable-write findings whose enclosing function is *node*.

    Taint and write calls are gathered over the function body but NOT into nested
    functions (each nested def is scanned as its own scope by the caller).
    """
    tainted: set[str] = set()
    writes: list[_Finding] = []

    def walk(n: ast.AST) -> None:
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # own scope
            if isinstance(child, ast.Assign):
                t = _assignment_taints(child)
                if t is not None:
                    tainted.add(t)
            if isinstance(child, ast.Call):
                is_wt = (
                    isinstance(child.func, ast.Attribute)
                    and child.func.attr in _TRUNCATING_WRITE_ATTRS
                )
                if is_wt:
                    assert isinstance(child.func, ast.Attribute)  # narrowed above
                    recv = _receiver_name(child)
                    if recv is not None:
                        writes.append(_Finding(child.lineno, recv, child.func.attr))
                else:
                    zt = _is_zipfile_write(child)
                    if zt is not None:
                        writes.append(_Finding(child.lineno, zt, "ZipFile"))
            walk(child)

    walk(node)
    return [w for w in writes if w.target in DURABLE_VARNAMES or w.target in tainted]


def _iter_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _relpath(path: Path, scan_root: Path) -> str:
    try:
        return path.resolve().relative_to(scan_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def lint_file(path: Path, scan_root: Path | None = None) -> list[tuple[int, str]]:
    """Return ``(lineno, message)`` per durable non-atomic write in *path*."""
    root = scan_root if scan_root is not None else SCAN_ROOT
    rel = _relpath(path, root)
    if any(rel.startswith(pre) for pre in _SKIP_PREFIXES):
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    findings: list[tuple[int, str]] = []
    for func in _iter_functions(tree):
        for w in _scan_function(func):
            key = f"{rel}::{func.name}"
            if key in ALLOWLIST:
                continue
            if w.kind == "ZipFile":
                how = (
                    f"ZipFile({w.target}, 'w') TRUNCATES the previously-sealed "
                    f"artifact on open; build it on a temp sibling via "
                    f"infra.io.atomic_replace_path and swap it in"
                )
            else:
                how = (
                    f"{w.target}.{w.kind}(...) truncates in place; route it through "
                    f"infra.io.atomic_write_json / atomic_write_text"
                )
            findings.append(
                (
                    w.lineno,
                    f"durable write not atomic: {how} (generator G12; "
                    f"add a cited ALLOWLIST entry {key!r} only if this target is "
                    f"a temp / regenerable cache).",
                )
            )
    findings.sort(key=lambda f: f[0])
    return findings


def iter_targets(scan_root: Path) -> list[Path]:
    pkg = scan_root / "hpc_agent"
    if not pkg.exists():
        return []
    return sorted(p for p in pkg.rglob("*.py") if p.is_file())


def main(scan_root: Path | None = None) -> int:
    root = scan_root if scan_root is not None else SCAN_ROOT
    failures = 0
    for path in iter_targets(root):
        for lineno, hint in lint_file(path, root):
            try:
                disp = path.resolve().relative_to(REPO).as_posix()
            except ValueError:
                disp = path.as_posix()
            print(f"{disp}:{lineno}: {hint}")
            failures += 1
    if failures:
        print(
            f"\n{failures} durable non-atomic write(s). A truncating write "
            f"destroys the previously-good durable file on a crash mid-write "
            f"(generator G12). Route it through infra.io.atomic_write_json / "
            f"atomic_write_text / atomic_replace_path, or add a cited ALLOWLIST "
            f"entry in scripts/lint_atomic_durable_writes.py for a temp / "
            f"regenerable-cache target.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
