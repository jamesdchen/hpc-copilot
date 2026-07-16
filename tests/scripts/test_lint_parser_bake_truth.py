"""Fire-path tests for ``scripts/lint_parser_bake_truth.py``.

The lint forbids a hand ``add_argument`` inside the registry parser walk
(``_register_from_registry``): such a flag is invisible to the ``operations.json``
bake, so the live CLI and the baked catalog silently diverge (latency A2).
"""

from __future__ import annotations

from pathlib import Path

import scripts.lint_parser_bake_truth as lint

# A synthetic parser walk carrying a hand ``add_argument`` — the exact shape the
# lint exists to reject.
_DIRTY = """
def _register_from_registry(sub):
    for name, meta in registry.items():
        parser = sub.add_parser(name)
        _add_standard_args(parser, meta.cli)
        if name == "describe":
            parser.add_argument("--schema", action="store_true")
        _bind_dispatch(parser, name)
"""

# The compliant shape: every flag flows through _add_standard_args (CliShape.args);
# the walk only creates subparsers.
_CLEAN = """
def _register_from_registry(sub):
    for name, meta in registry.items():
        parser = sub.add_parser(name)
        _add_standard_args(parser, meta.cli)
        _bind_dispatch(parser, name)
"""


def test_fires_on_synthetic_in_walk_add_argument() -> None:
    violations = lint.find_violations(_DIRTY, "synthetic.py")
    assert len(violations) == 1
    assert "add_argument" in violations[0]
    assert "_register_from_registry" in violations[0]


def test_clean_walk_passes() -> None:
    assert lint.find_violations(_CLEAN, "synthetic.py") == []


def test_missing_guarded_function_is_a_failure() -> None:
    # A rename that removes _register_from_registry must not silently disable the
    # guard — it fires so someone updates the lint.
    violations = lint.find_violations("def something_else():\n    pass\n", "synthetic.py")
    assert len(violations) == 1
    assert "not found" in violations[0]


def test_main_passes_on_the_real_parser(tmp_path: Path) -> None:
    # The shipped parser.py must be clean (the --schema flag now lives in
    # describe's CliShape.args, not a hand add_argument in the walk).
    assert lint.main() == 0


def test_main_fires_on_a_dirty_target(tmp_path: Path) -> None:
    dirty = tmp_path / "parser.py"
    dirty.write_text(_DIRTY, encoding="utf-8")
    assert lint.main(target=dirty) == 1
