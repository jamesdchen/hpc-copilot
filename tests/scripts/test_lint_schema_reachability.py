"""Tests for the schema-reachability lint (N8).

Pins these invariants (mirrors ``test_lint_telemetry_labels.py``):

1. The real tree passes — every ``schemas/*.json`` file is reachable from
   ``schema_for`` over the live registry, or carries an ALLOWLIST reason. This
   is the coupling test: it fails if a schema loses its owning primitive to a
   rename, or a new schema is baked without a consumer.
2. The lint can actually FIRE:
   * an unreachable file that is NOT on the allowlist (the core guard), and
   * a stale allowlist entry — both dangling (file gone) and redundant (the
     file became reachable).
3. The clean synthetic case passes (the fire cases are non-tautological).
4. The live ALLOWLIST is exactly the live unreachable set — no unused entries,
   no missing ones — so the shipped allowlist is minimal and complete.
"""

from __future__ import annotations

import importlib.util
import sys

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_schema_reachability", REPO_ROOT / "scripts" / "lint_schema_reachability.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_schema_reachability"] = lint
_SPEC.loader.exec_module(lint)


# --- 1. real tree is clean --------------------------------------------------


def test_real_tree_is_clean() -> None:
    """Every live schema file is schema_for-reachable or allowlisted."""
    assert lint.main() == 0


# --- 4. the live allowlist is minimal AND complete --------------------------


def test_allowlist_matches_live_unreachable_set() -> None:
    """The shipped ALLOWLIST equals the live unreachable set exactly.

    Complete (no unreachable file missing a reason) and minimal (no reason for
    a file that is reachable / gone). Stronger than ``main() == 0`` alone: it
    also fails if a NEW schema is unreachable OR an old allowlist entry rots.
    """
    all_files = lint._all_schema_files()
    reachable = lint._reachable_schema_files()
    unreachable = all_files - reachable
    assert set(lint.ALLOWLIST) == unreachable, (
        "ALLOWLIST drifted from the live unreachable set. "
        f"Unreachable but not allowlisted: {sorted(unreachable - set(lint.ALLOWLIST))}. "
        f"Allowlisted but reachable/gone: {sorted(set(lint.ALLOWLIST) - unreachable)}."
    )


# --- 3. clean synthetic case passes (fire cases are non-tautological) -------


def test_check_clean_case_passes() -> None:
    all_files = {"a.input.json", "b.output.json", "cross_cutting.json"}
    reachable = {"a.input.json", "b.output.json"}
    allowlist = {"cross_cutting.json": "documented cross-cutting shape"}
    assert lint.check(all_files, reachable, allowlist) == []


# --- 2a. unreachable & unlisted fires ---------------------------------------


def test_unreachable_unlisted_fires() -> None:
    all_files = {"a.input.json", "orphan.output.json"}
    reachable = {"a.input.json"}
    errors = lint.check(all_files, reachable, {})
    assert errors
    assert any("orphan.output.json" in e and "schema_for" in e for e in errors)


# --- 2b. dangling allowlist entry fires -------------------------------------


def test_dangling_allowlist_entry_fires() -> None:
    all_files = {"a.input.json"}
    reachable = {"a.input.json"}
    allowlist = {"removed.output.json": "was cross-cutting, since deleted"}
    errors = lint.check(all_files, reachable, allowlist)
    assert errors
    assert any("removed.output.json" in e and "does not exist" in e for e in errors)


# --- 2c. redundant allowlist entry fires ------------------------------------


def test_redundant_allowlist_entry_fires() -> None:
    # The file is now reachable (a primitive owns it) yet still allowlisted.
    all_files = {"now_owned.output.json"}
    reachable = {"now_owned.output.json"}
    allowlist = {"now_owned.output.json": "was cross-cutting, now a verb output"}
    errors = lint.check(all_files, reachable, allowlist)
    assert errors
    assert any("now_owned.output.json" in e and "redundant" in e for e in errors)
