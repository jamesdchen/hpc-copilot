"""Smoke tests for ``scripts/regen_all.py`` — the single regen entry point.

These assertions are SEMANTICS-INDEPENDENT: they pin the exported pipeline
(``REGEN_SCRIPTS``), the mode-refusal behaviour, and the per-script argv map,
WITHOUT running the generators against the working tree. The clean-tree
``regen_all.py --check`` assertion deliberately lives as a 3.12 CI STEP (after
the local ``--write`` heal), NOT here: as a pytest test it would execute on the
3.10 / 3.11 matrix legs and the Windows lane, which never run the local regen,
violating ci.yml's self-heal doctrine (every ``--check`` gate is preceded by a
local ``--write`` on the leg that runs it).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "regen_all.py"


def _load_regen_all():
    spec = importlib.util.spec_from_file_location("_regen_all_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MOD = _load_regen_all()


# ── the exported pipeline ──────────────────────────────────────────────────

_EXPECTED_ORDER = (
    "build_schemas",
    "bake_operations_json",
    "build_primitive_frontmatter",
    "build_primitive_index",
    "build_operations_index",
    "build_verb_module_map",
    "build_principles_index",
    "build_harness_runbook",
    "check_no_pending_primitive_docs",
)


def test_regen_scripts_tuple_is_the_frozen_order() -> None:
    """The exported canonical list (other units probe it) is the frozen
    dependency order from the architect memo."""
    assert _MOD.REGEN_SCRIPTS == _EXPECTED_ORDER
    assert isinstance(_MOD.REGEN_SCRIPTS, tuple)


def test_every_pipeline_script_exists() -> None:
    for stem in _MOD.REGEN_SCRIPTS:
        assert (_REPO_ROOT / "scripts" / f"{stem}.py").is_file(), stem


def test_steps_argv_map_matches_script_flag_semantics() -> None:
    """The per-script argv absorbs the scripts' inconsistent flag semantics:
    index scripts take bare argv in --write; check_no_pending takes none."""
    argv_by_stem = {stem: (chk, wrt) for stem, chk, wrt in _MOD._STEPS}
    # --check is uniform across the seven generators.
    for stem in _EXPECTED_ORDER[:-1]:
        assert argv_by_stem[stem][0] == ("--check",), stem
    # The index scripts recognise only --check; --write is expressed as bare.
    assert argv_by_stem["build_primitive_index"][1] == ()
    assert argv_by_stem["build_operations_index"][1] == ()
    # The flag-taking scripts pass --write explicitly.
    for stem in (
        "build_schemas",
        "bake_operations_json",
        "build_primitive_frontmatter",
        "build_verb_module_map",
        "build_principles_index",
        "build_harness_runbook",
    ):
        assert argv_by_stem[stem][1] == ("--write",), stem
    # check_no_pending is always a check (no flags, both modes).
    assert argv_by_stem["check_no_pending_primitive_docs"] == ((), ())


# ── mode refusal (bare invocation is refused) ──────────────────────────────


def test_bare_invocation_refused() -> None:
    assert _MOD.main([]) == 2


def test_both_modes_refused() -> None:
    assert _MOD.main(["--check", "--write"]) == 2


@pytest.mark.parametrize("mode", ["--check", "--write"])
def test_exactly_one_mode_accepted(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` dispatches to ``regen_all`` for a single valid mode (steps are
    stubbed so no subprocess runs — this pins argument parsing, not the run)."""
    calls: list[bool] = []

    def _fake_regen_all(*, write: bool) -> int:
        calls.append(write)
        return 0

    monkeypatch.setattr(_MOD, "regen_all", _fake_regen_all)
    assert _MOD.main([mode]) == 0
    assert calls == [mode == "--write"]


def test_run_step_uses_current_interpreter_and_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Steps run as subprocesses of the current interpreter, cwd=repo root."""
    captured_cmd: list[str] = []
    captured_cwd: list[Path] = []

    class _Proc:
        returncode = 0

    def _fake_run(cmd: list[str], cwd: Path) -> _Proc:
        captured_cmd.extend(cmd)
        captured_cwd.append(cwd)
        return _Proc()

    monkeypatch.setattr(_MOD.subprocess, "run", _fake_run)
    rc = _MOD._run_step("build_schemas", ("--check",))
    assert rc == 0
    assert captured_cwd == [_MOD.REPO_ROOT]
    assert captured_cmd[0] == sys.executable
    assert captured_cmd[1].endswith("build_schemas.py")
    assert captured_cmd[2] == "--check"
