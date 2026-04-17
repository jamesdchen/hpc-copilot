"""Tests backing the `/build-executor` slash command.

Covers:

* ``discover_executors`` — the shared helper used by both ``/submit`` and
  ``/build-executor`` to enumerate runnable scripts in an experiment repo.
* Template parseability — the files under ``templates/`` that
  ``/build-executor`` copies must remain importable Python so the smoke
  test in Step 4 can succeed.
* Dry-run scaffold — simulates mode (b) of the command by copying
  ``executor_template.py`` into a fresh tmp directory and verifying the
  file lands at the user-chosen path.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hpc_mapreduce import _PACKAGE_ROOT
from hpc_mapreduce.job.discover import (
    ExecutorInfo,
    discover_executors,
    is_executor_source,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "mock_experiment"
TEMPLATES_DIR = _PACKAGE_ROOT / "templates"


# ─── discover_executors ───────────────────────────────────────────────────


class TestDiscoverExecutors:
    def test_finds_executor_a(self) -> None:
        infos = discover_executors(FIXTURE_ROOT)
        names = [i.name for i in infos]
        assert "executor_a" in names, f"expected executor_a, got {names}"

    def test_single_executor_returned(self) -> None:
        infos = discover_executors(FIXTURE_ROOT)
        # _notes.py has no main guard / no argparse → must be excluded
        assert [i.name for i in infos] == ["executor_a"]

    def test_executor_info_shape(self) -> None:
        info = discover_executors(FIXTURE_ROOT)[0]
        assert isinstance(info, ExecutorInfo)
        assert info.cli_framework == "argparse"
        assert info.has_main_guard is True
        assert info.is_executor is True
        assert info.path.name == "executor_a.py"

    def test_empty_when_root_missing(self, tmp_path: Path) -> None:
        assert discover_executors(tmp_path / "nope") == []

    def test_empty_when_no_executors(self, tmp_path: Path) -> None:
        (tmp_path / "executors").mkdir()
        (tmp_path / "executors" / "util.py").write_text("def f():\n    return 1\n")
        assert discover_executors(tmp_path) == []

    def test_custom_search_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "custom").mkdir()
        (tmp_path / "custom" / "exec.py").write_text(
            "import argparse\n"
            "def main():\n"
            "    argparse.ArgumentParser().parse_args()\n"
            'if __name__ == "__main__":\n'
            "    main()\n"
        )
        infos = discover_executors(tmp_path, search_dirs=("custom",))
        assert [i.name for i in infos] == ["exec"]

    def test_recursive_walk(self, tmp_path: Path) -> None:
        nested = tmp_path / "executors" / "nested"
        nested.mkdir(parents=True)
        src = (
            "import argparse\n"
            'if __name__ == "__main__":\n'
            "    argparse.ArgumentParser().parse_args()\n"
        )
        (nested / "deep.py").write_text(src)
        flat_only = discover_executors(tmp_path, recursive=False)
        assert flat_only == []
        deep = discover_executors(tmp_path, recursive=True)
        assert [i.name for i in deep] == ["deep"]

    def test_reverse_main_guard_order(self, tmp_path: Path) -> None:
        """``"__main__" == __name__`` also counts."""
        (tmp_path / "executors").mkdir()
        (tmp_path / "executors" / "rev.py").write_text(
            'import argparse\nif "__main__" == __name__:\n    argparse.ArgumentParser()\n'
        )
        infos = discover_executors(tmp_path)
        assert [i.name for i in infos] == ["rev"]


class TestIsExecutorSource:
    def test_positive(self) -> None:
        src = (
            "import argparse\n"
            'if __name__ == "__main__":\n'
            "    argparse.ArgumentParser().parse_args()\n"
        )
        assert is_executor_source(src) is True

    def test_missing_main_guard(self) -> None:
        assert is_executor_source("import argparse\n") is False

    def test_missing_cli_framework(self) -> None:
        src = 'if __name__ == "__main__":\n    pass\n'
        assert is_executor_source(src) is False

    def test_syntax_error_returns_false(self) -> None:
        assert is_executor_source("def broken(:\n") is False

    def test_click_is_recognized(self) -> None:
        src = "import click\nif __name__ == \"__main__\":\n    click.Command('x').main([])\n"
        assert is_executor_source(src) is True


# ─── Template parseability ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "template_name",
    ["executor_template.py", "chunking_shim.py", "shim_template.py"],
)
def test_template_is_valid_python(template_name: str) -> None:
    path = TEMPLATES_DIR / template_name
    assert path.is_file(), f"missing template: {path}"
    source = path.read_text(encoding="utf-8")
    # Must parse cleanly — /build-executor copies this verbatim.
    ast.parse(source)


def test_executor_template_is_self_classified_as_executor() -> None:
    """The scaffold itself must pass `is_executor_source` so a freshly-scaffolded
    file passes `/submit`'s discovery test on the first try."""
    source = (TEMPLATES_DIR / "executor_template.py").read_text(encoding="utf-8")
    assert is_executor_source(source) is True


def test_executor_template_help_runs() -> None:
    """Step 4 of the command spec runs `--help` on the scaffold; make sure
    that contract holds before any customization happens."""
    result = subprocess.run(
        [sys.executable, str(TEMPLATES_DIR / "executor_template.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--output-file" in result.stdout


# ─── Dry-run scaffold ────────────────────────────────────────────────────


def test_scaffold_from_template_copies_into_experiment_repo(tmp_path: Path) -> None:
    """Simulates mode (b): user invokes /build-executor and picks a new path
    inside their experiment repo. The template must land at that exact path
    and remain runnable."""
    experiment_repo = tmp_path / "my_experiment"
    experiment_repo.mkdir()
    target = experiment_repo / "executors" / "my_new_model.py"
    target.parent.mkdir(parents=True)

    shutil.copyfile(TEMPLATES_DIR / "executor_template.py", target)

    assert target.is_file()
    assert target.read_text() == (TEMPLATES_DIR / "executor_template.py").read_text()

    # Post-copy discovery: /submit run on the experiment repo must now see it.
    infos = discover_executors(experiment_repo)
    assert [i.name for i in infos] == ["my_new_model"]

    # And it must still pass --help.
    result = subprocess.run(
        [sys.executable, str(target), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
