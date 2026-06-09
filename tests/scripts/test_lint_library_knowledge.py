"""Tests for the library-knowledge boundary lint
(docs/internals/engineering-principles.md, "core dispatches, never branches").

Pins four invariants:

1. The real tree passes — every current import of a knowledge package is a
   declared assembly point (or inside the package).
2. The lint can actually FIRE: an undeclared import of a knowledge package is
   reported with the remediation — including the evasive spellings (lazy
   function-level imports, the ``from parent import package`` alias form, and
   relative imports that climb parents).
3. The growth trigger fires: at >= 2 member modules, a non-registry assembly
   point still binding a member module by name fails with the collapse
   remediation; consuming the package-root API instead passes.
4. List hygiene fires: a declared assembly point that vanished, or that no
   longer imports its package, fails the lint — the list cannot rot.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_library_knowledge", REPO_ROOT / "scripts" / "lint_library_knowledge.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_library_knowledge"] = lint
_SPEC.loader.exec_module(lint)


def test_real_tree_is_clean() -> None:
    assert lint.main() == 0


def _mini_tree(tmp_path: Path) -> Path:
    """A minimal scan root satisfying assembly-point hygiene for both packages."""
    root = tmp_path / "src" / "hpc_agent"
    for rel, body in {
        "experiment_kit/solver_adapters/petsc.py": "X = 1\n",
        "experiment_kit/checkpoint_formats.py": (
            "from hpc_agent.experiment_kit.solver_adapters import petsc\n"
        ),
        "incorporation/wrap_entry_point.py": (
            "from hpc_agent.experiment_kit.solver_adapters.petsc import resume_args\n"
        ),
        "ops/detect_entry_point.py": (
            "from hpc_agent.experiment_kit.solver_adapters import detect_petsc_solver\n"
        ),
        "experiment_kit/axis_matcher/matchers/stencil.py": "Y = 2\n",
        "experiment_kit/axis_matcher/_classifier.py": (
            "from hpc_agent.experiment_kit.axis_matcher.matchers.stencil import Y\n"
        ),
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


def test_mini_tree_with_only_assembly_points_is_clean(tmp_path: Path) -> None:
    assert lint.main(_mini_tree(tmp_path)) == 0


def test_undeclared_import_fires(tmp_path: Path, capsys) -> None:
    root = _mini_tree(tmp_path)
    rogue = root / "ops" / "rogue.py"
    rogue.write_text("from hpc_agent.experiment_kit.solver_adapters import petsc\n")
    assert lint.main(root) == 1
    err = capsys.readouterr().err
    assert "rogue.py" in err and "not a declared assembly point" in err
    assert "lint_library_knowledge.py" in err  # remediation names the list


def test_lazy_function_level_import_fires(tmp_path: Path) -> None:
    """A lazy import crosses the boundary just the same — the AST walk must
    see inside function bodies."""
    root = _mini_tree(tmp_path)
    (root / "ops" / "lazy.py").write_text(
        "def f():\n"
        "    from hpc_agent.experiment_kit.solver_adapters.petsc import canary_options\n"
        "    return canary_options\n"
    )
    assert lint.main(root) == 1


def test_alias_form_import_fires(tmp_path: Path) -> None:
    """``from parent import package`` binds the knowledge package without its
    dotted name ever appearing in the ``from`` clause — must still fire."""
    root = _mini_tree(tmp_path)
    (root / "ops" / "alias.py").write_text("from hpc_agent.experiment_kit import solver_adapters\n")
    assert lint.main(root) == 1


def test_relative_import_evasion_fires(tmp_path: Path) -> None:
    """A relative import can climb parents (``from ..experiment_kit...``) and
    reach the knowledge package — resolved, not skipped."""
    root = _mini_tree(tmp_path)
    (root / "ops" / "sneaky.py").write_text("from ..experiment_kit.solver_adapters import petsc\n")
    assert lint.main(root) == 1


def test_intra_package_imports_are_free(tmp_path: Path) -> None:
    """Modules inside a knowledge package may import their siblings — even
    when the family has multiple members (matchers has two here, and the
    classifier registry binding them by name stays legal too)."""
    root = _mini_tree(tmp_path)
    (root / "experiment_kit" / "axis_matcher" / "matchers" / "ema.py").write_text(
        "from hpc_agent.experiment_kit.axis_matcher.matchers.stencil import Y\n"
    )
    assert lint.main(root) == 0


def test_growth_trigger_fires_on_second_member(tmp_path: Path, capsys) -> None:
    """Adapter #2 lands: a non-registry assembly point still binding a member
    module by name fails with the collapse remediation."""
    root = _mini_tree(tmp_path)
    (root / "experiment_kit" / "solver_adapters" / "fenics.py").write_text("X = 2\n")
    assert lint.main(root) == 1
    err = capsys.readouterr().err
    assert "wrap_entry_point.py" in err and "growth trigger" in err
    assert "experiment_kit/checkpoint_formats.py" in err  # names the registry


def test_private_helpers_do_not_arm_growth_trigger(tmp_path: Path) -> None:
    """A shared ``_common.py`` is an implementation detail — the natural
    shape of the collapse refactor itself — not a second family member."""
    root = _mini_tree(tmp_path)
    (root / "experiment_kit" / "solver_adapters" / "_common.py").write_text("Z = 0\n")
    assert lint.main(root) == 0  # wrap_entry_point still binds .petsc: legal at 1 member


def test_growth_trigger_satisfied_by_root_api(tmp_path: Path) -> None:
    """Same two-member family: assembly points consuming the package-root API
    pass, and the registry keeps binding member modules by name."""
    root = _mini_tree(tmp_path)
    (root / "experiment_kit" / "solver_adapters" / "fenics.py").write_text("X = 2\n")
    (root / "incorporation" / "wrap_entry_point.py").write_text(
        "from hpc_agent.experiment_kit.solver_adapters import resume_args\n"
    )
    assert lint.main(root) == 0


def test_stale_assembly_point_missing_file_fires(tmp_path: Path, capsys) -> None:
    root = _mini_tree(tmp_path)
    (root / "ops" / "detect_entry_point.py").unlink()
    assert lint.main(root) == 1
    assert "does not exist" in capsys.readouterr().err


def test_stale_assembly_point_no_longer_importing_fires(tmp_path: Path, capsys) -> None:
    root = _mini_tree(tmp_path)
    (root / "ops" / "detect_entry_point.py").write_text("Z = 3\n")
    assert lint.main(root) == 1
    assert "no longer imports it" in capsys.readouterr().err
