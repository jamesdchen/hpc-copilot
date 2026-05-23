"""CI lint: text-mode file I/O must pass ``encoding=...`` explicitly.

The repo has regressed 25+ times on cp1252 round-trips because Python's
default text encoding is platform-dependent. This script walks the source
trees with AST (so docstrings / quoted samples don't false-trigger) and
flags any of the following calls in text mode that omit ``encoding=...``:

* builtin ``open(...)``
* ``Path(...).open(...)`` (pathlib's ``open`` — narrow match on ``Path(...)``)
* ``Path(...).read_text(...)`` / ``.read_text(...)``
* ``Path(...).write_text(...)`` / ``.write_text(...)``
* ``subprocess.run(..., text=True, ...)`` (only when ``text=True``)

Binary-mode opens (``"rb"``, ``"wb"``, ``"ab"`` etc.) are skipped because
``encoding=`` is meaningless there.

Per-line opt-out: append a ``noqa`` comment tagged ``TIO001`` to the
call's line for the rare legitimate case (e.g. caller already detected
the content-type).

Exits 1 with one ``path:line: <hint>`` per finding; exits 0 when clean.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Source trees that ship to users — every text I/O here must be explicit.
SCAN_GLOBS: tuple[tuple[str, str], ...] = (
    ("src/hpc_agent", "**/*.py"),
    ("hpc-agent-pro/src", "**/*.py"),
    ("scripts", "*.py"),
)

NOQA_TAG = "TIO001"

# File-IO call names (builtin ``open`` plus pathlib text-mode methods).
_FILE_IO_NAMES = {"open", "read_text", "write_text"}


# Mode strings that mean *binary* — ``encoding=`` is meaningless and the
# kwarg would actually raise. ``"r"``/``"w"``/``"a"`` + optional ``"t"``,
# ``"+"``, ``"x"`` are text; presence of ``"b"`` flips to binary.
def _is_binary_mode(mode: str) -> bool:
    return "b" in mode


def _has_encoding_kwarg(call: ast.Call) -> bool:
    return any(kw.arg == "encoding" for kw in call.keywords)


def _mode_arg(call: ast.Call, mode_pos: int) -> str | None:
    """Return the literal mode string, or None if non-literal / absent."""
    # Positional
    if len(call.args) > mode_pos:
        node = call.args[mode_pos]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None  # non-literal: be conservative, assume text
    # Keyword
    for kw in call.keywords:
        if kw.arg == "mode":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
            return None
    return None


def _has_text_true(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "text":
            return isinstance(kw.value, ast.Constant) and kw.value.value is True
    return False


def _call_name(call: ast.Call) -> str | None:
    """Return ``open`` / ``read_text`` / ``write_text`` / ``subprocess.run``
    for matchable calls, else None."""
    func = call.func
    if isinstance(func, ast.Name):
        if func.id == "open":
            return "open"
        return None
    if isinstance(func, ast.Attribute):
        if func.attr in _FILE_IO_NAMES:
            return func.attr
        # Match ``subprocess.run``-shaped calls. Be conservative: only
        # fire when the receiver is literally the ``subprocess`` name,
        # since arbitrary ``*.run(...)`` (e.g. flow runners) would
        # otherwise false-trigger.
        if (
            func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ):
            return "subprocess.run"
        return None
    return None


def _line_has_noqa(source_lines: list[str], lineno: int) -> bool:
    """Check the call's line for the noqa tag."""
    # Inline ``noqa`` tagged ``TIO001`` — must appear inside a comment on
    # the call's start line. (Multi-line calls: we only check the start
    # line for simplicity; if needed, callers can break the call so the
    # comment lives on the same line as the call name.)
    idx = lineno - 1
    if 0 <= idx < len(source_lines) and NOQA_TAG in source_lines[idx]:
        # Be precise: tag must live inside a comment, not bare prose.
        line = source_lines[idx]
        comment_at = line.find("#")
        if comment_at >= 0 and NOQA_TAG in line[comment_at:]:
            return True
    return False


def _check_call(call: ast.Call, name: str) -> str | None:
    """Return a hint string if the call is a violation, else None."""
    if name == "subprocess.run":
        if _has_text_true(call) and not _has_encoding_kwarg(call):
            return "subprocess.run(text=True, ...) without encoding=...; pass encoding='utf-8'"
        return None
    # File I/O: open(), .open(), .read_text(), .write_text()
    if name == "open":
        mode = _mode_arg(call, mode_pos=1)
    elif name in {"read_text", "write_text"}:
        # ``Path.read_text(encoding=..., errors=...)`` — no mode arg.
        mode = None
    else:
        return None
    if mode is not None and _is_binary_mode(mode):
        return None
    if _has_encoding_kwarg(call):
        return None
    if name == "open":
        return "open(...) without encoding=...; pass encoding='utf-8' (or open in binary mode)"
    return f"{name}(...) without encoding=...; pass encoding='utf-8'"


def _check_path_open(call: ast.Call) -> str | None:
    """``Path(...).open(...)`` is a separate path — pathlib's ``Path.open``.

    We can't always tell ``Path(p).open()`` from ``socket.open()`` from
    static AST, so we restrict the match: receiver must be a literal
    ``Path(...)`` call. ``.open()`` on bare names is common and ambiguous
    — they're caught transitively via review and the ``read_text`` /
    ``write_text`` cousins, not this lint.
    """
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "open":
        return None
    recv = func.value
    if not (
        isinstance(recv, ast.Call)
        and isinstance(recv.func, ast.Name)
        and recv.func.id == "Path"
    ):
        return None
    mode = _mode_arg(call, mode_pos=0)
    if mode is not None and _is_binary_mode(mode):
        return None
    if _has_encoding_kwarg(call):
        return None
    return "Path(...).open(...) without encoding=...; pass encoding='utf-8'"


def lint_file(path: Path) -> list[tuple[int, str]]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    source_lines = source.splitlines()
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Path(...).open(...) — handled separately so the receiver check
        # is explicit.
        hint = _check_path_open(node)
        if hint is None:
            name = _call_name(node)
            if name is None:
                continue
            # ``open`` as an Attribute (``foo.open(...)``) is already
            # routed through ``_check_path_open``; don't double-fire.
            if name == "open" and isinstance(node.func, ast.Attribute):
                continue
            hint = _check_call(node, name)
        if hint is None:
            continue
        if _line_has_noqa(source_lines, node.lineno):
            continue
        findings.append((node.lineno, hint))
    return findings


def iter_targets() -> list[Path]:
    targets: list[Path] = []
    for root, pattern in SCAN_GLOBS:
        root_path = REPO / root
        if not root_path.exists():
            continue
        for p in root_path.glob(pattern):
            if not p.is_file():
                continue
            # Skip the lint script itself (its docstring shows example
            # calls without encoding= as the very thing it forbids).
            if p.resolve() == Path(__file__).resolve():
                continue
            targets.append(p)
    return targets


def main() -> int:
    failures = 0
    for path in iter_targets():
        for lineno, hint in lint_file(path):
            rel = path.resolve().relative_to(REPO)
            print(f"{rel}:{lineno}: {hint}")
            failures += 1
    if failures:
        print(
            f"\n{failures} text-I/O call(s) without explicit encoding=. "
            f"Pass encoding='utf-8' (or # noqa: {NOQA_TAG} with a justification).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
