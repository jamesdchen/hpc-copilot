"""Tests for ``hpc_agent.incorporation.build.template`` (Layer 4 — scaffold injection)."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import pytest
import yaml

import hpc_agent
from hpc_agent import errors
from hpc_agent.incorporation.build.template import build_template
from hpc_agent.incorporation.template import discover_runs, export_notebook

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

    assert ".hpc/template.mk" in data["framework_files"]
    assert ".hpc/scaffold.py" in data["framework_files"]
    assert "Makefile" in data["written"]
    assert data["skipped"] == []
    assert data["needs_manual_merge"] == []


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
        / "template"
        / "scaffold"
        / "experiment.ipynb.tmpl"
    )
    nb = tmp_path / "experiment.ipynb"
    nb.write_text(skeleton.read_text(encoding="utf-8"), encoding="utf-8")

    # discover_runs scans the notebook natively (the exported .py inlines
    # the runtime and no longer carries the hpc_agent.incorporation.template import).
    runs = discover_runs(nb)
    assert [r.name for r in runs] == ["run"]

    # And it exports to a self-contained executor.
    out = tmp_path / "experiment.py"
    export_notebook(nb, out)
    assert "def register_run(" in out.read_text()
