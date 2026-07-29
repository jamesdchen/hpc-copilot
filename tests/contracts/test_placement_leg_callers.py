"""Caller census for the S1 placement leg — the fail-open guard's mechanized half.

The placement leg (run-queue plan §10.S1) is DELIBERATELY fail-open at
consumption: a caller that omits ``current_placement`` silently keeps the leg
off (absent-disables — the conservative rule that made the leg additive on the
pre-migration corpus). Fail-open guards have a known failure mode: a future
consent-consuming call site simply forgets the kwarg and the guard never fires
there, invisibly. Prose ("callers should pass the sidecar's cluster") cannot
catch that; this census can.

The rule: every call to a consent-consuming entry point in ``src/`` must
either pass ``current_placement`` explicitly or sit on the allowlist below
with a stated reason. Adding a new call site without deciding the placement
question fails this test — which is the point: the decision must be made,
in either direction, on purpose.

Since Phase 2 (§10.S1.4) the allowlist is EMPTY — the two campaign sites that
held it open now pass the campaign's cluster key — so this census is the live
enforcement of the whole leg's caller side, not a ledger of exceptions.
"""

from __future__ import annotations

import ast
from pathlib import Path

import hpc_agent

SRC_ROOT = Path(hpc_agent.__file__).resolve().parent

#: Entry points whose calls consume (or gate on) a standing consent.
CONSENT_ENTRY_POINTS = frozenset(
    {
        "standing_consent_status",
        "consume_boundary_under_consent",
        "assert_standing_consent",
        "assert_greenlit_or_consented",
    }
)

#: (relative path, enclosing function) -> why this site does not pass placement.
#: Every entry must name a real reason; "forgot" is not one.
#:
#: EMPTY since Phase 2 (§10.S1.4, D7): the two campaign sites that used to sit
#: here — ``campaign_watch`` and ``self_heal_campaign`` — now pass the campaign's
#: cluster key (``overnight.campaign_current_placement``), so every consent-
#: consuming call site in ``src/`` decides the placement question by wiring it.
#: The emptiness is the point: the census below is now a pure enforcement, with
#: no pre-approved hole a future omission could hide in. Re-adding an entry is
#: legitimate but must state a real reason, exactly as before.
ALLOWLIST: dict[tuple[str, str], str] = {}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _walk_file(
    node: ast.AST, rel: str, stack: list[str], found: list[tuple[str, str, str, bool]]
) -> None:
    """Stack-based walk with enclosing-function tracking, so a call site is
    reported against the def the fix would edit."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        stack.append(node.name)
        for child in ast.iter_child_nodes(node):
            _walk_file(child, rel, stack, found)
        stack.pop()
        return
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name in CONSENT_ENTRY_POINTS:
            enclosing = stack[-1] if stack else "<module>"
            # The entry points' own bodies forward the kwarg to each other; a
            # self-named enclosing def is the forwarding chain, not a consumer.
            if enclosing not in CONSENT_ENTRY_POINTS or name != enclosing:
                passes = any(kw.arg == "current_placement" for kw in node.keywords)
                found.append((rel, enclosing, name, passes))
    for child in ast.iter_child_nodes(node):
        _walk_file(child, rel, stack, found)


def _census() -> list[tuple[str, str, str, bool]]:
    """Every (rel_path, enclosing_function, entry_point, passes_placement) in src."""
    found: list[tuple[str, str, str, bool]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        _walk_file(tree, rel, [], found)
    return found


def test_every_consent_consuming_call_site_decides_the_placement_question() -> None:
    undecided = [
        (rel, enclosing, entry)
        for rel, enclosing, entry, passes in _census()
        if not passes and (rel, enclosing) not in ALLOWLIST
    ]
    assert not undecided, (
        "Consent-consuming call site(s) neither pass current_placement nor sit "
        "on the allowlist with a stated reason. The placement leg is fail-open "
        "(absent-disables), so an undecided omission means the S1 guard silently "
        "never fires at that boundary. Pass the sidecar's cluster "
        "(state/runs.read_run_cluster) or allowlist the site with its reason: "
        f"{undecided}"
    )


def test_census_still_sees_the_known_callers() -> None:
    """The census must not go blind: if a refactor renames the entry points or
    moves the callers, an empty census would make the test above pass
    vacuously. Pin the sites known today (wired and allowlisted alike)."""
    seen = {(rel, enclosing) for rel, enclosing, _, _ in _census()}
    for expected in [
        ("ops/block_gate.py", "assert_greenlit_or_consented"),
        ("ops/submit_blocks.py", "_submit_s3_impl"),
        ("_kernel/lifecycle/block_drive.py", "_consume_overnight"),
        ("meta/campaign/blocks.py", "campaign_watch"),
        ("ops/overnight.py", "self_heal_campaign"),
    ]:
        assert expected in seen, f"census no longer finds {expected} — did a refactor move it?"


def test_the_campaign_sites_pass_placement() -> None:
    """The Phase-2 wiring (§10.S1.4, D7), pinned POSITIVELY.

    ``test_every_consent_consuming_call_site_decides_the_placement_question``
    would also pass if these two sites were re-allowlisted, and the whole point
    of D7 is that they are not: a campaign consent binds a cluster SET and both
    campaign boundaries must compare against it. Pin the kwarg's presence at the
    two sites by name so re-allowlisting them (or dropping the kwarg in a
    refactor) fails here, not silently.
    """
    passing = {(rel, enclosing) for rel, enclosing, _, passes in _census() if passes}
    for expected in [
        ("meta/campaign/blocks.py", "campaign_watch"),
        ("ops/overnight.py", "self_heal_campaign"),
    ]:
        assert expected in passing, (
            f"{expected} no longer passes current_placement — the campaign half of "
            "the S1 placement leg (§10.S1.4) is the wiring this test exists to hold."
        )


def test_allowlist_entries_are_live() -> None:
    """A stale allowlist entry is a hole: it pre-approves a site that no longer
    exists, ready to silently cover a future omission at the same key."""
    seen = {(rel, enclosing) for rel, enclosing, _, _ in _census()}
    stale = [key for key in ALLOWLIST if key not in seen]
    assert not stale, f"allowlist entries with no matching call site: {stale}"
