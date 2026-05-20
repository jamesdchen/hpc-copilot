"""``discover_runs`` AST walk + decorator-alias resolution (Layer 1)."""

from __future__ import annotations

from pathlib import Path

from hpc_agent.template import discover_runs


def test_discovers_bare_decorator(tmp_path: Path) -> None:
    (tmp_path / "exp.py").write_text(
        "from hpc_agent.template import register_run\n"
        "\n"
        "@register_run\n"
        "def run(alpha: float = 1.0, mode: str = 'am'):\n"
        "    return {}\n"
    )
    runs = discover_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].name == "run"
    assert runs[0].gpu is False
    names = {f.name for f in runs[0].flags}
    assert names == {"alpha", "mode"}


def test_discovers_aliased_decorator(tmp_path: Path) -> None:
    (tmp_path / "exp.py").write_text(
        "from hpc_agent.template import register_run as rr\n"
        "\n"
        "@rr(gpu=True)\n"
        "def run(epochs: int = 10):\n"
        "    return {}\n"
    )
    runs = discover_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].gpu is True


def test_discovers_attribute_decorator(tmp_path: Path) -> None:
    (tmp_path / "exp.py").write_text(
        "import hpc_agent.template\n"
        "\n"
        "@hpc_agent.template.register_run\n"
        "def run(x: int = 1):\n"
        "    return {}\n"
    )
    runs = discover_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].name == "run"


def test_discovers_module_aliased_decorator(tmp_path: Path) -> None:
    (tmp_path / "exp.py").write_text(
        "from hpc_agent import template\n"
        "\n"
        "@template.register_run\n"
        "def run(x: int = 1):\n"
        "    return {}\n"
    )
    runs = discover_runs(tmp_path)
    assert len(runs) == 1


def test_ignores_unrelated_and_undecorated(tmp_path: Path) -> None:
    (tmp_path / "util.py").write_text(
        "def register_run(f):\n    return f\n"  # a same-named local decorator
        "\n"
        "@register_run\n"
        "def not_a_run():\n    return 1\n"
    )
    (tmp_path / "plain.py").write_text("def helper():\n    return 1\n")
    assert discover_runs(tmp_path) == []


def test_ast_signature_flag_mapping(tmp_path: Path) -> None:
    (tmp_path / "exp.py").write_text(
        "from hpc_agent.template import register_run\n"
        "from typing import Literal\n"
        "\n"
        "@register_run\n"
        "def run(a: int, b: float = 2.0, verbose: bool = False, "
        "mode: Literal['am', 'pm'] = 'am', tags: list[str] = None):\n"
        "    return {}\n"
    )
    runs = discover_runs(tmp_path)
    flags = {f.name: f for f in runs[0].flags}
    assert flags["a"].type is int and flags["a"].required is True
    assert flags["b"].type is float and flags["b"].default == 2.0
    assert flags["verbose"].action == "store_true"
    assert flags["mode"].choices == ("am", "pm")
    assert flags["tags"].nargs == "+" and flags["tags"].type is str
