"""Tests for ``hpc_agent.incorporation.build.template`` (Layer 4 — scaffold injection)."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import pytest
import yaml

import hpc_agent
from hpc_agent import errors
from hpc_agent.experiment_kit import discover_runs, export_notebook
from hpc_agent.incorporation.build.template import build_template

if TYPE_CHECKING:
    from pathlib import Path

_ROOT_FILES = (
    "Makefile",
    ".pre-commit-config.yaml",
    ".github/workflows/ci.yml",
    "conftest.py",
    "pyproject.toml",
)


def test_full_scaffold_on_empty_repo(tmp_path: Path) -> None:
    data = build_template(repo_dir=tmp_path)

    assert (tmp_path / ".hpc" / "template.mk").is_file()
    assert (tmp_path / ".hpc" / "scaffold.py").is_file()
    for rel in _ROOT_FILES:
        assert (tmp_path / rel).is_file(), rel
    # The default shape is `script` — train.py rides in alongside the rest.
    assert (tmp_path / "train.py").is_file()

    assert ".hpc/template.mk" in data["framework_files"]
    assert ".hpc/scaffold.py" in data["framework_files"]
    assert "Makefile" in data["written"]
    assert "train.py" in data["written"]
    assert data["skipped"] == []
    assert data["needs_manual_merge"] == []


@pytest.mark.parametrize(
    "shape,rel",
    [
        ("script", "train.py"),
        ("notebook", "notebooks/experiment.ipynb"),
    ],
)
def test_shape_scaffold_is_a_discoverable_register_run(
    tmp_path: Path, shape: str, rel: str
) -> None:
    """Both shapes produce a discoverable ``@register_run`` function.

    The point of the bilingual on-ramp: the framework's contract is the
    decorated function, not the file format. ``discover_runs`` AST-walks
    ``.py`` and ``.ipynb`` indifferently, so both scaffolds satisfy it.
    """
    data = build_template(repo_dir=tmp_path, shape=shape)
    assert rel in data["written"], rel
    seed = tmp_path / rel
    assert seed.is_file(), seed

    runs = discover_runs(seed)
    assert [r.name for r in runs] == ["run"], (shape, runs)


def test_shape_script_is_default(tmp_path: Path) -> None:
    """No --shape flag means train.py — the script shape is the default."""
    data = build_template(repo_dir=tmp_path)
    assert "train.py" in data["written"]
    assert not (tmp_path / "notebooks" / "experiment.ipynb").exists()


def test_invalid_shape_raises_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="shape"):
        build_template(repo_dir=tmp_path, shape="banana")


def test_existing_shape_seed_is_refused_without_force(tmp_path: Path) -> None:
    build_template(repo_dir=tmp_path, shape="script")
    # User has been editing their entry point.
    (tmp_path / "train.py").write_text(
        "from hpc_agent.experiment_kit import register_run\n"
        "@register_run\n"
        "def run(seed: int = 7) -> None:\n"
        "    return None\n",
        encoding="utf-8",
    )

    data = build_template(repo_dir=tmp_path, shape="script")
    assert "train.py" in data["skipped"]
    # User edits preserved.
    assert "seed: int = 7" in (tmp_path / "train.py").read_text()

    data = build_template(repo_dir=tmp_path, shape="script", force=True)
    assert "train.py" in data["written"]
    assert "seed: int = 7" not in (tmp_path / "train.py").read_text()


def test_hpc_assets_self_heal_root_files_refused(tmp_path: Path) -> None:
    build_template(repo_dir=tmp_path)
    # User edits a root file; a stale .hpc/ asset is corrupted.
    (tmp_path / "conftest.py").write_text("# my edits\n", encoding="utf-8")
    (tmp_path / ".hpc" / "scaffold.py").write_text("# stale\n", encoding="utf-8")

    data = build_template(repo_dir=tmp_path)

    # Root file refused — user edits preserved.
    assert "conftest.py" in data["skipped"]
    assert (tmp_path / "conftest.py").read_text() == "# my edits\n"
    # .hpc/ asset re-injected (self-healing).
    assert "# stale" not in (tmp_path / ".hpc" / "scaffold.py").read_text()
    assert ".hpc/scaffold.py" in data["framework_files"]


def test_force_overwrites_root_files(tmp_path: Path) -> None:
    build_template(repo_dir=tmp_path)
    (tmp_path / "conftest.py").write_text("# my edits\n", encoding="utf-8")

    data = build_template(repo_dir=tmp_path, force=True)

    assert "conftest.py" in data["written"]
    assert "# my edits" not in (tmp_path / "conftest.py").read_text()


def test_existing_makefile_gets_include_appended(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text("all:\n\techo hi\n", encoding="utf-8")

    data = build_template(repo_dir=tmp_path)
    text = makefile.read_text()
    assert "all:" in text  # original target preserved
    assert "include .hpc/template.mk" in text
    assert "Makefile" in data["merged"]

    # Re-running does not append the include twice.
    build_template(repo_dir=tmp_path)
    assert makefile.read_text().count("include .hpc/template.mk") == 1


def test_existing_pyproject_is_never_clobbered(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "mine"\n'
    pyproject.write_text(original, encoding="utf-8")

    data = build_template(repo_dir=tmp_path)

    assert pyproject.read_text() == original
    assert "pyproject.toml" in data["needs_manual_merge"]
    assert (tmp_path / ".hpc" / "pyproject-fragment.toml").is_file()


def test_missing_repo_dir_raises_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="does not exist"):
        build_template(repo_dir=tmp_path / "nonexistent")


def test_injected_assets_parse(tmp_path: Path) -> None:
    build_template(repo_dir=tmp_path)
    # The injected Python is syntactically valid.
    ast.parse((tmp_path / ".hpc" / "scaffold.py").read_text())
    ast.parse((tmp_path / "conftest.py").read_text())
    # The injected YAML parses.
    yaml.safe_load((tmp_path / ".github" / "workflows" / "ci.yml").read_text())
    yaml.safe_load((tmp_path / ".pre-commit-config.yaml").read_text())


def test_notebook_skeleton_is_a_discoverable_register_run(tmp_path: Path) -> None:
    skeleton = (
        hpc_agent._PACKAGE_ROOT
        / "incorporation"
        / "build"
        / "scaffolds"
        / "experiment.ipynb.tmpl"
    )
    nb = tmp_path / "experiment.ipynb"
    nb.write_text(skeleton.read_text(encoding="utf-8"), encoding="utf-8")

    # discover_runs scans the notebook natively (the exported .py inlines
    # the runtime and no longer carries the hpc_agent.experiment_kit import).
    runs = discover_runs(nb)
    assert [r.name for r in runs] == ["run"]

    # And it exports to a self-contained executor.
    out = tmp_path / "experiment.py"
    export_notebook(nb, out)
    assert "def register_run(" in out.read_text()
