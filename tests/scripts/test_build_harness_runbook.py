"""Fire-path tests for ``scripts/build_harness_runbook.py``.

``docs/generated/harness-runbook.md`` is the prose-neutral harness runbook —
a GENERATED projection of the code-homed procedure (``_wire/spawn_contract.py::
DECISION_POINTS`` + the ``infra/block_chain`` sequence/consent tables) for a
NON-Claude harness (anti-vendor-lockout T5/R5). These tests pin the generator's
contract:

* the committed runbook is UP TO DATE (``--check`` green) — it equals a fresh
  regen;
* the round-trip fires: a hand-edit of the generated doc turns ``--check`` RED,
  and ``--write`` heals it back to green;
* COMPLETENESS — the projection covers EVERY workflow in ``DECISION_POINTS``, so
  a workflow added to the contract without a regen drifts (``--check`` red);
* the output carries NO Claude-idiom token (the ``CLAUDE_IDIOM_DENYLIST`` — the
  §1 prose-saturation inventory: ``run_in_background`` / ``AskUserQuestion`` /
  ``CronCreate`` / ``tool call`` / …).

The fire path runs against a throwaway copy so it never mutates the tree.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "build_harness_runbook.py"
_RUNBOOK = _REPO_ROOT / "docs" / "generated" / "harness-runbook.md"


def _load_module():
    spec = importlib.util.spec_from_file_location("_build_harness_runbook_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MOD = _load_module()


# ── real-tree state ────────────────────────────────────────────────────────


def test_real_tree_runbook_is_up_to_date() -> None:
    """The committed runbook equals a fresh regen (CI-parity for the regen step)."""
    assert _MOD.build(write=False) == 0


def test_bare_invocation_refused() -> None:
    assert _MOD.main([]) == 2


def test_both_modes_refused() -> None:
    assert _MOD.main(["--check", "--write"]) == 2


# ── completeness pin ───────────────────────────────────────────────────────


def test_projection_covers_every_workflow_in_decision_points() -> None:
    """Every ``DECISION_POINTS`` workflow appears as a section header, and every
    decision-point id + backing verb it declares is projected."""
    rendered = _MOD.render_runbook()
    for workflow, points in _MOD.DECISION_POINTS.items():
        assert f"## `{workflow}`" in rendered, workflow
        for point in points:
            assert f"`{point.id}`" in rendered, (workflow, point.id)
            if point.primitive:
                assert f"`{point.primitive}`" in rendered, (workflow, point.primitive)


def test_new_workflow_without_regen_would_drift() -> None:
    """A workflow added to DECISION_POINTS but not regenerated drifts the doc —
    the completeness contract that makes ``--check`` catch a stale runbook.

    Proven by construction: augmenting the contract changes the rendered bytes
    and introduces a header the committed file cannot contain, so ``--check``
    (committed vs fresh render) necessarily goes red."""
    base = _MOD.render_runbook()
    dp = _MOD.DecisionPoint("synthetic", "gate", "code", "verify-canary")
    augmented = dict(_MOD.DECISION_POINTS)
    augmented["synthetic_workflow"] = (dp,)
    grown = _MOD.render_runbook(augmented)
    assert grown != base
    assert "## `synthetic_workflow`" in grown
    assert "## `synthetic_workflow`" not in base


# ── the Claude-idiom denylist pin ──────────────────────────────────────────


def test_output_carries_no_claude_idiom() -> None:
    """The runbook is harness-neutral: no rendered line carries a Claude-idiom
    token (the §1 prose-saturation inventory)."""
    rendered = _MOD.render_runbook()
    hits = [tok for tok in _MOD.CLAUDE_IDIOM_DENYLIST if tok in rendered]
    assert not hits, f"Claude-idiom token(s) leaked into the runbook: {hits}"


def test_committed_runbook_carries_no_claude_idiom() -> None:
    """Belt-and-braces: the on-disk committed doc is idiom-free too."""
    text = _RUNBOOK.read_text(encoding="utf-8")
    hits = [tok for tok in _MOD.CLAUDE_IDIOM_DENYLIST if tok in text]
    assert not hits, f"Claude-idiom token(s) in committed runbook: {hits}"


# ── the fire path (throwaway copy) ─────────────────────────────────────────


def test_check_fires_red_on_hand_edit_then_write_heals(tmp_path: Path) -> None:
    """Hand-edit the generated doc (the thing a foreign harness must NOT do)
    without regenerating → ``--check`` is RED; ``--write`` heals → green."""
    out = tmp_path / "harness-runbook.md"
    # Seed the throwaway with a fresh render, then confirm it is in sync.
    assert _MOD.build(out_path=out, write=True) == 0
    assert _MOD.build(out_path=out, write=False) == 0

    text = out.read_text(encoding="utf-8")
    mutated = text.replace("# The harness runbook", "# The harness runbook (hand-edited)", 1)
    assert mutated != text, "banner heading not found to mutate"
    out.write_text(mutated, encoding="utf-8")

    # The doc drifted from the projection → check is red.
    assert _MOD.build(out_path=out, write=False) == 1
    # Regenerate → green, and the hand edit is gone.
    assert _MOD.build(out_path=out, write=True) == 0
    assert _MOD.build(out_path=out, write=False) == 0
    assert "(hand-edited)" not in out.read_text(encoding="utf-8")


def test_write_creates_missing_parent(tmp_path: Path) -> None:
    """``--write`` materializes the doc (and its parent) when absent."""
    out = tmp_path / "generated" / "harness-runbook.md"
    assert not out.exists()
    assert _MOD.build(out_path=out, write=True) == 0
    assert out.is_file()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
