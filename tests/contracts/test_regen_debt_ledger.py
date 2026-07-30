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

EVERY ROW IS EVALUATED INDEPENDENTLY (collect-then-assert). The obvious
per-row loop is WRONG here and was the shape this file shipped with: raising
``pytest.xfail`` inside the loop ends the whole test at the FIRST outstanding
row, so a later row's "debt paid — remove the row" hard failure could never
fire while any earlier row was still red — the punch list silently stopped
checking after its first entry. :func:`evaluate_ledger_rows` therefore walks
ALL rows, collecting each one's verdict (format error / outstanding / paid)
without ever short-circuiting; :func:`assert_debt_ledger_clean` hard-fails on
the collected format errors and paid rows; and only then, with nothing left to
mask, does the live test ``xfail`` for the genuinely-outstanding remainder.

The pure helpers ``parse_ledger`` / ``check_row_format`` / ``check_red_row`` /
``evaluate_ledger_rows`` / ``assert_debt_ledger_clean`` operate on strings +
an injected ``run_gate`` so the fires-AND-passes pairs run on synthetic ledger
text with no pytest-in-pytest in the steady state (after reconciliation the
live table carries zero ``**RED**`` rows) — including the two-row fixture that
proves the independence property above.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Sequence
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


class DebtPaidError(LedgerFormatError):
    """A ``**RED**`` row's named gate now PASSES — the row must be removed.

    A distinct type (not a bare :class:`LedgerFormatError`) so
    :func:`evaluate_ledger_rows` can tell "this row's debt is paid" apart from
    "this row is malformed" without matching on message text. It SUBCLASSES
    ``LedgerFormatError`` so every existing ``pytest.raises(LedgerFormatError)``
    caller keeps catching it.
    """


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
        raise DebtPaidError(
            f"{row.item!r}: named gate {target!r} now PASSES — debt paid, "
            "remove the row from the outstanding table"
        )


@dataclass(frozen=True)
class LedgerVerdict:
    """The outcome of evaluating EVERY row — nothing short-circuited.

    ``errors`` are per-row contract violations (bad ``Live gate today`` cell, a
    gate reference that no longer resolves, a ``**RED**`` row naming no runnable
    target); ``paid`` names the rows whose gate now passes (hard failure — the
    row must be deleted); ``outstanding`` names the rows whose gate is still red
    (real debt — the live test ``xfail``s on these, and ONLY these).
    """

    errors: tuple[str, ...]
    outstanding: tuple[str, ...]
    paid: tuple[str, ...]


def evaluate_ledger_rows(
    rows: Sequence[LedgerRow],
    run_gate: Callable[[str], bool],
    *,
    ref_resolves: Callable[[str], bool] = gate_ref_resolves,
) -> LedgerVerdict:
    """Evaluate every row's gate INDEPENDENTLY; never short-circuit.

    ``run_gate(target) -> bool`` reports whether a named gate PASSES (the live
    caller passes :func:`_run_gate_subprocess`; the fixtures pass a stub).

    One row's outcome must never decide another's: a malformed row is recorded
    and the walk continues, and an outstanding (still-red) row is recorded
    WITHOUT raising, so a later row's paid debt is still reached. That is the
    whole point of this function — the loop it replaces ``xfail``ed in place and
    stopped the test dead at the first red row.

    A row that fails its format/resolution check is NOT then run as a gate: a
    row whose cell is broken has no trustworthy target, and running one anyway
    would report a second, derived failure for the same defect.
    """
    errors: list[str] = []
    outstanding: list[str] = []
    paid: list[str] = []
    for row in rows:
        try:
            check_row_format(row)
        except LedgerFormatError as exc:
            errors.append(str(exc))
            continue
        unresolved = [ref for ref in row.gate_refs if not ref_resolves(ref)]
        if unresolved:
            errors.append(
                f"{row.item!r}: gate reference(s) {', '.join(repr(r) for r in unresolved)} "
                "do not resolve under tests/"
            )
            continue
        if not row.is_red:
            continue
        try:
            check_red_row(row, run_gate)
        except DebtPaidError:
            paid.append(row.item)
        except LedgerFormatError as exc:
            errors.append(str(exc))
        else:
            outstanding.append(row.item)
    return LedgerVerdict(tuple(errors), tuple(outstanding), tuple(paid))


def assert_debt_ledger_clean(verdict: LedgerVerdict) -> None:
    """Hard-fail on any collected format error or any PAID row.

    The ONE assertion the live gate makes, factored out so the two-row fixture
    can prove it fires on the SECOND row while the first is still red. Both
    lists are reported whole — a ledger with three paid rows names three, not
    the first one it tripped over.
    """
    if verdict.errors:
        raise AssertionError(
            "regen-debt ledger row contract violations:\n  - " + "\n  - ".join(verdict.errors)
        )
    if verdict.paid:
        raise AssertionError(
            "debt paid — remove these rows from the outstanding table: "
            + ", ".join(repr(item) for item in verdict.paid)
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
    resolve, and EVERY **RED** row is evaluated (strict-xpass).

    Collect-then-assert: all rows are evaluated first, the hard failures are
    asserted next, and the ``xfail`` for outstanding debt comes LAST — so an
    outstanding row can no longer end the test before a later row's paid debt
    is checked.
    """
    text = _LEDGER_PATH.read_text(encoding="utf-8")
    rows = parse_ledger(text)  # asserts heading + 5-column header even at zero rows
    verdict = evaluate_ledger_rows(rows, _run_gate_subprocess)
    assert_debt_ledger_clean(verdict)
    if verdict.outstanding:
        pytest.xfail("regen debt outstanding (gates still red): " + ", ".join(verdict.outstanding))


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


# ── the independence property (the two-row fixture) ─────────────────────────
#
# The regression this file's restructure closes: a per-row loop that ``xfail``ed
# in place stopped at the FIRST red row, so no later row's "debt paid" hard
# failure could ever fire. These fixtures pin the property directly.

_RED_GATE = "test_spec_verb_inventory_matches_cli"  # a real, runnable function
_PAID_GATE = "test_gate_ref_resolution_function_stem_and_path"  # ditto


def _two_rows(first_gate: str, second_gate: str):
    return parse_ledger(
        _table(
            f"| first | d.md | owed | `{first_gate}` **RED** | w |",
            f"| second | d.md | owed | `{second_gate}` **RED** | w |",
        )
    )


def test_red_first_row_does_not_mask_a_paid_second_row() -> None:
    """Row 1 still red + row 2 paid → row 2 is REACHED and reported paid."""
    rows = _two_rows(_RED_GATE, _PAID_GATE)
    verdict = evaluate_ledger_rows(rows, run_gate=lambda target: target == _PAID_GATE)
    assert verdict.outstanding == ("first",)
    assert verdict.paid == ("second",)
    assert verdict.errors == ()


def test_paid_second_row_hard_fails_behind_a_red_first_row() -> None:
    """…and the live gate's own assertion HARD FAILS on it (not an xfail)."""
    rows = _two_rows(_RED_GATE, _PAID_GATE)
    verdict = evaluate_ledger_rows(rows, run_gate=lambda target: target == _PAID_GATE)
    with pytest.raises(AssertionError, match="'second'"):
        assert_debt_ledger_clean(verdict)


def test_two_outstanding_rows_stay_green_and_are_both_reported() -> None:
    """The passing half of the pair: nothing paid → no raise, both rows named."""
    rows = _two_rows(_RED_GATE, _PAID_GATE)
    verdict = evaluate_ledger_rows(rows, run_gate=lambda _target: False)
    assert verdict.outstanding == ("first", "second")
    assert verdict.paid == ()
    assert_debt_ledger_clean(verdict)  # no raise


def test_a_malformed_first_row_does_not_mask_a_paid_second_row() -> None:
    """A format error on row 1 also must not short-circuit row 2's gate."""
    rows = parse_ledger(
        _table(
            "| first | d.md | owed | readers tolerant | w |",  # no gate ref at all
            f"| second | d.md | owed | `{_PAID_GATE}` **RED** | w |",
        )
    )
    verdict = evaluate_ledger_rows(rows, run_gate=lambda _target: True)
    assert len(verdict.errors) == 1
    assert "'first'" in verdict.errors[0]
    assert verdict.paid == ("second",)


def test_unresolvable_gate_ref_is_collected_not_raised() -> None:
    """A vanished gate is a per-row error, and the walk continues past it."""
    rows = parse_ledger(
        _table(
            "| first | d.md | owed | `test_this_gate_does_not_exist_anywhere` | w |",
            f"| second | d.md | owed | `{_PAID_GATE}` **RED** | w |",
        )
    )
    verdict = evaluate_ledger_rows(rows, run_gate=lambda _target: True)
    assert len(verdict.errors) == 1
    assert "does not resolve" in verdict.errors[0] or "do not resolve" in verdict.errors[0]
    assert verdict.paid == ("second",)


def test_clean_ledger_verdict_is_empty_on_all_three_axes() -> None:
    rows = parse_ledger(_table(f"| X | d.md | owed | `{_RED_GATE}` | w |"))
    verdict = evaluate_ledger_rows(rows, run_gate=_never_called)
    assert verdict == LedgerVerdict((), (), ())
    assert_debt_ledger_clean(verdict)


def _never_called(target: str) -> bool:
    raise AssertionError(f"a non-RED row must never run its gate (ran {target!r})")


def test_gate_ref_resolution_function_stem_and_path() -> None:
    assert gate_ref_resolves("test_spec_verb_inventory_matches_cli")  # function def
    assert gate_ref_resolves("test_lint_primitive_doc_templates")  # file stem
    assert gate_ref_resolves(
        "tests/contracts/test_primitive_remediation.py::test_spec_verb_inventory_matches_cli"
    )
    assert not gate_ref_resolves("test_this_gate_does_not_exist_anywhere")
