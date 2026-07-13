"""Tests for ``hpc_agent.incorporation.build.template`` (Layer 4 — scaffold injection)."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import pytest
import yaml

import hpc_agent
from hpc_agent import errors
from hpc_agent.experiment_kit import discover_runs, export_cells
from hpc_agent.incorporation.build.template import build_template
from hpc_agent.state.audit_source import CELL_DELIMITER, parse_percent_source, percent_cell_sources

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
        ("notebook", "notebooks/experiment.py"),
    ],
)
def test_shape_scaffold_is_a_discoverable_register_run(
    tmp_path: Path, shape: str, rel: str
) -> None:
    """Both shapes produce a discoverable ``@register_run`` function.

    The point of the bilingual on-ramp: the framework's contract is the
    decorated function, not the file shape. Both shapes are ``.py`` now —
    the notebook shape is jupytext percent format (``# %%`` cells), and
    ``discover_runs`` AST-walks it like any Python file (the delimiters
    are comments).
    """
    data = build_template(repo_dir=tmp_path, shape=shape)
    assert rel in data["written"], rel
    seed = tmp_path / rel
    assert seed.is_file(), seed

    runs = discover_runs(seed)
    assert [r.name for r in runs] == ["run"], (shape, runs)


def test_script_main_routes_through_compute_result_writer(tmp_path: Path) -> None:
    """#16 (proving run #5): the rendered ``train.py`` ``__main__`` must route
    through the injected ``compute()`` result-writer, NOT call ``run()`` and only
    ``print()``. An executor that only prints exits 0 while writing no
    ``metrics.json`` — the dispatcher then has nothing to promote and the canary
    greens on a run that produced nothing to aggregate."""
    build_template(repo_dir=tmp_path, shape="script")
    src = (tmp_path / "train.py").read_text(encoding="utf-8")

    # Isolate the __main__ block so the assertions are about the entry point.
    main_block = src.split('if __name__ == "__main__":', 1)[1]
    assert "compute(args)" in main_block
    assert "print(run(" not in main_block  # the bypassing anti-pattern is gone
    # The result artifact defaults to $RESULT_DIR/metrics.json (the dispatcher
    # sets RESULT_DIR per task) so a hand-run CLI writes the framework's result.
    assert "output_file" in main_block
    assert "metrics.json" in main_block
    assert "RESULT_DIR" in main_block
    # Still valid, importable Python.
    ast.parse(src)


def test_shape_script_is_default(tmp_path: Path) -> None:
    """No --shape flag means train.py — the script shape is the default."""
    data = build_template(repo_dir=tmp_path)
    assert "train.py" in data["written"]
    assert not (tmp_path / "notebooks" / "experiment.py").exists()


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


def test_existing_gitignore_gets_block_appended(tmp_path: Path) -> None:
    """An existing .gitignore without the marker gains the hpc-agent block,
    keeping its own entries — the non-destructive merge path (sibling of the
    Makefile-include merge, but for ignore rules)."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.pyc\n__pycache__/\n", encoding="utf-8")

    data = build_template(repo_dir=tmp_path)
    text = gitignore.read_text()
    assert "*.pyc" in text  # original entries preserved
    assert "hpc-agent experiment-template" in text  # generated block merged in
    assert ".gitignore" in data["merged"]
    assert ".gitignore" not in data["written"]  # merged, not freshly written

    # Idempotent: the marker is now present, so a re-run skips rather than
    # appending the block a second time.
    data2 = build_template(repo_dir=tmp_path)
    assert ".gitignore" in data2["skipped"]
    assert gitignore.read_text().count("hpc-agent experiment-template") == 1


def test_absent_gitignore_is_written_fresh(tmp_path: Path) -> None:
    """No .gitignore at all → the starter block is written, not merged."""
    data = build_template(repo_dir=tmp_path)
    gitignore = tmp_path / ".gitignore"
    assert gitignore.is_file()
    assert "hpc-agent experiment-template" in gitignore.read_text()
    assert ".gitignore" in data["written"]
    assert ".gitignore" not in data["merged"]


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


def test_notebook_seed_is_percent_format(tmp_path: Path) -> None:
    """The notebook shape emits a jupytext percent-format ``.py`` that the
    audit substrate's one parser reads — never a raw ``.ipynb`` (the
    un-auditable format the notebook-audit doctrine forbids as an
    LLM-drafted source)."""
    build_template(repo_dir=tmp_path, shape="notebook")
    seed = tmp_path / "notebooks" / "experiment.py"
    text = seed.read_text(encoding="utf-8")

    # Valid Python (the delimiters are comments) and genuinely cellular.
    ast.parse(text)
    assert CELL_DELIMITER in text
    # The one percent-format parser accepts it (no sections yet — the seed
    # carries no audit markers; module_sha still fingerprints the file).
    mod = parse_percent_source(text)
    assert mod.module_sha
    assert mod.sections == ()
    # Cell segmentation sees the import / def / scratch cells.
    assert len(percent_cell_sources(text)) >= 3


def test_notebook_skeleton_is_a_discoverable_register_run(tmp_path: Path) -> None:
    skeleton = (
        hpc_agent._PACKAGE_ROOT / "incorporation" / "build" / "scaffolds" / "experiment.py.tmpl"
    )
    nb = tmp_path / "notebooks" / "experiment.py"
    nb.parent.mkdir(parents=True)
    nb.write_text(skeleton.read_text(encoding="utf-8"), encoding="utf-8")

    # discover_runs scans the percent .py natively (the delimiters are
    # ordinary comments to the AST walk).
    runs = discover_runs(nb)
    assert [r.name for r in runs] == ["run"]

    # And its cells export to a self-contained executor through the same
    # strict-AST core the ipynb path uses.
    out = tmp_path / "exported.py"
    export_cells(percent_cell_sources(nb.read_text(encoding="utf-8")), out)
    text = out.read_text()
    assert "def register_run(" in text  # inlined runtime
    assert "run(alpha=2.0)" not in text  # scratch cell dropped


def test_scaffold_py_new_scaffolds_percent_notebook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The injected ``.hpc/scaffold.py new <name>`` writes a percent-format
    ``notebooks/executors/<name>.py`` (never an .ipynb) and refuses to
    overwrite an existing one."""
    import importlib.util

    build_template(repo_dir=tmp_path)
    monkeypatch.chdir(tmp_path)

    spec = importlib.util.spec_from_file_location(
        "hpc_scaffold_under_test", tmp_path / ".hpc" / "scaffold.py"
    )
    assert spec is not None and spec.loader is not None
    scaffold = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scaffold)

    assert scaffold.main(["new", "sweep"]) == 0
    dest = tmp_path / "notebooks" / "executors" / "sweep.py"
    assert dest.is_file()
    skeleton = (
        hpc_agent._PACKAGE_ROOT / "incorporation" / "build" / "scaffolds" / "experiment.py.tmpl"
    )
    assert dest.read_text(encoding="utf-8") == skeleton.read_text(encoding="utf-8")
    assert [r.name for r in discover_runs(dest)] == ["run"]

    # Refuses to clobber the user's in-progress notebook.
    dest.write_text("# my edits\n", encoding="utf-8")
    assert scaffold.main(["new", "sweep"]) == 1
    assert dest.read_text(encoding="utf-8") == "# my edits\n"
