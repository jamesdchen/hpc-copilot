"""Tests for the curated per-module mutation runner scoping (devx B4).

mutmut is Linux-CI-only, so these never invoke it. They pin the PURE-PYTHON
scoping logic the memo Units B-D touched:

* every ``MODULE_MAP`` entry's source + covering test files exist (Units C/D);
* ``render_scoped_pyproject`` emits a RELATIVE, POSIX ``paths_to_mutate`` (mirror
  of the working sweep) so mutmut 3.6.0's coverage-join keys on a clean dotted
  module name instead of an absolute path that zeroes the matrix (triage-2 #1) —
  and the rendered TOML parses on every host;
* the chdir'ing in-process tests are ``--deselect``\\ ed (not path-absolutized)
  from the scoped run, and every deselected node-ID lives in the module's
  covering set (triage-2 #1 combiner/state-journal chdir crash);
* the curated zero-signal tripwire flags a dark module (triage-2 #2);
* the Unit D correctness/consent seams are present in the map.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT

# scripts/run_mutation.py itself imports tomllib (3.11+ stdlib) and only ever
# runs in the Linux mutation workflow's interpreter — under 3.10 the module
# under test cannot import either, so skipping here is honest, not lost
# coverage.
tomllib = pytest.importorskip("tomllib")

_SPEC = importlib.util.spec_from_file_location(
    "run_mutation", REPO_ROOT / "scripts" / "run_mutation.py"
)
assert _SPEC is not None and _SPEC.loader is not None
rm = importlib.util.module_from_spec(_SPEC)
sys.modules["run_mutation"] = rm
_SPEC.loader.exec_module(rm)


def test_every_module_scope_is_valid():
    """Source + every covering test path in the map exists (no false survivors
    from a scoped-to-a-missing-file entry)."""
    problems: list[str] = []
    for scope in rm.MODULE_MAP.values():
        problems.extend(f"{scope.key}: {p}" for p in rm._validate_scope(scope))
    assert not problems, f"MODULE_MAP references missing files: {problems}"


def test_unit_d_seams_present():
    """The correctness/consent/journal core is in the curated map (Unit D)."""
    for key in ("state-journal", "state-index", "decision-journal", "consent-hint"):
        assert key in rm.MODULE_MAP, f"{key} missing from MODULE_MAP"


def test_fast_path_cache_paired_with_in_process_battery():
    """Unit B: fast-path-cache must point at the IN-PROCESS test battery, not the
    slow subprocess test_fast_dispatch.py (mutmut can't instrument a child)."""
    scope = rm.MODULE_MAP["fast-path-cache"]
    assert scope.tests == ("tests/cli/test_fast_path_cache.py",)


def test_block_chain_covering_set_broadened():
    """Unit C: the spec-hint contract test (which kills the 183 memo false
    survivors) is in scope."""
    scope = rm.MODULE_MAP["block-chain"]
    assert "tests/contracts/test_spec_hint_completeness.py" in scope.tests


@pytest.mark.parametrize("key", list(rm.MODULE_MAP))
def test_render_paths_to_mutate_is_relative_posix_and_parseable(key):
    """triage-2 #1: paths_to_mutate is rendered RELATIVE + POSIX (mirror of the
    working sweep) so mutmut 3.6.0's coverage-join keys on a clean dotted module
    name -- an absolute path bakes the runner cwd into the mutant key and zeroes
    every mutant. The value is exactly the repo-relative source, and the TOML
    parses (a Windows backslash would be an illegal escape)."""
    scope = rm.MODULE_MAP[key]
    rendered = rm.render_scoped_pyproject(scope)
    parsed = tomllib.loads(rendered)  # raises if the rewrite broke the TOML
    paths = parsed["tool"]["mutmut"]["paths_to_mutate"]
    assert len(paths) == 1
    p = paths[0]
    assert not Path(p).is_absolute(), f"{key}: paths_to_mutate must be relative: {p!r}"
    assert "\\" not in p, f"{key}: paths_to_mutate must be POSIX (no backslash): {p!r}"
    assert p == scope.source, f"{key}: paths_to_mutate must equal the repo-relative source"
    # tests_dir is the module's covering set, verbatim.
    assert parsed["tool"]["mutmut"]["tests_dir"] == list(scope.tests)


def test_render_injects_deselect_for_chdir_modules():
    """triage-2 #1: a scope with ``deselect`` appends ``--deselect=<node-id>`` to
    pytest_add_cli_args (never replacing the committed xdist/addopts overrides),
    so the relative path never has to resolve while a chdir'd test sits in a
    foreign cwd."""
    scope = rm.MODULE_MAP["combiner"]
    assert scope.deselect, "combiner must deselect its chdir'ing end-to-end tests"
    rendered = rm.render_scoped_pyproject(scope)
    cli_args = tomllib.loads(rendered)["tool"]["mutmut"]["pytest_add_cli_args"]
    for node_id in scope.deselect:
        assert f"--deselect={node_id}" in cli_args
    # The committed overrides survive the append.
    assert "-o" in cli_args
    assert any(a.startswith("addopts=") for a in cli_args)


def test_render_no_deselect_leaves_cli_args_untouched():
    """A scope with no ``deselect`` (the common case) must not perturb
    pytest_add_cli_args at all."""
    committed = tomllib.loads(rm.PYPROJECT.read_text(encoding="utf-8"))
    baseline = committed["tool"]["mutmut"]["pytest_add_cli_args"]
    scope = rm.MODULE_MAP["block-chain"]
    assert not scope.deselect
    rendered = rm.render_scoped_pyproject(scope)
    assert tomllib.loads(rendered)["tool"]["mutmut"]["pytest_add_cli_args"] == baseline


@pytest.mark.parametrize("key", list(rm.MODULE_MAP))
def test_deselect_node_ids_reference_covered_test_files(key):
    """Every ``--deselect`` node-ID's file must be in the module's covering set,
    and its Class/test name must actually appear in that file -- a typo'd node-ID
    silently deselects nothing and re-opens the chdir crash."""
    scope = rm.MODULE_MAP[key]
    for node_id in scope.deselect:
        file_part, _, selector = node_id.partition("::")
        assert file_part in scope.tests, f"{key}: {file_part} not in the covering set"
        src = (REPO_ROOT / file_part).read_text(encoding="utf-8")
        for name in selector.split("::"):
            assert name in src, f"{key}: node-id fragment {name!r} not found in {file_part}"


def test_only_chdir_modules_carry_deselect():
    """Guard against deselect creep: only the two modules with in-process chdir
    tests (combiner, state-journal) may carry a deselect list."""
    with_deselect = {k for k, s in rm.MODULE_MAP.items() if s.deselect}
    assert with_deselect == {"combiner", "state-journal"}


# ── curated zero-signal tripwire (triage-2 #2) ──────────────────────────────


def _write_meta(path, exit_code_by_key):
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"exit_code_by_key": exit_code_by_key}), encoding="utf-8")


def test_count_mutant_signal_separates_signal_from_checked(tmp_path):
    """signal = killed(1)+survived(0); checked adds 33 no-tests / 34 skipped;
    total counts every key incl. null."""
    _write_meta(tmp_path / "a.meta", {"m1": 1, "m2": 0, "m3": 33, "m4": 34, "m5": None})
    signal, checked, total = rm.count_mutant_signal(tmp_path)
    assert signal == 2  # only killed + survived
    assert checked == 4  # everything but the null
    assert total == 5


def test_tripwire_fails_when_all_no_tests(tmp_path, capsys):
    """The refinement: a module where every mutant is exit-33 'no tests' is
    checked>0 but carries ZERO signal → RED (was the green loophole)."""
    _write_meta(tmp_path / "a.meta", {"m1": 33, "m2": 33})
    rc = rm.main(["--tripwire", "--mutants-dir", str(tmp_path)])
    assert rc == 1
    assert "CURATED TRIPWIRE FAILED" in capsys.readouterr().err


def test_tripwire_passes_with_signal(tmp_path, capsys):
    _write_meta(tmp_path / "a.meta", {"m1": 1, "m2": None})
    rc = rm.main(["--tripwire", "--mutants-dir", str(tmp_path)])
    assert rc == 0
    assert "tripwire OK" in capsys.readouterr().out


def test_tripwire_fails_on_no_mutants(tmp_path):
    """No *.meta at all (aborted stats / infra crash) is zero-signal → RED."""
    assert rm.main(["--tripwire", "--mutants-dir", str(tmp_path)]) == 1


def test_render_preserves_sibling_mutmut_keys():
    """The scoped render rewrites only paths_to_mutate + tests_dir; also_copy /
    do_not_mutate / pytest_add_cli_args survive."""
    rendered = rm.render_scoped_pyproject(rm.MODULE_MAP["combiner"])
    mutmut = tomllib.loads(rendered)["tool"]["mutmut"]
    assert "also_copy" in mutmut
    assert "do_not_mutate" in mutmut
    assert "pytest_add_cli_args" in mutmut


def test_keys_mode_lists_all_modules(capsys):
    """--keys drives the CI matrix; it must enumerate the whole map."""
    import json

    rm.main(["--keys"])
    keys = json.loads(capsys.readouterr().out)
    assert set(keys) == set(rm.MODULE_MAP)
