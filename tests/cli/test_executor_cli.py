"""Smoke tests for the executor-side CLI helpers.

These pin the public surface used by every auto-generated
``.hpc/tasks.py`` and ``.hpc/cli.py`` — anything that breaks here
breaks every experiment repo's executor invocations.
"""

from __future__ import annotations

import argparse

import pytest

from hpc_agent.executor_cli import (
    build_parser_from_flags,
    flag,
    generic_args,
    gpu_args,
)


def test_flag_underscore_name_becomes_hyphenated_cli() -> None:
    p = argparse.ArgumentParser()
    flag("output_file", str, required=True).add_to(p)
    args = p.parse_args(["--output-file", "out.csv"])
    assert args.output_file == "out.csv"


def test_flag_default_is_set_when_optional() -> None:
    p = argparse.ArgumentParser()
    flag("seed", int, default=42).add_to(p)
    args = p.parse_args([])
    assert args.seed == 42


def test_flag_optional_with_no_default_resolves_to_none() -> None:
    p = argparse.ArgumentParser()
    flag("epochs", int).add_to(p)
    args = p.parse_args([])
    assert args.epochs is None


def test_flag_required_aborts_when_missing() -> None:
    p = argparse.ArgumentParser()
    flag("output_file", str, required=True).add_to(p)
    with pytest.raises(SystemExit):
        p.parse_args([])


def test_flag_choices_enforced() -> None:
    p = argparse.ArgumentParser()
    flag("segment", str, choices=["am", "pm", "all"]).add_to(p)
    args = p.parse_args(["--segment", "am"])
    assert args.segment == "am"
    with pytest.raises(SystemExit):
        p.parse_args(["--segment", "noon"])


def test_generic_args_includes_required_output_file() -> None:
    flags = generic_args()
    by_name = {f.name: f for f in flags}
    assert "output_file" in by_name
    assert by_name["output_file"].required is True
    # The rest are optional and have stable defaults.
    assert by_name["seed"].default == 42
    assert by_name["start"].default == 0
    assert by_name["end"].default == -1


def test_gpu_args_present() -> None:
    flags = gpu_args()
    by_name = {f.name: f for f in flags}
    assert {"gpu_count", "epochs", "batch_size", "learning_rate"} <= set(by_name)
    assert by_name["gpu_count"].default == 1


def test_build_parser_from_flags_accepts_dict_entries() -> None:
    flags = [{"name": "horizon", "type": int, "default": 1}, flag("seed", int, default=42)]
    p = build_parser_from_flags(flags, description="mixed")
    args = p.parse_args(["--horizon", "5"])
    assert args.horizon == 5
    assert args.seed == 42


def test_build_parser_from_flags_rejects_bad_entry_type() -> None:
    with pytest.raises(TypeError, match="must be Flag instances or dicts"):
        build_parser_from_flags(["not a flag"])  # type: ignore[list-item]


def test_realistic_tasks_py_shape() -> None:
    """End-to-end: the FLAGS dict shape an auto-generated tasks.py would have."""
    FLAGS = {
        "src.ml_ridge": [
            *generic_args(),
            flag("horizon", int, default=1),
            flag("segment", str, choices=("am", "pm", "all")),
        ],
        "src.dl_patchts": [
            *generic_args(),
            *gpu_args(),
            flag("horizon", int, default=1),
        ],
    }
    # Each per-executor parser is built independently, no flag bleed.
    ridge = build_parser_from_flags(FLAGS["src.ml_ridge"], description="src.ml_ridge")
    args = ridge.parse_args(["--output-file", "r.csv", "--horizon", "5", "--segment", "pm"])
    assert args.horizon == 5
    assert args.segment == "pm"
    # ml_ridge parser does NOT know about --epochs.
    with pytest.raises(SystemExit):
        ridge.parse_args(["--output-file", "r.csv", "--epochs", "10"])

    patchts = build_parser_from_flags(FLAGS["src.dl_patchts"], description="src.dl_patchts")
    args = patchts.parse_args(["--output-file", "p.csv", "--epochs", "10"])
    assert args.epochs == 10
