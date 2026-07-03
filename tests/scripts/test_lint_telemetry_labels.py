"""Tests for the telemetry-label lint (design §5: tick telemetry is lintable).

Pins these invariants (mirrors ``test_lint_no_raw_ssh.py``):

1. The real tree passes — every field the real tick record + renderers emit is
   declared in ``FIELD_KIND`` today. This is the coupling test: it fails if the
   registry ever drifts from what ``summary.py`` / ``tick_log.py`` actually
   emit.
2. The lint can actually FIRE:
   * a tick-record field emitted without a declared kind (the ``told 0``
     failure: a ``told`` count shipped with no cumulative/delta label), and
   * a renderer field referenced but absent from ``FIELD_KIND``.
3. A missing ``FIELD_KIND`` registry fires.
4. A clean synthetic pair passes (the fire cases are real, not tautological).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_telemetry_labels", REPO_ROOT / "scripts" / "lint_telemetry_labels.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_telemetry_labels"] = lint
_SPEC.loader.exec_module(lint)


# --- synthetic-tree helpers -------------------------------------------------

# A clean registry declaring exactly the fields the synthetic sources below
# reference / emit.
_CLEAN_SUMMARY = """\
FIELD_KIND = {
    "complete": "cumulative",
    "total": "cumulative",
    "newly_complete": "delta",
    "tick_id": "label",
    "summary": "label",
}


def _render_scalar(name, value):
    return f"{name}={value}"


def _format_counts(summary, total):
    return _render_scalar("complete", summary.get("complete")) + _render_scalar("total", total)


def _format_diff(diff):
    return _render_scalar("newly_complete", diff.get("newly_complete"))
"""

_CLEAN_TICK_LOG = """\
def _append_tick(experiment_dir, run_id):
    record = {
        "tick_id": "t",
        "summary": {},
    }
    return record
"""


def _write_pair(tmp_path: Path, *, summary: str, tick_log: str) -> Path:
    """Write a synthetic ``summary.py`` + ``tick_log.py`` under a repo root."""
    base = tmp_path / "src" / "hpc_agent" / "ops" / "monitor"
    base.mkdir(parents=True, exist_ok=True)
    (base / "summary.py").write_text(summary, encoding="utf-8")
    (base / "tick_log.py").write_text(tick_log, encoding="utf-8")
    return tmp_path


# --- 1. real tree is clean --------------------------------------------------


def test_real_tree_is_clean() -> None:
    """Every field the live renderers + tick record emit is declared today."""
    assert lint.main() == 0


# --- 4. clean synthetic pair passes (fire cases are non-tautological) -------


def test_clean_pair_passes(tmp_path: Path) -> None:
    root = _write_pair(tmp_path, summary=_CLEAN_SUMMARY, tick_log=_CLEAN_TICK_LOG)
    assert lint.main(root) == 0


# --- 2a. tick-record field with no declared kind fires ("told 0") ----------


def test_undeclared_tick_record_field_fires(tmp_path: Path, capsys) -> None:
    # A ``told`` count added to the record without declaring cumulative vs
    # delta — the exact confusion class the contract exists to prevent.
    dirty_tick = """\
def _append_tick(experiment_dir, run_id):
    record = {
        "tick_id": "t",
        "summary": {},
        "told": 0,
    }
    return record
"""
    root = _write_pair(tmp_path, summary=_CLEAN_SUMMARY, tick_log=dirty_tick)
    assert lint.main(root) == 1
    out = capsys.readouterr().out
    assert "told" in out
    assert "FIELD_KIND" in out


# --- 2b. renderer field with no declared kind fires ------------------------


def test_undeclared_rendered_field_fires(tmp_path: Path, capsys) -> None:
    # ``skipped`` is rendered but never declared in FIELD_KIND (swap the
    # declared ``newly_complete`` reference for an undeclared field name).
    dirty_summary = _CLEAN_SUMMARY.replace("newly_complete", "skipped")
    # Drop the now-stale ``newly_complete`` registry entry so ``skipped`` is
    # genuinely undeclared (otherwise the replace would have renamed it too).
    dirty_summary = dirty_summary.replace('    "skipped": "delta",\n', "")
    root = _write_pair(tmp_path, summary=dirty_summary, tick_log=_CLEAN_TICK_LOG)
    assert lint.main(root) == 1
    out = capsys.readouterr().out
    assert "skipped" in out


# --- 3. a missing registry fires -------------------------------------------


def test_missing_registry_fires(tmp_path: Path, capsys) -> None:
    no_registry = """\
def _render_scalar(name, value):
    return f"{name}={value}"


def _format_counts(summary, total):
    return _render_scalar("complete", summary.get("complete"))
"""
    root = _write_pair(tmp_path, summary=no_registry, tick_log=_CLEAN_TICK_LOG)
    assert lint.main(root) == 1
    out = capsys.readouterr().out
    assert "FIELD_KIND" in out


# --- a `.get` on a non-count receiver is NOT a telemetry field -------------


def test_get_on_unrelated_receiver_is_ignored(tmp_path: Path) -> None:
    """``other.get("x")`` (receiver not a cumulative/delta block) is not a
    telemetry-field reference — only ``summary`` / ``diff`` blocks count."""
    summary = _CLEAN_SUMMARY + '\n\ndef _misc(other):\n    return other.get("unlabeled_key")\n'
    root = _write_pair(tmp_path, summary=summary, tick_log=_CLEAN_TICK_LOG)
    assert lint.main(root) == 0


# --- 2c. kill render helper is covered (a kill field must be declared) -------


def test_undeclared_kill_field_fires(tmp_path: Path, capsys) -> None:
    """A field rendered through ``_format_kill_count`` but absent from FIELD_KIND
    fires — the §5 kill-telemetry render helper is inside the lint's coverage."""
    dirty = _CLEAN_SUMMARY + (
        "\n\ndef _format_kill_count(field, value):\n"
        '    return f"{value}"\n\n\n'
        "def _render_kill():\n"
        '    return _format_kill_count("kill_requested", 3)\n'
    )
    root = _write_pair(tmp_path, summary=dirty, tick_log=_CLEAN_TICK_LOG)
    assert lint.main(root) == 1
    out = capsys.readouterr().out
    assert "kill_requested" in out


def test_declared_kill_field_passes(tmp_path: Path) -> None:
    """The same kill render, with the field declared cumulative, is clean —
    proving the fire above is non-tautological."""
    clean = _CLEAN_SUMMARY.replace(
        '    "complete": "cumulative",\n',
        '    "complete": "cumulative",\n    "kill_requested": "cumulative",\n',
    ) + (
        "\n\ndef _format_kill_count(field, value):\n"
        '    return f"{value}"\n\n\n'
        "def _render_kill():\n"
        '    return _format_kill_count("kill_requested", 3)\n'
    )
    root = _write_pair(tmp_path, summary=clean, tick_log=_CLEAN_TICK_LOG)
    assert lint.main(root) == 0
