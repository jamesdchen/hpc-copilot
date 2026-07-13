"""Tests for the mirror-ledger lint (N7: sync convention -> checked contract).

Pins these invariants (mirrors ``test_lint_telemetry_labels.py``):

1. The real tree passes — every mirror comment is either promoted to a
   structured ``# MIRROR: <twin> pinned-by <test>`` annotation or deferred via
   ``scripts/mirror_ledger_allowlist.txt``. This is the coupling test: it fails
   if a new un-annotated mirror comment lands in a non-allowlisted file.
2. The lint can actually FIRE:
   * a mirror comment in a NON-allowlisted file without a nearby annotation,
   * a mirror phrase inside a docstring (not just a ``#`` comment),
   * a STALE allowlist entry (listed file with no mirror comment left).
3. A well-formed annotation nearby SATISFIES the comment (the fire is
   non-tautological), and allowlisting the file also satisfies it.
4. A malformed annotation (missing ``pinned-by`` half) does NOT satisfy.
5. A ``mirrors`` token in code / a runtime message string is NOT a mirror
   comment (scope is comments + docstrings only).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_mirror_ledger", REPO_ROOT / "scripts" / "lint_mirror_ledger.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_mirror_ledger"] = lint
_SPEC.loader.exec_module(lint)


# --- synthetic-tree helpers -------------------------------------------------


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _write_source(root: Path, rel: str, body: str) -> Path:
    """Write a python module under ``src/hpc_agent/`` of a synthetic repo."""
    return _write(root, f"src/hpc_agent/{rel}", body)


def _write_allowlist(root: Path, *entries: str) -> None:
    text = "# synthetic allowlist\n" + "\n".join(entries) + "\n"
    _write(root, "scripts/mirror_ledger_allowlist.txt", text)


# --- 1. real tree is clean --------------------------------------------------


def test_real_tree_is_clean() -> None:
    """Every mirror comment is annotated or deferred today."""
    assert lint.main() == 0


# --- 2a. un-annotated mirror comment in a non-allowlisted file fires ---------


def test_unannotated_comment_fires(tmp_path: Path, capsys) -> None:
    _write_source(tmp_path, "widget.py", "# this block mirrors the sibling copy\nX = 1\n")
    _write_allowlist(tmp_path)  # empty
    assert lint.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "widget.py" in out
    assert "MIRROR" in out


# --- 2b. mirror phrase inside a docstring fires -----------------------------


def test_docstring_phrase_fires(tmp_path: Path, capsys) -> None:
    body = '"""Helper.\n\nKept in sync with the other loader.\n"""\n\nX = 1\n'
    _write_source(tmp_path, "loader.py", body)
    _write_allowlist(tmp_path)
    assert lint.main(tmp_path) == 1
    assert "loader.py" in capsys.readouterr().out


# --- 3a. a nearby well-formed annotation satisfies (non-tautological) --------


def test_nearby_annotation_satisfies(tmp_path: Path) -> None:
    body = (
        "# this block mirrors the sibling copy\n"
        "# MIRROR: pkg.other::THING pinned-by tests/test_other.py::test_thing\n"
        "X = 1\n"
    )
    _write_source(tmp_path, "widget.py", body)
    _write_allowlist(tmp_path)
    assert lint.main(tmp_path) == 0


# --- 3b. allowlisting the file also satisfies -------------------------------


def test_allowlisted_file_satisfies(tmp_path: Path) -> None:
    _write_source(tmp_path, "widget.py", "# this block mirrors the sibling copy\nX = 1\n")
    _write_allowlist(tmp_path, "src/hpc_agent/widget.py")
    assert lint.main(tmp_path) == 0


# --- 4. a malformed annotation (no pinned-by) does NOT satisfy --------------


def test_malformed_annotation_does_not_satisfy(tmp_path: Path, capsys) -> None:
    body = (
        "# this block mirrors the sibling copy\n# MIRROR: pkg.other::THING (no test named)\nX = 1\n"
    )
    _write_source(tmp_path, "widget.py", body)
    _write_allowlist(tmp_path)
    assert lint.main(tmp_path) == 1
    assert "widget.py" in capsys.readouterr().out


# --- 2c. a stale allowlist entry fires --------------------------------------


def test_stale_allowlist_entry_fires(tmp_path: Path, capsys) -> None:
    # File exists but no longer has any mirror comment — the entry is stale.
    _write_source(tmp_path, "clean.py", "X = 1  # nothing to see here\n")
    _write_allowlist(tmp_path, "src/hpc_agent/clean.py")
    assert lint.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "stale" in out
    assert "clean.py" in out


def test_stale_allowlist_entry_missing_file_fires(tmp_path: Path, capsys) -> None:
    _write_source(tmp_path, "real.py", "X = 1\n")  # some src so the tree exists
    _write_allowlist(tmp_path, "src/hpc_agent/gone.py")
    assert lint.main(tmp_path) == 1
    assert "stale" in capsys.readouterr().out


# --- 5. code / message-string uses of "mirrors" are out of scope ------------


def test_code_and_message_string_not_flagged(tmp_path: Path) -> None:
    # ``mirrors`` as an identifier and inside a runtime message string are NOT
    # documentation mirror comments — only comments + docstrings are in scope.
    body = 'mirrors = 3\n\n\ndef f():\n    return _err(message="mirrors the run sidecar")\n'
    _write_source(tmp_path, "runtime.py", body)
    _write_allowlist(tmp_path)
    assert lint.main(tmp_path) == 0
