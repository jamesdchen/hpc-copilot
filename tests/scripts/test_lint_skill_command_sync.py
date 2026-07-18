"""Fire-path tests for the ``SKILL_ONLY_OK`` / ``SLASH_ONLY_OK`` ⊆-present
guards in ``lint_skill_command_sync``.

The guard exists because an allow-list entry for a file that does NOT exist on
disk passes every prose lint VACUOUSLY — the ``hpc-claim-check`` drift the
sweep caught (a skill named in ``SKILL_ONLY_OK`` but never packaged). These
tests pin that a stale allow-list entry is a hard error, per the
engineering-principle "every lint rule must demonstrate its fire path".
"""

from __future__ import annotations

import importlib.util
import sys

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_skill_command_sync", REPO_ROOT / "scripts" / "lint_skill_command_sync.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_skill_command_sync"] = lint
_SPEC.loader.exec_module(lint)


def test_real_tree_is_in_sync() -> None:
    """The current tree passes — every allow-list entry names a present file."""
    assert lint.main() == 0


def test_stale_skill_only_ok_fires(monkeypatch, capsys) -> None:
    """A ``SKILL_ONLY_OK`` entry with no SKILL.md on disk is a hard error."""
    monkeypatch.setattr(lint, "SKILL_ONLY_OK", lint.SKILL_ONLY_OK | {"ghost-skill"})
    assert lint.main() == 1
    assert "ghost-skill" in capsys.readouterr().err


def test_stale_slash_only_ok_fires(monkeypatch, capsys) -> None:
    """A ``SLASH_ONLY_OK`` entry with no command .md on disk is a hard error."""
    monkeypatch.setattr(lint, "SLASH_ONLY_OK", lint.SLASH_ONLY_OK | {"ghost-slash"})
    assert lint.main() == 1
    assert "ghost-slash" in capsys.readouterr().err


def test_workflow_grammar_passes_on_real_tree() -> None:
    """The frozen ``hpc-<stem>`` <-> ``<stem>-hpc`` grammar holds on the real tree.

    The two grandfathered divergences (``monitor-hpc``, ``new-experiment-hpc``)
    are covered by ``_GRAMMAR_EXEMPT_PAIRS``, so no error is raised.
    """
    errors: list[str] = []
    lint._check_workflow_grammar(errors)
    assert errors == []


def test_workflow_grammar_fails_on_bad_pair(monkeypatch) -> None:
    """A non-conforming, non-exempt WORKFLOW_PAIRS entry is a hard error."""
    monkeypatch.setattr(lint, "WORKFLOW_PAIRS", [("hpc-foo", "bar-hpc")])
    errors: list[str] = []
    lint._check_workflow_grammar(errors)
    assert len(errors) == 1
    assert "hpc-foo" in errors[0]
    assert "bar-hpc" in errors[0]
    assert "grammar" in errors[0].lower()


def test_workflow_grammar_exempt_pair_is_grandfathered(monkeypatch) -> None:
    """An exempt divergent pair passes even though its stems differ."""
    monkeypatch.setattr(lint, "WORKFLOW_PAIRS", [("hpc-status", "monitor-hpc")])
    errors: list[str] = []
    lint._check_workflow_grammar(errors)
    assert errors == []
