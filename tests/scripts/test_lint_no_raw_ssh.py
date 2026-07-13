"""Tests for the no-raw-ssh lint (issue: throttled inspect-deployment + lint).

Pins these invariants (mirrors ``test_lint_backend_boundary.py``):

1. The real tree passes — no SKILL body offers a raw-ssh affordance today.
2. The lint can actually FIRE: a bare ``ssh`` / ``scp`` / ``rsync`` invocation
   in a code span (inline or fenced) of a SKILL is reported with the
   throttled-verb remediation.
3. Documentation forms do NOT fire: plain-prose mentions outside code spans,
   the bare word with no argument, identifier forms (``ssh_run`` /
   ``ssh_target`` / ``rsync_push`` / ``ssh-add``), and angle-bracket
   placeholder destinations.
4. The cited ALLOWLIST exempts a path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_no_raw_ssh", REPO_ROOT / "scripts" / "lint_no_raw_ssh.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_no_raw_ssh"] = lint
_SPEC.loader.exec_module(lint)


def test_real_tree_is_clean() -> None:
    """No agent-facing surface offers a raw-ssh affordance on the current tree."""
    assert lint.main() == 0


def _skill(tmp_path: Path, body: str, *, name: str = "hpc-demo") -> Path:
    """Write *body* as a SKILL.md under a synthetic scan root and return the root."""
    root = tmp_path / "src"
    p = root / "hpc_agent" / "slash_commands" / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return root


def test_inline_raw_ssh_fires(tmp_path: Path, capsys) -> None:
    root = _skill(tmp_path, 'Inspect with `ssh usc-discovery "ls /scratch1/jc_905/"`.\n')
    assert lint.main(root) == 1
    err = capsys.readouterr()
    assert "raw-ssh affordance" in err.out
    assert "inspect-deployment" in err.out


def test_fenced_raw_ssh_fires(tmp_path: Path) -> None:
    """A fenced (multi-line) code block is scanned just like an inline span."""
    root = _skill(tmp_path, 'Run:\n\n```bash\nssh usc-discovery "hostname"\n```\n')
    assert lint.main(root) == 1


def test_reports_the_line_of_the_match_not_the_span_start(tmp_path: Path, capsys) -> None:
    """Regression: the reported line is the match's own line — even deep inside a
    fenced block (the offset-vs-line-count bug reported a wrong number)."""
    # ``ssh ...`` sits on line 6 of the file (1: text, 2: blank, 3: fence,
    # 4: comment, 5: blank, 6: the invocation).
    body = 'Intro line\n\n```bash\n# preamble comment\n\nssh host "ls"\n```\n'
    root = _skill(tmp_path, body)
    assert lint.main(root) == 1
    out = capsys.readouterr().out
    assert ":6:" in out, out


def test_raw_scp_and_rsync_fire(tmp_path: Path) -> None:
    root = _skill(tmp_path, "Pull with `scp host:/a /b` or `rsync host:/a /b`.\n")
    assert lint.main(root) == 1


def test_prose_mention_outside_code_span_is_clean(tmp_path: Path) -> None:
    """Only code spans are scanned — explanatory prose ("raw ssh") is fine."""
    root = _skill(tmp_path, "Do not reach for raw ssh which bypasses the guards.\n")
    assert lint.main(root) == 0


def test_bare_word_no_argument_is_clean(tmp_path: Path) -> None:
    """The keyword in backticks with no argument is a noun, not an invocation."""
    root = _skill(tmp_path, "Do **not** `rsync` the sidecar by hand; use the verb.\n")
    assert lint.main(root) == 0


def test_identifier_forms_are_clean(tmp_path: Path) -> None:
    """``ssh_run`` / ``ssh_target`` / ``rsync_push`` / ``ssh-add`` are not invocations."""
    root = _skill(
        tmp_path,
        "Routes through `infra.remote.ssh_run` to the `ssh_target`; "
        "`rsync_push` diffs; setup runs `ssh-add -l`.\n",
    )
    assert lint.main(root) == 0


def test_placeholder_destination_is_clean(tmp_path: Path) -> None:
    """An angle-bracket placeholder destination is illustrative documentation."""
    root = _skill(tmp_path, "check-preflight runs an `ssh <host> echo ok` round-trip.\n")
    assert lint.main(root) == 0


def test_allowlist_exempts_a_path(tmp_path: Path, monkeypatch) -> None:
    root = _skill(tmp_path, 'Debug with `ssh usc-discovery "ls"`.\n', name="hpc-debug")
    rel = "hpc_agent/slash_commands/skills/hpc-debug/SKILL.md"
    assert lint.main(root) == 1  # fires without the exemption
    monkeypatch.setattr(lint, "ALLOWLIST", frozenset({rel}))
    assert lint.main(root) == 0  # cited exemption clears it
