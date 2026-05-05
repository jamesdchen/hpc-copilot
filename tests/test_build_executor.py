"""Tests backing executor discovery + the starter template.

The standalone ``/build-executor`` slash command was retired once
``/submit-hpc`` Step 1 absorbed the scaffolding interview, but the
underlying primitives this file tests are still load-bearing:

* ``discover_executors`` — used by ``/submit-hpc`` Step 1 and the
  ``hpc-mapreduce build-executor`` CLI subcommand (still exposed for
  MARs orchestrators) to enumerate runnable executors in an experiment
  repo. Recognizes both contracts: new (``compute(args)`` exported)
  and old (``__main__`` + argparse).
* Template parseability — ``templates/starters/executor_template.py``
  must remain importable Python so the post-copy smoke test can
  succeed.
* Dry-run scaffold — simulates the new-executor flow by copying
  ``executor_template.py`` into a fresh tmp directory and verifying
  the file lands at the user-chosen path and remains call-able.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import shutil
import sys
from pathlib import Path

from claude_hpc import _PACKAGE_ROOT
from claude_hpc.state.discover import (
    ExecutorInfo,
    discover_executors,
    is_executor_source,
)


def _load_template_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"_loaded_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "mock_experiment"
TEMPLATES_DIR = _PACKAGE_ROOT / "mapreduce" / "templates" / "starters"


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

    def test_recognizes_new_compute_contract(self, tmp_path: Path) -> None:
        """New contract: a file exporting ``compute(args)`` is an executor
        even without a ``__main__`` guard or CLI framework import. The
        dispatcher in .hpc/cli.py provides argv parsing and entry point."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "ml_ridge.py").write_text("def compute(args):\n    pass\n")
        infos = discover_executors(tmp_path)
        assert [i.name for i in infos] == ["ml_ridge"]
        info = infos[0]
        assert info.has_compute_function is True
        assert info.has_main_guard is False
        assert info.cli_framework is None
        assert info.is_executor is True

    def test_rejects_zero_arg_compute(self, tmp_path: Path) -> None:
        """``def compute():`` (no positional args) does not match the
        contract — most likely a coincidence, not the dispatcher entry
        point. The dispatcher always calls ``compute(args)``."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "noop.py").write_text("def compute():\n    pass\n")
        assert discover_executors(tmp_path) == []


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


def test_executor_template_is_valid_python() -> None:
    path = TEMPLATES_DIR / "executor_template.py"
    assert path.is_file(), f"missing template: {path}"
    # Must parse cleanly — /submit-hpc Step 1 scaffolding copies this verbatim.
    ast.parse(path.read_text(encoding="utf-8"))


def test_tasks_example_is_valid_python_and_exposes_total_resolve() -> None:
    """The canonical .hpc/tasks.py reference must parse cleanly and expose
    total() / resolve(task_id) — the agent reads it as the teaching
    example during /submit Step 6.
    """
    import importlib.util

    path = TEMPLATES_DIR.parent / "tasks_example.py"
    assert path.is_file(), f"missing canonical example: {path}"
    source = path.read_text(encoding="utf-8")
    ast.parse(source)
    spec = importlib.util.spec_from_file_location("tasks_example_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.total)
    assert callable(mod.resolve)
    assert mod.total() > 0
    assert isinstance(mod.resolve(0), dict)


def test_executor_template_is_self_classified_as_executor() -> None:
    """The scaffold itself must pass `is_executor_source` so a freshly-scaffolded
    file passes `/submit`'s discovery test on the first try."""
    source = (TEMPLATES_DIR / "executor_template.py").read_text(encoding="utf-8")
    assert is_executor_source(source) is True


def test_executor_template_compute_runs_standalone(tmp_path: Path) -> None:
    """Under the new contract the template has no ``__main__`` block (the
    .hpc/cli.py dispatcher is the entry point). What every scaffolded
    executor must still satisfy is: ``compute(args)`` is callable with
    a minimal Namespace and writes the per-task output file. This is
    the standalone smoke test for fresh-template state."""
    mod = _load_template_module(TEMPLATES_DIR / "executor_template.py")
    out = tmp_path / "out.csv"
    mod.compute(argparse.Namespace(output_file=str(out)))
    assert out.is_file()


# ─── Dry-run scaffold ────────────────────────────────────────────────────


def test_scaffold_from_template_copies_into_experiment_repo(tmp_path: Path) -> None:
    """Simulates the /submit-hpc Step 1 scaffold path: user picks a new path
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

    # And it must still expose a callable compute(args) post-copy.
    mod = _load_template_module(target)
    out = tmp_path / "scaffold_out.csv"
    mod.compute(argparse.Namespace(output_file=str(out)))
    assert out.is_file()
