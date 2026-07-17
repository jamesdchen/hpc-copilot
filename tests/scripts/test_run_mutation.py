"""Tests for the curated per-module mutation runner scoping (devx B4).

mutmut is Linux-CI-only, so these never invoke it. They pin the PURE-PYTHON
scoping logic the memo Units B-D touched:

* every ``MODULE_MAP`` entry's source + covering test files exist (Units C/D);
* ``render_scoped_pyproject`` emits an ABSOLUTE, POSIX ``paths_to_mutate`` so a
  chdir'ing test can't break mutmut's ``resolve(strict=True)`` (Unit B combiner
  crash) — and the rendered TOML parses on every host;
* the Unit D correctness/consent seams are present in the map.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import tomllib

from tests._paths import REPO_ROOT

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
def test_render_paths_to_mutate_is_absolute_posix_and_parseable(key):
    """Unit B: paths_to_mutate is rendered ABSOLUTE + POSIX so mutmut's
    relative-source resolution can't break under a chdir'ing test, and the TOML
    always parses (a Windows backslash would be an illegal escape)."""
    scope = rm.MODULE_MAP[key]
    rendered = rm.render_scoped_pyproject(scope)
    parsed = tomllib.loads(rendered)  # raises if the rewrite broke the TOML
    paths = parsed["tool"]["mutmut"]["paths_to_mutate"]
    assert len(paths) == 1
    p = paths[0]
    assert Path(p).is_absolute(), f"{key}: paths_to_mutate not absolute: {p!r}"
    assert "\\" not in p, f"{key}: paths_to_mutate must be POSIX (no backslash): {p!r}"
    assert p.endswith(scope.source.split("/")[-1])
    # tests_dir is the module's covering set, verbatim.
    assert parsed["tool"]["mutmut"]["tests_dir"] == list(scope.tests)


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
