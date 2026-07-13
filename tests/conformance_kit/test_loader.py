"""Adapter loading — happy path + every failure mode (K1 machinery unit test).

``load_adapter`` is the pytest-free core of ``--harness-adapter``; these drive
it directly (no kit run needed) so the dotted-path resolution and its error
envelope are pinned independently of the conformance modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent.conformance._loader import AdapterLoadError, load_adapter

if TYPE_CHECKING:
    from pathlib import Path


def _write_module(tmp_path: Path, name: str, body: str) -> None:
    (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")


def test_load_adapter_happy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_module(
        tmp_path,
        "adp_ok",
        "class _A:\n    name = 'demo-harness'\n\ndef build():\n    return _A()\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    adapter = load_adapter("adp_ok:build")
    assert adapter.name == "demo-harness"


def test_malformed_spec_no_colon() -> None:
    with pytest.raises(AdapterLoadError, match="module.path:factory"):
        load_adapter("adp_ok")


def test_malformed_spec_empty_halves() -> None:
    with pytest.raises(AdapterLoadError, match="module.path:factory"):
        load_adapter(":build")
    with pytest.raises(AdapterLoadError, match="module.path:factory"):
        load_adapter("mod:")


def test_unimportable_module() -> None:
    with pytest.raises(AdapterLoadError, match="cannot import module"):
        load_adapter("hpc_agent_no_such_module_xyz:build")


def test_missing_factory_attribute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_module(tmp_path, "adp_noattr", "x = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(AdapterLoadError, match="has no attribute 'build'"):
        load_adapter("adp_noattr:build")


def test_non_callable_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_module(tmp_path, "adp_notcallable", "build = 5\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(AdapterLoadError, match="is not callable"):
        load_adapter("adp_notcallable:build")


def test_factory_that_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_module(
        tmp_path,
        "adp_raises",
        "def build():\n    raise RuntimeError('boom')\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(AdapterLoadError, match="raised RuntimeError: boom"):
        load_adapter("adp_raises:build")
