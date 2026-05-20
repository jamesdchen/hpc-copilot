"""Signature → Flag synthesis (Layer 1)."""

from __future__ import annotations

from typing import Literal, Optional

import pytest

from hpc_agent.template import flags_for_run, flags_from_signature


def _by_name(flags: list) -> dict:
    return {f.name: f for f in flags}


def test_scalar_types_and_required() -> None:
    def run(a: int, b: float, c: str = "x") -> dict:
        return {}

    flags = _by_name(flags_from_signature(run))
    assert flags["a"].type is int and flags["a"].required is True
    assert flags["b"].type is float and flags["b"].required is True
    assert flags["c"].type is str and flags["c"].required is False
    assert flags["c"].default == "x"


def test_bool_default_false_is_store_true() -> None:
    def run(verbose: bool = False) -> dict:
        return {}

    flag = flags_from_signature(run)[0]
    assert flag.action == "store_true"
    assert flag.required is False


def test_bool_default_true_is_store_false() -> None:
    def run(enabled: bool = True) -> dict:
        return {}

    assert flags_from_signature(run)[0].action == "store_false"


def test_optional_unwraps_and_is_not_required() -> None:
    def run(x: Optional[int] = None) -> dict:  # noqa: UP045
        return {}

    flag = flags_from_signature(run)[0]
    assert flag.type is int
    assert flag.required is False


def test_pep604_optional_with_no_default_is_still_optional() -> None:
    def run(x: int | None) -> dict:
        return {}

    flag = flags_from_signature(run)[0]
    assert flag.type is int
    assert flag.required is False


def test_list_becomes_nargs_plus() -> None:
    def run(xs: list[int]) -> dict:
        return {}

    flag = flags_from_signature(run)[0]
    assert flag.type is int
    assert flag.nargs == "+"


def test_literal_becomes_choices() -> None:
    def run(mode: Literal["am", "pm", "all"] = "am") -> dict:
        return {}

    flag = flags_from_signature(run)[0]
    assert flag.choices == ("am", "pm", "all")
    assert flag.type is str


def test_missing_annotation_defaults_to_str_with_warning() -> None:
    def run(mystery) -> dict:  # type: ignore[no-untyped-def]
        return {}

    with pytest.warns(UserWarning, match="no type annotation"):
        flag = flags_from_signature(run)[0]
    assert flag.type is str


def test_flags_for_run_dedupes_against_generic_args() -> None:
    # ``seed`` collides with generic_args(); the signature must win.
    def run(seed: int = 7, horizon: int = 1) -> dict:
        return {}

    names = [f.name for f in flags_for_run(run)]
    assert names.count("seed") == 1
    assert "output_file" in names  # generic_args still contributes the rest
    assert "halo" in names  # planner flag injected
    assert "horizon" in names
    seed_flag = next(f for f in flags_for_run(run) if f.name == "seed")
    assert seed_flag.default == 7  # the signature's default, not generic_args' 42


def test_flags_for_run_gpu_adds_gpu_args() -> None:
    def run(epochs: int = 10) -> dict:
        return {}

    names = [f.name for f in flags_for_run(run, gpu=True)]
    assert "gpu_count" in names
    assert "batch_size" in names
