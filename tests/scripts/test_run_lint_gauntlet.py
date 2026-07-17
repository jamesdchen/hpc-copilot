"""Tests for ``scripts/run_lint_gauntlet.py`` — the discovery-based lint runner.

Pins the guarantees that make the runner a real defense against the
``c41c7d24`` class ("a lint exists but the local gauntlet doesn't run it"):

1. Discovery globs ``lint_*.py`` at call time, so a NEW lint dropped into
   scripts/ is auto-included and non-lint scripts are ignored.
2. The special-case table is honored: ``extra_argv`` is appended and a
   ``run=False`` entry is skipped without a subprocess.
3. A failing lint reds the run (non-zero exit) AND appears in the summary —
   run-all-report-all, no truncation on the first failure.
4. The CI-parity audit FIRES on a planted orphan (a lint in scripts/ absent
   from ci.yml and unacknowledged), on a stale ci.yml reference, and on a
   stale ``ci_absent`` acknowledgement — while a genuinely acknowledged
   ci-absent lint stays clean.

These tests NEVER run the real full gauntlet (CPU rule): every subprocess
here is a single synthetic one-line lint in a tmp dir.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "run_lint_gauntlet", REPO_ROOT / "scripts" / "run_lint_gauntlet.py"
)
assert _SPEC is not None and _SPEC.loader is not None
gauntlet = importlib.util.module_from_spec(_SPEC)
sys.modules["run_lint_gauntlet"] = gauntlet
_SPEC.loader.exec_module(gauntlet)

LintSpec = gauntlet.LintSpec


def _write_lint(scripts_dir: Path, stem: str, body: str) -> None:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / f"{stem}.py").write_text(body, encoding="utf-8")


# A lint that always passes / always fails / echoes its argv.
_PASS = "import sys\nsys.exit(0)\n"
_FAIL = "import sys\nprint('boom on stdout')\nprint('why', file=sys.stderr)\nsys.exit(1)\n"
_ECHO_ARGV = "import sys\nsys.exit(0 if '--fire-path' in sys.argv else 3)\n"


# ── 1. discovery ───────────────────────────────────────────────────────────


def test_discovery_globs_lint_files_and_ignores_others(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    _write_lint(scripts, "lint_alpha", _PASS)
    _write_lint(scripts, "lint_beta", _PASS)
    _write_lint(scripts, "build_something", _PASS)  # not a lint
    _write_lint(scripts, "helper", _PASS)  # not a lint
    found = gauntlet.discover_lints(scripts)
    assert found == ["lint_alpha", "lint_beta"]


def test_discovery_picks_up_a_newly_added_lint(tmp_path: Path) -> None:
    """The core anti-c41c7d24 property: a lint added later is auto-included."""
    scripts = tmp_path / "scripts"
    _write_lint(scripts, "lint_alpha", _PASS)
    assert gauntlet.discover_lints(scripts) == ["lint_alpha"]
    _write_lint(scripts, "lint_zzz_new", _PASS)  # someone adds a lint
    assert gauntlet.discover_lints(scripts) == ["lint_alpha", "lint_zzz_new"]


# ── 2. special-case table honored ──────────────────────────────────────────


def test_extra_argv_is_appended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = tmp_path / "scripts"
    _write_lint(scripts, "lint_flags", _ECHO_ARGV)
    monkeypatch.setattr(gauntlet, "SCRIPTS_DIR", scripts)
    special = {"lint_flags": LintSpec(reason="needs the flag", extra_argv=("--fire-path",))}
    argv = gauntlet.invocation("lint_flags", special)
    assert argv[-1] == "--fire-path"
    # And it actually runs green only because the flag was passed (script exits
    # 3 without it).
    result = gauntlet.run_one("lint_flags", special)
    assert result.returncode == 0
    assert result.argv == ("--fire-path",)


def test_run_false_entry_is_skipped_without_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = tmp_path / "scripts"
    # No file on disk at all — proving no subprocess is attempted.
    monkeypatch.setattr(gauntlet, "SCRIPTS_DIR", scripts)
    special = {"lint_excluded": LintSpec(reason="cannot run standalone", run=False)}
    result = gauntlet.run_one("lint_excluded", special)
    assert result.skipped is True
    assert result.returncode == 0


# ── 3. a failing lint reds the run and shows in the summary ────────────────


def test_failing_lint_reds_run_and_appears_in_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts = tmp_path / "scripts"
    _write_lint(scripts, "lint_ok", _PASS)
    _write_lint(scripts, "lint_bad", _FAIL)
    monkeypatch.setattr(gauntlet, "SCRIPTS_DIR", scripts)
    rc = gauntlet.run_gauntlet(["lint_ok", "lint_bad"], special={})
    assert rc == 1
    out = capsys.readouterr()
    combined = out.out + out.err
    # Both lints show in the summary (run-all-report-all, not truncated).
    assert "lint_ok" in combined and "lint_bad" in combined
    assert "PASS" in combined and "FAIL" in combined
    # The failing lint's captured output is printed in full.
    assert "boom on stdout" in combined


def test_all_pass_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = tmp_path / "scripts"
    _write_lint(scripts, "lint_a", _PASS)
    _write_lint(scripts, "lint_b", _PASS)
    monkeypatch.setattr(gauntlet, "SCRIPTS_DIR", scripts)
    assert gauntlet.run_gauntlet(["lint_a", "lint_b"], special={}) == 0


# ── 4. the CI-parity audit — the point of the tool ─────────────────────────

# Minimal ci.yml text: references two lints, one of which is on disk.
_CI_TEXT = """
  - name: Lint alpha
    run: python scripts/lint_alpha.py
  - name: Lint beta
    run: python scripts/lint_beta.py --fire-path
"""


def test_parity_clean_when_everything_lines_up() -> None:
    discovered = ["lint_alpha", "lint_beta"]
    problems = gauntlet.check_parity(discovered, _CI_TEXT, special={})
    assert problems == []


def test_parity_fires_on_planted_orphan() -> None:
    """A lint in scripts/ but absent from ci.yml and unacknowledged is the
    c41c7d24 orphan — reported loudly."""
    discovered = ["lint_alpha", "lint_beta", "lint_orphan"]
    problems = gauntlet.check_parity(discovered, _CI_TEXT, special={})
    assert any("lint_orphan" in p and "NOT referenced in ci.yml" in p for p in problems)


def test_parity_orphan_silenced_by_ci_absent_acknowledgement() -> None:
    """The same orphan is clean once the table acknowledges it runs elsewhere."""
    discovered = ["lint_alpha", "lint_beta", "lint_orphan"]
    special = {"lint_orphan": LintSpec(reason="runs via pre-commit", ci_absent=True)}
    problems = gauntlet.check_parity(discovered, _CI_TEXT, special=special)
    assert problems == []


def test_parity_fires_on_stale_ci_reference() -> None:
    """ci.yml names a lint that no longer exists on disk."""
    discovered = ["lint_alpha"]  # lint_beta was renamed/removed
    problems = gauntlet.check_parity(discovered, _CI_TEXT, special={})
    assert any("lint_beta" in p and "does not" in p for p in problems)


def test_parity_fires_on_stale_ci_absent_entry() -> None:
    """A ci_absent acknowledgement for a lint ci.yml actually references is
    stale — flagged so the table can't rot."""
    discovered = ["lint_alpha", "lint_beta"]
    special = {"lint_alpha": LintSpec(reason="was moved out of ci once", ci_absent=True)}
    problems = gauntlet.check_parity(discovered, _CI_TEXT, special=special)
    assert any("lint_alpha" in p and "stale" in p for p in problems)


# ── the real table is internally coherent (cheap, no subprocess) ───────────


def test_real_special_cases_reference_existing_scripts() -> None:
    """Every stem in the shipped SPECIAL_CASES names a real lint file — a table
    entry for a non-existent lint would be dead weight."""
    for stem in gauntlet.SPECIAL_CASES:
        assert (REPO_ROOT / "scripts" / f"{stem}.py").is_file(), stem


def test_real_tree_parity_is_clean() -> None:
    """On the shipped tree the discovered lint set and ci.yml agree (given the
    SPECIAL_CASES acknowledgements) — this is the guard that would red if a lint
    were added to scripts/ without wiring it into ci.yml or the table."""
    discovered = gauntlet.discover_lints()
    ci_text = gauntlet.CI_WORKFLOW.read_text(encoding="utf-8")
    problems = gauntlet.check_parity(discovered, ci_text)
    assert problems == [], "\n".join(problems)
