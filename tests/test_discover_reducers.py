"""Tests for ``claude_hpc.state.discover.discover_reducers``.

Motivating failure mode: at /aggregate-hpc time the agent writes a
fresh QLIKE / RMSE / etc. aggregator instead of finding the one
already in the repo. The primitive surfaces every candidate so the
slash command can route through a CLI primitive instead of grep'ing
by hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_hpc.state.discover import ReducerInfo, discover_reducers

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_returns_empty_when_no_candidates(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "model.py", "def predict(x): return x")
    assert discover_reducers(tmp_path) == []


def test_filename_stem_match(tmp_path: Path) -> None:
    _write(tmp_path / "scripts" / "qlike.py", '"""QLIKE loss."""\n')
    out = discover_reducers(tmp_path)
    assert len(out) == 1
    assert out[0].name == "qlike"
    assert "name:qlike" in out[0].matches
    assert out[0].docstring == "QLIKE loss."


def test_function_name_match_without_filename_hint(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "metrics.py",
        "def aggregate(result_dir):\n    return {}\n",
    )
    out = discover_reducers(tmp_path)
    assert len(out) == 1
    assert out[0].name == "metrics"
    # "metrics" stem matches a name hint AND the function matches.
    # `metric` (the hint) is a substring of "metrics" (the stem); function name matches too.
    assert any(m.startswith("name:") for m in out[0].matches)
    assert "function:aggregate" in out[0].matches


def test_substring_filename_hint_matches(tmp_path: Path) -> None:
    """Stem-as-substring catches `aggregate_qlike.py`, `compute_qlike.py`, etc."""
    _write(tmp_path / "scripts" / "aggregate_qlike.py", "x = 1\n")
    _write(tmp_path / "scripts" / "compute_rmse.py", "x = 1\n")
    names = [r.name for r in discover_reducers(tmp_path)]
    assert "aggregate_qlike" in names
    assert "compute_rmse" in names


def test_recursive_default_walks_nested_dirs(tmp_path: Path) -> None:
    """Reducers often live nested (e.g. src/eval/qlike.py); recursive=True default."""
    nested = tmp_path / "src" / "eval" / "qlike.py"
    _write(nested, '"""qlike."""\n')
    out = discover_reducers(tmp_path)
    assert len(out) == 1
    assert out[0].path == nested.resolve()


def test_excludes_framework_dirs(tmp_path: Path) -> None:
    """`.hpc/`, `.git/`, `__pycache__/` are never user code."""
    _write(tmp_path / ".hpc" / "qlike.py", '"""shadow."""\n')
    _write(tmp_path / ".git" / "scoring.py", '"""shadow."""\n')
    _write(tmp_path / "__pycache__" / "aggregate.py", '"""shadow."""\n')
    _write(tmp_path / "scripts" / "qlike.py", '"""real."""\n')
    out = discover_reducers(tmp_path)
    assert [r.name for r in out] == ["qlike"]


def test_skip_init_dunder(tmp_path: Path) -> None:
    _write(tmp_path / "scripts" / "__init__.py", "")
    _write(tmp_path / "scripts" / "qlike.py", '"""qlike."""\n')
    out = discover_reducers(tmp_path)
    assert [r.name for r in out] == ["qlike"]


def test_multi_signal_hits_sort_first(tmp_path: Path) -> None:
    """A file with both a name hint AND a function hint outranks name-only."""
    _write(
        tmp_path / "scripts" / "score.py",
        "def score(results):\n    return 1.0\n",
    )
    _write(tmp_path / "scripts" / "qlike.py", '"""qlike."""\n')
    out = discover_reducers(tmp_path)
    # score.py has matches: ["name:score", "function:score"] (2 signals).
    # qlike.py has matches: ["name:qlike"] (1 signal).
    assert out[0].name == "score"
    assert out[1].name == "qlike"


def test_dedicated_reducer_dirs_are_searched(tmp_path: Path) -> None:
    """`aggregators/`, `reducers/`, `scoring/` are checked alongside scripts/src."""
    _write(tmp_path / "aggregators" / "rmse.py", '"""rmse."""\n')
    _write(tmp_path / "reducers" / "mae.py", '"""mae."""\n')
    out = discover_reducers(tmp_path)
    names = sorted(r.name for r in out)
    assert names == ["mae", "rmse"]


def test_zero_arg_function_does_not_match(tmp_path: Path) -> None:
    """`def aggregate():` with no params is too generic to be the entry point."""
    _write(
        tmp_path / "src" / "model.py",
        "def aggregate():\n    pass\n",
    )
    assert discover_reducers(tmp_path) == []


def test_syntax_error_skipped(tmp_path: Path) -> None:
    """A broken .py file shouldn't tank the scan."""
    _write(tmp_path / "scripts" / "broken.py", "this is :: not python\n")
    _write(tmp_path / "scripts" / "qlike.py", '"""qlike."""\n')
    out = discover_reducers(tmp_path)
    assert [r.name for r in out] == ["qlike"]


def test_returns_reducer_info_with_path(tmp_path: Path) -> None:
    """Sanity: returned items are ReducerInfo with absolute paths."""
    target = tmp_path / "scripts" / "qlike.py"
    _write(target, "x = 1\n")
    out = discover_reducers(tmp_path)
    assert len(out) == 1
    assert isinstance(out[0], ReducerInfo)
    assert out[0].path == target.resolve()
