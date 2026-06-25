"""Tests for the ``decorate-entry-point`` AST line-splice verb.

The load-bearing property: the verb inserts exactly the import + ``@register_run``
and leaves every other byte of the file untouched — the LLM-``Edit`` failure mode
(rewriting the body) is structurally impossible here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.incorporation.decorate_entry_point import decorate_entry_point

_KWARG_FN = '''\
"""A scaffolded experiment."""

from __future__ import annotations

import random


def run(seed: int = 0, n_samples: int = 1000) -> dict:
    """Estimate something."""
    rng = random.Random(seed)
    total = sum(rng.random() for _ in range(n_samples))
    return {"mean": total / n_samples, "seed": seed}


if __name__ == "__main__":
    print(run())
'''


def _write(tmp_path: Path, text: str, name: str = "train.py") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8", newline="")
    return p


def test_decorates_and_leaves_body_byte_identical(tmp_path: Path) -> None:
    p = _write(tmp_path, _KWARG_FN)
    before = p.read_text(encoding="utf-8").splitlines()

    out = decorate_entry_point(path=str(p), function_name="run")

    assert out["decorated"] is True
    assert out["already_decorated"] is False
    assert out["import_added"] is True
    assert out["lines_changed"] == 2

    after = p.read_text(encoding="utf-8")
    after_lines = after.splitlines()
    # Exactly two lines added.
    assert len(after_lines) == len(before) + 2
    assert "from hpc_agent import register_run" in after_lines
    assert "@register_run" in after_lines
    # Every original line still present, in order, byte-identical.
    added = {"from hpc_agent import register_run", "@register_run"}
    assert [ln for ln in after_lines if ln not in added] == before
    # The decorator sits immediately above the def.
    assert after_lines[after_lines.index("@register_run") + 1].startswith("def run(")
    # Still parses; the function body is unchanged.
    tree = ast.parse(after)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")
    assert any(isinstance(d, ast.Name) and d.id == "register_run" for d in fn.decorator_list)


def test_idempotent_second_call_is_noop(tmp_path: Path) -> None:
    p = _write(tmp_path, _KWARG_FN)
    decorate_entry_point(path=str(p), function_name="run")
    once = p.read_text(encoding="utf-8")

    out = decorate_entry_point(path=str(p), function_name="run")

    assert out["already_decorated"] is True
    assert out["decorated"] is False
    assert out["lines_changed"] == 0
    assert p.read_text(encoding="utf-8") == once  # byte-identical, no second insert


def test_import_already_present_only_adds_decorator(tmp_path: Path) -> None:
    text = (
        "from hpc_agent import register_run\n\n\n"
        "def run(seed: int = 0) -> dict:\n"
        "    return {'seed': seed}\n"
    )
    p = _write(tmp_path, text)
    out = decorate_entry_point(path=str(p), function_name="run")
    assert out["import_added"] is False
    assert out["lines_changed"] == 1
    after = p.read_text(encoding="utf-8")
    assert after.count("from hpc_agent import register_run") == 1
    assert "@register_run" in after


def test_future_import_ordering(tmp_path: Path) -> None:
    p = _write(tmp_path, _KWARG_FN)
    decorate_entry_point(path=str(p), function_name="run")
    after = p.read_text(encoding="utf-8")
    lines = after.splitlines()
    # The new import lands AFTER `from __future__ import annotations` and the
    # docstring, so the file still parses (a __future__ import before the
    # docstring or another import would be a SyntaxError).
    assert lines.index("from __future__ import annotations") < lines.index(
        "from hpc_agent import register_run"
    )
    ast.parse(after)  # must not raise


def test_crlf_is_preserved(tmp_path: Path) -> None:
    text = _KWARG_FN.replace("\n", "\r\n")
    p = tmp_path / "train.py"
    p.write_bytes(text.encode("utf-8"))
    decorate_entry_point(path=str(p), function_name="run")
    raw = p.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")  # no bare LF introduced
    assert b"@register_run\r\n" in raw


def test_outermost_stacking_over_nonconsuming_decorator(tmp_path: Path) -> None:
    text = (
        "import functools\n\n\n"
        "@functools.cache\n"
        "def run(seed: int = 0) -> dict:\n"
        "    return {'seed': seed}\n"
    )
    p = _write(tmp_path, text)
    decorate_entry_point(path=str(p), function_name="run")
    lines = p.read_text(encoding="utf-8").splitlines()
    # @register_run is inserted ABOVE the existing decorator (outermost).
    assert lines.index("@register_run") < lines.index("@functools.cache")


@pytest.mark.parametrize(
    "decorator",
    ["@hydra.main(version_base=None)", "@hydra.main", "@click.command()", "@app.command()"],
)
def test_refuses_signature_rewriting_decorator(tmp_path: Path, decorator: str) -> None:
    text = f"import x\n\n\n{decorator}\ndef run(cfg) -> None:\n    return None\n"
    p = _write(tmp_path, text)
    before = p.read_bytes()
    with pytest.raises(errors.SpecInvalid):
        decorate_entry_point(path=str(p), function_name="run")
    assert p.read_bytes() == before  # refused → no write


def test_function_not_found(tmp_path: Path) -> None:
    p = _write(tmp_path, _KWARG_FN)
    with pytest.raises(errors.SpecInvalid):
        decorate_entry_point(path=str(p), function_name="nonexistent")


def test_syntax_error_refused(tmp_path: Path) -> None:
    p = _write(tmp_path, "def run(:\n    pass\n")
    with pytest.raises(errors.SpecInvalid):
        decorate_entry_point(path=str(p), function_name="run")


def test_missing_file_refused(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        decorate_entry_point(path=str(tmp_path / "nope.py"), function_name="run")
