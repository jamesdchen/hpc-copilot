"""Strict-xfail gate for ``docs/internals/regen-debt-ledger.md``.

The ledger consolidates every deferred "rebake at merge" so an unpaid regen
is visible in one place. This test mechanizes it (precedent:
``tests/contracts/test_recovery_registry.py``):

1. **Strict format parse.** ``parse_ledger`` requires the ``## Outstanding
   regen debt`` heading followed by a pipe table with the EXACT 5-column
   header. Any deviation (missing heading, wrong header, wrong cell count)
   is a hard failure — a format change can't silently disable the gate.
2. **Named-gate existence.** Every row's ``Live gate today`` cell must carry
   at least one backticked ``test_*`` / ``tests/….py`` reference (or the
   literal ``no live gate``), and each named reference must resolve under
   ``tests/`` — as a function definition OR a file stem (a renamed/deleted
   gate fails loudly).
3. **Strict-xpass punch list.** A row marked ``**RED**`` names a currently-
   failing gate: the live test runs it and ``xfail``s while it still fails
   (debt outstanding, suite stays green) but HARD FAILS the moment it passes
   ("debt paid — remove the row"). A ``no live gate`` row may NOT be
   ``**RED**`` (nothing to xfail — hard format error).

The pure helpers ``parse_ledger`` / ``check_row_format`` / ``check_red_row``
operate on strings so the fires-AND-passes pairs run on synthetic ledger text
with no pytest-in-pytest in the steady state (after reconciliation the live
table carries zero ``**RED**`` rows).
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LEDGER_PATH = _REPO_ROOT / "docs" / "internals" / "regen-debt-ledger.md"
_TESTS_ROOT = _REPO_ROOT / "tests"

_HEADING = "## Outstanding regen debt"
_EXPECTED_HEADER = (
    "Item",
    "Source drift log",
    "What is owed",
    "Live gate today",
    "Owner / wave",
)
_NO_LIVE_GATE = "no live gate"
_RED_TOKEN = "**RED**"

# A backticked token counts as a gate reference when it names a pytest target:
# a ``test_*`` function/file stem or a ``tests/….py`` path (optionally with a
# ``::node`` suffix).
_GATE_REF_RE = re.compile(r"`([^`]+)`")


class LedgerFormatError(AssertionError):
    """Raised on any deviation from the strict ledger table contract."""


# ── data model ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LedgerRow:
    cells: tuple[str, ...]

    @property
    def item(self) -> str:
        return self.cells[0]

    @property
    def live_gate(self) -> str:
        return self.cells[3]

    @property
    def is_red(self) -> bool:
        return _RED_TOKEN in self.live_gate

    @property
    def declares_no_live_gate(self) -> bool:
        return _NO_LIVE_GATE in self.live_gate

    @property
    def gate_refs(self) -> tuple[str, ...]:
        return _extract_gate_refs(self.live_gate)


# ── pure helpers ───────────────────────────────────────────────────────────


def _split_row(line: str) -> tuple[str, ...]:
    """Split a markdown table row into stripped cells."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        raise LedgerFormatError(f"not a table row: {line!r}")
    inner = stripped.strip("|")
    return tuple(cell.strip() for cell in inner.split("|"))


def _is_separator_row(cells: tuple[str, ...]) -> bool:
    return all(set(cell) <= {"-", ":"} and cell for cell in cells)


def parse_ledger(text: str) -> list[LedgerRow]:
    """Parse the outstanding-debt table; raise on ANY format deviation.

    Zero data rows is valid (header still asserted). The table is the FIRST
    pipe table after the ``## Outstanding regen debt`` heading.
    """
    lines = text.splitlines()
    # Locate the heading.
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == _HEADING)
    except StopIteration as exc:
        raise LedgerFormatError(f"missing heading {_HEADING!r}") from exc

    # Find the header row (first ``|`` line) — refuse if another ``##`` heading
    # intervenes (the table must belong to this section).
    header_idx: int | None = None
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("## "):
            break
        if stripped.startswith("|"):
            header_idx = i
            break
    if header_idx is None:
        raise LedgerFormatError(f"no pipe table follows {_HEADING!r}")

    header = _split_row(lines[header_idx])
    if header != _EXPECTED_HEADER:
        raise LedgerFormatError(f"table header {header!r} != expected {_EXPECTED_HEADER!r}")

    sep_idx = header_idx + 1
    if sep_idx >= len(lines) or not lines[sep_idx].strip().startswith("|"):
        raise LedgerFormatError("table header not followed by a separator row")
    sep = _split_row(lines[sep_idx])
    if len(sep) != len(_EXPECTED_HEADER) or not _is_separator_row(sep):
        raise LedgerFormatError(f"malformed separator row: {lines[sep_idx]!r}")

    rows: list[LedgerRow] = []
    for i in range(sep_idx + 1, len(lines)):
        if not lines[i].strip().startswith("|"):
            break
        cells = _split_row(lines[i])
        if len(cells) != len(_EXPECTED_HEADER):
            raise LedgerFormatError(
                f"row has {len(cells)} cells, expected {len(_EXPECTED_HEADER)}: {lines[i]!r}"
            )
        rows.append(LedgerRow(cells))
    return rows


def _extract_gate_refs(cell: str) -> tuple[str, ...]:
    """Backticked tokens in a cell that name a pytest target."""
    refs = []
    for tok in _GATE_REF_RE.findall(cell):
        if tok.startswith("tests/") or re.search(r"\btest_\w+", tok):
            refs.append(tok)
    return tuple(refs)


def check_row_format(row: LedgerRow) -> None:
    """Validate one row's ``Live gate today`` cell against the contract."""
    if row.declares_no_live_gate:
        if row.is_red:
            raise LedgerFormatError(
                f"{row.item!r}: a 'no live gate' row cannot be marked **RED** "
                "(there is nothing to xfail)"
            )
        return
    if not row.gate_refs:
        raise LedgerFormatError(
            f"{row.item!r}: 'Live gate today' cell names no backticked test "
            f"reference and is not '{_NO_LIVE_GATE}': {row.live_gate!r}"
        )
    if row.is_red and not any(_runnable_target(ref) for ref in row.gate_refs):
        raise LedgerFormatError(f"{row.item!r}: **RED** row names no runnable gate to xfail")


def _runnable_target(ref: str, tests_root: Path = _TESTS_ROOT) -> str | None:
    """The runnable pytest target for a ref, or None if it is not runnable.

    A path (``tests/….py`` with optional ``::node``) whose file exists is
    runnable directly; a bare ``test_*`` name is runnable as a ``-k`` selection
    ONLY when it resolves to a function *definition* — a bare file stem is a
    valid *existence* reference (``gate_ref_resolves``) but not ``-k``-runnable,
    so RED rows require a path or a function.
    """
    path_part = ref.split("::", 1)[0]
    if "/" in path_part:
        return ref if (_REPO_ROOT / path_part).is_file() else None
    if not re.fullmatch(r"test_\w+", path_part):
        return None
    needle = f"def {path_part}("
    if any(needle in f.read_text(encoding="utf-8") for f in tests_root.rglob("test_*.py")):
        return path_part
    return None


def gate_ref_resolves(ref: str, tests_root: Path = _TESTS_ROOT) -> bool:
    """A gate reference resolves as a path, a function definition, or a file stem."""
    path_part = ref.split("::", 1)[0]
    if "/" in path_part:
        return (_REPO_ROOT / path_part).is_file()
    stem = path_part[:-3] if path_part.endswith(".py") else path_part
    py_files = list(tests_root.rglob("test_*.py"))
    if any(f.stem == stem for f in py_files):  # file-stem reference
        return True
    needle = f"def {stem}("  # function definition
    return any(needle in f.read_text(encoding="utf-8") for f in py_files)


def check_red_row(row: LedgerRow, run_gate) -> None:
    """Strict-xpass semantics for a **RED** row.

    ``run_gate(target) -> bool`` reports whether the named gate PASSES. A
    still-failing gate returns cleanly (caller xfails: debt outstanding); a
    now-passing gate HARD FAILS ("debt paid — remove the row").
    """
    target = next((_runnable_target(r) for r in row.gate_refs if _runnable_target(r)), None)
    if target is None:
        raise LedgerFormatError(f"{row.item!r}: **RED** row has no runnable gate")
    if run_gate(target):
        raise LedgerFormatError(
            f"{row.item!r}: named gate {target!r} now PASSES — debt paid, "
            "remove the row from the outstanding table"
        )


def _run_gate_subprocess(target: str) -> bool:
    """Run a ledger row's named gate; True iff pytest reports it passing."""
    # A path (optionally with ::node) runs directly; a bare function name is a
    # ``-k`` selection over tests/.
    args = [target] if "/" in target else ["tests", "-k", target]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", *args],
        cwd=_REPO_ROOT,
        timeout=600,
        capture_output=True,
    )
    return proc.returncode == 0


# ── the live gate ──────────────────────────────────────────────────────────


def test_live_ledger_is_well_formed_and_paid() -> None:
    """The real ledger parses, every row is contract-shaped, its named gates
    resolve, and every **RED** row is evaluated (strict-xpass)."""
    text = _LEDGER_PATH.read_text(encoding="utf-8")
    rows = parse_ledger(text)  # asserts heading + 5-column header even at zero rows
    for row in rows:
        check_row_format(row)
        for ref in row.gate_refs:
            assert gate_ref_resolves(ref), (
                f"{row.item!r}: gate reference {ref!r} does not resolve under tests/"
            )
        if row.is_red:
            check_red_row(row, _run_gate_subprocess)  # raises if debt is paid
            pytest.xfail(f"{row.item}: regen debt outstanding (gate still red)")


# ── fires-AND-passes pairs on synthetic ledger text ─────────────────────────


def _table(*data_rows: str) -> str:
    header = "| " + " | ".join(_EXPECTED_HEADER) + " |"
    sep = "|" + "|".join(["---"] * len(_EXPECTED_HEADER)) + "|"
    body = "\n".join(data_rows)
    return f"# Ledger\n\n{_HEADING}\n\n{header}\n{sep}\n{body}\n"


def test_clean_empty_table_passes() -> None:
    assert parse_ledger(_table()) == []


def test_clean_row_with_gate_ref_parses() -> None:
    rows = parse_ledger(_table("| X | d.md | owed | `test_spec_verb_inventory_matches_cli` | w |"))
    assert len(rows) == 1
    check_row_format(rows[0])  # no raise
    assert not rows[0].is_red


def test_missing_heading_hard_fails() -> None:
    with pytest.raises(LedgerFormatError):
        parse_ledger("# Ledger\n\nno table here\n")


def test_wrong_header_hard_fails() -> None:
    bad = f"{_HEADING}\n\n| Item | What | Gate |\n|---|---|---|\n"
    with pytest.raises(LedgerFormatError):
        parse_ledger(bad)


def test_wrong_cell_count_hard_fails() -> None:
    with pytest.raises(LedgerFormatError):
        parse_ledger(_table("| only | three | cells |"))


def test_prose_only_gate_cell_rejected() -> None:
    rows = parse_ledger(_table("| X | d.md | owed | readers tolerant | w |"))
    with pytest.raises(LedgerFormatError):
        check_row_format(rows[0])


def test_no_live_gate_literal_accepted() -> None:
    rows = parse_ledger(_table("| X | d.md | owed | no live gate | w |"))
    check_row_format(rows[0])  # no raise


def test_no_live_gate_marked_red_rejected() -> None:
    rows = parse_ledger(_table("| X | d.md | owed | no live gate **RED** | w |"))
    with pytest.raises(LedgerFormatError):
        check_row_format(rows[0])


def test_red_row_without_runnable_ref_rejected() -> None:
    # A bare file-stem ref is not runnable; **RED** needs a function/path.
    rows = parse_ledger(
        _table("| X | d.md | owed | `test_lint_primitive_doc_templates` **RED** | w |")
    )
    with pytest.raises(LedgerFormatError):
        check_row_format(rows[0])


def test_red_claimed_but_gate_green_hard_fails() -> None:
    rows = parse_ledger(
        _table("| X | d.md | owed | `test_spec_verb_inventory_matches_cli` **RED** | w |")
    )
    with pytest.raises(LedgerFormatError):
        check_red_row(rows[0], run_gate=lambda _target: True)  # gate passes -> debt paid


def test_outstanding_red_row_stays_green() -> None:
    rows = parse_ledger(
        _table("| X | d.md | owed | `test_spec_verb_inventory_matches_cli` **RED** | w |")
    )
    check_red_row(rows[0], run_gate=lambda _target: False)  # still failing -> no raise


def test_gate_ref_resolution_function_stem_and_path() -> None:
    assert gate_ref_resolves("test_spec_verb_inventory_matches_cli")  # function def
    assert gate_ref_resolves("test_lint_primitive_doc_templates")  # file stem
    assert gate_ref_resolves(
        "tests/contracts/test_primitive_remediation.py::test_spec_verb_inventory_matches_cli"
    )
    assert not gate_ref_resolves("test_this_gate_does_not_exist_anywhere")
