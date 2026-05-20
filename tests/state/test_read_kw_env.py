"""Tests for the executor-side ``read_kw_env`` helper.

Lives in ``hpc_agent.mapreduce.metrics_io`` (the same stdlib-only module
that ships to the cluster), so the test only depends on the env shape
the dispatcher already exports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.mapreduce.metrics_io import read_kw_env

if TYPE_CHECKING:
    import pytest


def test_read_kw_env_strips_prefix_and_lowercases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_KW_LR", "1e-3")
    monkeypatch.setenv("HPC_KW_N_LAYERS", "4")
    monkeypatch.setenv("HPC_KW_OPTIMIZER", "adam")
    assert read_kw_env() == {"lr": "1e-3", "n_layers": "4", "optimizer": "adam"}


def test_read_kw_env_ignores_other_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_KW_SEED", "42")
    monkeypatch.setenv("HPC_RUN_ID", "ml-x-deadbeef")  # framework var, NOT user kw
    monkeypatch.setenv("HPC_TASK_ID", "0")
    monkeypatch.setenv("PATH", "/usr/bin")
    out = read_kw_env()
    assert out == {"seed": "42"}


def test_read_kw_env_empty_when_no_kw_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Strip every HPC_KW_* var that might be in the test environment.
    import os

    for k in list(os.environ):
        if k.startswith("HPC_KW_"):
            monkeypatch.delenv(k, raising=False)
    assert read_kw_env() == {}


def test_read_kw_env_value_is_string_even_for_numeric_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-tripping through the env always yields strings; the executor
    casts. Pin this so users don't accidentally rely on auto-typing."""
    monkeypatch.setenv("HPC_KW_LR", "1.5e-3")
    monkeypatch.setenv("HPC_KW_N", "100")
    out = read_kw_env()
    assert out["lr"] == "1.5e-3"
    assert out["n"] == "100"
    assert isinstance(out["lr"], str) and isinstance(out["n"], str)
