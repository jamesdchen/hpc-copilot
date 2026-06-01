"""A float-tolerant, structural comparator for the agent surface's JSON results.

The behavioral eval harness (see ``tests/eval/README.md``) grades the
thing that varies in production: given a natural-language request + a
repo, does the agent resolve the *right* submit spec — right cluster,
right grid, right wave plan, sane resources? Unit/contract/property
tests already pin the *mechanics* of every primitive; what they cannot
catch is a prompt/skill edit that regresses **decision quality** while
every contract test stays green. This module is the grader for that gap.

WHY a bespoke comparator rather than ``assertEqual`` on the whole dict:

* **Decisions are structural, not textual.** We assert that the resolved
  ``cluster`` is ``hoffman2`` and that the grid expands to ``6`` points —
  not that the agent's prose matched a golden paragraph. So the grader
  walks the *result object* (nested dict / list / scalar) and compares it
  field by field, ignoring anything the gold does not mention.

* **Some fields must be exact, others only approximately right.** The
  cluster name and ``grid_points`` are correctness — a one-off there is a
  wrong decision. But a resolved ``walltime_sec`` or ``mem_mb`` is a
  *judgement within a band*: 14400s vs 14700s is the same decision, and
  pinning the exact second would make the suite reject good resolutions
  and rot on every planner-heuristic tweak. So the grader is exact by
  default but supports per-key **tolerant** matching: absolute/relative
  float tolerance, and ``[lo, hi]`` range bounds.

* **Subset, not equality, for dicts.** A gold ``expect`` block names only
  the fields a case cares about; the candidate (a real envelope) carries
  many more. Requiring full equality would couple every case to the
  entire envelope schema and break them all whenever an unrelated field
  is added. So a gold dict matches when every key it lists is present and
  matches — extra keys in the candidate are allowed. (Lists, by contrast,
  are compared element-by-element and length-checked: a grid of 6 axes is
  *not* the same decision as a grid of 5.)

The whole thing is deliberately tiny and stdlib-only — the design is
lifted from lara-hpc's ``recursive_compare`` (a ~15-line float-tolerant
structural diff), kept small on purpose so the grader itself is obviously
correct and never the flaky part of the suite.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# Default absolute / relative tolerances applied to a key only when it is
# named in ``tolerant`` without its own override. Chosen loose enough to
# absorb planner-heuristic jitter (a walltime trimmed by a few minutes, a
# mem buffer rounded) but tight enough that a *wrong* resource ask — off by
# an order of magnitude — still fails. A caller that needs a different band
# passes ``Tol(abs=..., rel=...)`` or ``Range(lo, hi)`` per key.
_DEFAULT_ABS_TOL = 1e-9
_DEFAULT_REL_TOL = 0.05  # 5% — a resolved resource within 5% is "the same decision"


@dataclass(frozen=True)
class Tol:
    """A float-tolerant match: candidate ≈ gold within ``abs`` or ``rel``.

    Mirrors :func:`math.isclose` semantics — the comparison passes when
    either the absolute difference is within ``abs`` OR the relative
    difference is within ``rel``. Use for a numeric field whose exact value
    is a heuristic output (resolved walltime/mem) where "close enough" is
    "the same decision".
    """

    abs: float = _DEFAULT_ABS_TOL
    rel: float = _DEFAULT_REL_TOL


@dataclass(frozen=True)
class Range:
    """An inclusive ``[lo, hi]`` band the candidate value must fall inside.

    Use when a decision is correct across a *window* rather than near a
    point — e.g. "the resolved walltime is somewhere between 1h and 6h" —
    so the case states the acceptable envelope directly instead of a
    centre + tolerance that only approximates it.
    """

    lo: float
    hi: float


@dataclass
class Mismatch:
    """One field where candidate diverged from gold. ``path`` is dotted."""

    path: str
    expected: Any
    actual: Any
    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        loc = self.path or "<root>"
        return f"{loc}: {self.reason} (expected={self.expected!r}, actual={self.actual!r})"


@dataclass
class CompareResult:
    """Outcome of a structural compare: the list of mismatches (empty == pass)."""

    mismatches: list[Mismatch] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def __bool__(self) -> bool:  # let callers write ``if result:``
        return self.ok

    def report(self) -> str:
        """A multi-line, human-readable diff for a failing assertion message."""
        if self.ok:
            return "match"
        return "structural mismatch:\n" + "\n".join(f"  - {m}" for m in self.mismatches)


def _is_number(value: Any) -> bool:
    # bool is an int subclass, but ``True``/``False`` are categorical
    # decisions (canary on/off), never tolerant numerics — compare them
    # by identity, not by float tolerance.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _compare(
    gold: Any,
    candidate: Any,
    *,
    path: str,
    tolerant: Mapping[str, Tol | Range],
    out: list[Mismatch],
) -> None:
    """Recurse over *gold*, recording every divergence from *candidate*.

    The walk is driven by the *gold* shape, never the candidate's:
    extra keys in a candidate dict are ignored (subset match), but every
    key/element the gold names must be present and must match. ``tolerant``
    is keyed by the LEAF key name (the last dotted segment) so a case can
    say ``{"walltime_sec": Tol(rel=0.2)}`` without spelling the full path.
    """
    # ── tolerant leaf override (range / float tol) ──────────────────────
    leaf = path.rsplit(".", 1)[-1] if path else path
    rule = tolerant.get(leaf)
    if rule is not None:
        if not _is_number(candidate):
            out.append(
                Mismatch(path, gold, candidate, f"expected a number for tolerant key {leaf!r}")
            )
            return
        if isinstance(rule, Range):
            if not (rule.lo <= candidate <= rule.hi):
                out.append(Mismatch(path, [rule.lo, rule.hi], candidate, "outside allowed range"))
            return
        # Tol: float-close to the gold centre.
        if not _is_number(gold):
            out.append(Mismatch(path, gold, candidate, "tolerant rule needs a numeric gold value"))
            return
        if not math.isclose(candidate, gold, rel_tol=rule.rel, abs_tol=rule.abs):
            out.append(
                Mismatch(path, gold, candidate, f"not within tol (rel={rule.rel}, abs={rule.abs})")
            )
        return

    # ── mappings: subset match (every gold key present + matching) ──────
    if isinstance(gold, Mapping):
        if not isinstance(candidate, Mapping):
            out.append(Mismatch(path, gold, candidate, "expected a mapping"))
            return
        for key, sub_gold in gold.items():
            if key not in candidate:
                out.append(Mismatch(_join(path, key), sub_gold, None, "missing key"))
                continue
            _compare(
                sub_gold,
                candidate[key],
                path=_join(path, key),
                tolerant=tolerant,
                out=out,
            )
        return

    # ── sequences: element-wise + length (str is not a sequence here) ───
    if isinstance(gold, Sequence) and not isinstance(gold, (str, bytes)):
        if isinstance(candidate, (str, bytes)) or not isinstance(candidate, Sequence):
            out.append(Mismatch(path, gold, candidate, "expected a sequence"))
            return
        if len(gold) != len(candidate):
            out.append(
                Mismatch(path, gold, candidate, f"length {len(candidate)} != expected {len(gold)}")
            )
            return
        for i, (g_item, c_item) in enumerate(zip(gold, candidate, strict=True)):
            _compare(g_item, c_item, path=f"{path}[{i}]", tolerant=tolerant, out=out)
        return

    # ── numeric scalars: default float-tolerant (absorbs 1.0 vs 1) ──────
    if _is_number(gold) and _is_number(candidate):
        if not math.isclose(candidate, gold, rel_tol=_DEFAULT_REL_TOL, abs_tol=_DEFAULT_ABS_TOL):
            # An un-listed numeric still gets a small default band so a
            # JSON int/float distinction (6 vs 6.0) never trips the suite;
            # a key that must be EXACT (grid_points) is asserted by the
            # case at a value where the 5% band is < 1, so off-by-one fails.
            out.append(Mismatch(path, gold, candidate, "numbers differ beyond default tolerance"))
        return

    # ── bools are categorical, never numeric ────────────────────────────
    # ``True == 1`` and ``False == 0`` in Python, so a bare ``gold !=
    # candidate`` would let a boolean decision (canary on/off) silently match
    # an int 1/0. Require the *types* to agree when either side is a bool:
    # ``True`` matches only ``True``, not ``1``.
    if isinstance(gold, bool) or isinstance(candidate, bool):
        if type(gold) is not type(candidate) or gold != candidate:
            out.append(Mismatch(path, gold, candidate, "values differ"))
        return

    # ── everything else: exact equality (strings, None, …) ──────────────
    if gold != candidate:
        out.append(Mismatch(path, gold, candidate, "values differ"))


def recursive_compare(
    gold: Any,
    candidate: Any,
    *,
    tolerant: Mapping[str, Tol | Range] | None = None,
) -> CompareResult:
    """Structurally compare *candidate* against *gold*; return the diff.

    Parameters
    ----------
    gold:
        The reference shape — a case's ``expect`` block, or a hand-written
        gold object. Drives the walk: only the fields it names are checked.
    candidate:
        The thing under test — a resolved submit spec or a parsed
        ``submit`` envelope. May carry extra keys; they are ignored.
    tolerant:
        Map of *leaf key name* → :class:`Tol` (float-close to the gold) or
        :class:`Range` (inclusive ``[lo, hi]``). Any key not listed is
        compared exactly (numbers get a small default band so ``6`` and
        ``6.0`` agree, but off-by-one still fails). This is the exact-where-
        it-must-be / tolerant-where-it-should-be knob: keep ``cluster`` and
        ``grid_points`` out of it; put ``walltime_sec`` / ``mem_mb`` in it.

    Returns
    -------
    :class:`CompareResult` — truthy (and ``.ok``) when every gold field
    matched; otherwise carries the list of :class:`Mismatch` records, each
    with a dotted path, for a readable assertion message.
    """
    out: list[Mismatch] = []
    _compare(gold, candidate, path="", tolerant=tolerant or {}, out=out)
    return CompareResult(out)
