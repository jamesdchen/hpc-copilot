"""Tests for the no-blocklisted-commands lint (harness block-list avoidance).

Pins these invariants (mirrors ``test_lint_no_raw_ssh.py``):

1. The real tree passes — no SKILL authors a harness-blocked command today
   (``worker_prompts/*.md`` is retired from the scan; its invoke-only rule is
   exercised via ``lint_file`` directly).
2. The lint FIRES on each blocked shape: ``python -c`` / ``bash -c`` with a real
   argument, command substitution ``$(...)``, a pipe, a deny-listed verb
   (``scancel`` / ``rm -rf`` / …), a chain to a non-allow-listed command, a
   background ``&``, and ANY chaining inside a worker prompt (invoke-only).
3. The legitimate forms do NOT fire: an all-``hpc-agent``/``git`` ``&&`` chain in
   a SKILL (the classifier splits + allows each), a bare operator/keyword noun
   (`` `&&` `` / `` `bash -c` ``), a ``<sge|slurm>`` placeholder, and a ``&`` /
   ``;`` on a non-framework line (a counter-example or prose).
4. The cited ALLOWLIST exempts a ``(path, category)``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_no_blocklisted_commands", REPO_ROOT / "scripts" / "lint_no_blocklisted_commands.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_no_blocklisted_commands"] = lint
_SPEC.loader.exec_module(lint)


def _skill(tmp_path: Path, body: str, *, name: str = "hpc-demo") -> Path:
    root = tmp_path / "src"
    p = root / "hpc_agent" / "slash_commands" / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return root


def _worker_prompt_file(tmp_path: Path, body: str, *, name: str = "demo.md") -> Path:
    """Write *body* at a ``worker_prompts/`` path and return the FILE.

    The ``worker_prompts/*.md`` glob is retired from the default scan (see
    ``scripts/_agent_prose_targets.py``), so the invoke-only worker-strictness
    logic is exercised by handing the path straight to :func:`lint_file`, not
    by scanning a root through ``main``.
    """
    p = tmp_path / "src" / "hpc_agent" / "_kernel" / "extension" / "worker_prompts" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_real_tree_is_clean() -> None:
    """No agent-facing surface authors a harness-blocked command on the tree."""
    assert lint.main() == 0


# --- the lint FIRES -------------------------------------------------------


def test_python_dash_c_fires(tmp_path: Path, capsys) -> None:
    root = _skill(tmp_path, "Read it with `python -c 'import json,sys; print(1)'`.\n")
    assert lint.main(root) == 1
    assert "python -c" in capsys.readouterr().out


def test_bash_dash_c_fires(tmp_path: Path) -> None:
    assert lint.main(_skill(tmp_path, "Run `bash -c 'qstat -u $USER'` to check.\n")) == 1


def test_command_substitution_fires(tmp_path: Path) -> None:
    root = _skill(tmp_path, "Submit with `hpc-agent submit --spec $(cat spec.json)`.\n")
    assert lint.main(root) == 1


def test_pipe_fires(tmp_path: Path) -> None:
    assert lint.main(_skill(tmp_path, "Filter via `hpc-agent describe x | grep run`.\n")) == 1


def test_deny_command_fires(tmp_path: Path) -> None:
    assert lint.main(_skill(tmp_path, "Cancel with `scancel 9580235` if stuck.\n")) == 1
    assert lint.main(_skill(tmp_path, "Clean up: `rm -rf .hpc/runs`.\n", name="hpc-rm")) == 1


def test_mixed_chain_to_nonallowlisted_fires(tmp_path: Path) -> None:
    """A SKILL ``&&`` chain whose segment is NOT hpc-agent/git is flagged."""
    root = _skill(tmp_path, "Do `hpc-agent submit --experiment-dir . && python setup.py`.\n")
    assert lint.main(root) == 1


def test_background_fires(tmp_path: Path) -> None:
    assert lint.main(_skill(tmp_path, "Background it: `hpc-agent run --workflow submit &`.\n")) == 1


def test_chain_in_worker_prompt_fires(tmp_path: Path) -> None:
    """The invoke-only worker forbids ALL chaining — even all-hpc-agent.

    ``worker_prompts/*.md`` is retired from the default scan, so the
    worker-strictness path is exercised by handing the file to ``lint_file``
    directly (``is_worker`` still keys off the path's ``worker_prompts`` part).
    """
    path = _worker_prompt_file(
        tmp_path, "First `hpc-agent install-commands && hpc-agent submit`.\n"
    )
    findings = lint.lint_file(path)
    assert any("worker is invoke-only" in category for _lineno, category, _msg in findings)


# --- the lint stays QUIET on legitimate forms -----------------------------


def test_allowlisted_chain_in_skill_is_clean(tmp_path: Path) -> None:
    """An all-hpc-agent/git ``&&`` chain in a SKILL is classifier-safe → not flagged."""
    root = _skill(
        tmp_path,
        "Warm up: `hpc-agent install-commands && hpc-agent load-context --experiment-dir .`.\n",
    )
    assert lint.main(root) == 0


def test_bare_operator_and_keyword_nouns_are_clean(tmp_path: Path) -> None:
    """Operators/keywords NAMED in backticks (warning prose) are nouns, not commands."""
    body = "Never chain with `&&` or `||`, and never shell `bash -c` / `python -c`.\n"
    assert lint.main(_skill(tmp_path, body)) == 0


def test_placeholder_alternation_is_clean(tmp_path: Path) -> None:
    """A ``|`` inside a ``<sge|slurm>`` placeholder is an alternation, not a pipe."""
    root = _skill(tmp_path, "Run `hpc-agent reconcile --scheduler <sge|slurm|pbspro>`.\n")
    assert lint.main(root) == 0


def test_nonframework_counterexample_is_clean(tmp_path: Path) -> None:
    """``&`` / ``;`` on a line with no hpc-agent/git is a counter-example or prose."""
    root = _skill(tmp_path, "Do NOT use shell concurrency (`cmd1 & cmd2 & wait`, `parallel`).\n")
    assert lint.main(root) == 0


def test_prose_semicolon_in_message_is_clean(tmp_path: Path) -> None:
    """A ``;`` inside a quoted prose/message string (no framework command) is fine."""
    root = _skill(tmp_path, "Record `note: main.py present, no marker; ask the user`.\n")
    assert lint.main(root) == 0


def test_python_type_hint_in_python_fence_is_clean(tmp_path: Path) -> None:
    """A ``int | str`` in a non-shell fenced block is not a pipe."""
    root = _skill(tmp_path, "Signature:\n\n```python\ndef f(x: int | str) -> None: ...\n```\n")
    assert lint.main(root) == 0


def test_allowlist_exempts_a_path_and_category(tmp_path: Path, monkeypatch) -> None:
    root = _skill(tmp_path, "Cancel with `scancel 9580235`.\n", name="hpc-debug")
    rel = "hpc_agent/slash_commands/skills/hpc-debug/SKILL.md"
    assert lint.main(root) == 1  # fires without the exemption
    monkeypatch.setattr(lint, "ALLOWLIST", frozenset({(rel, "scancel")}))
    assert lint.main(root) == 0  # cited (path, category) exemption clears it
