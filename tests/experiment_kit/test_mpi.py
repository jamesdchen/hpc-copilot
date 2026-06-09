"""Multi-rank executor convention: ``@register_run(mpi=True)`` (#293 PR3).

Covers the three convention pieces: rank/world injection into the run from the
launcher env, the rank-0-only output gate, exclusion of the injected params
from synthesised flags, and AST-level mpi detection in ``discover_runs``.
"""

from __future__ import annotations

import argparse
import json
import types
from pathlib import Path

import pytest


def _exec_module(src: str, name: str = "hpc_mpi_test_mod") -> types.ModuleType:
    mod = types.ModuleType(name)
    exec(compile(src, f"<{name}>", "exec"), mod.__dict__)
    return mod


_MPI_RUN_SRC = (
    "from hpc_agent.experiment_kit import register_run\n"
    "\n"
    "@register_run(mpi=True)\n"
    "def run(rank: int = -1, world_size: int = -1, alpha: float = 1.0):\n"
    "    return {'rank': rank, 'world_size': world_size, 'alpha': alpha}\n"
)


# --- mpi flag recorded on the RunSpec --------------------------------------


def test_register_run_records_mpi_flag() -> None:
    mod = _exec_module(_MPI_RUN_SRC)
    assert mod._RUNS["run"].mpi is True
    assert mod._RUNS["run"].gpu is False


# --- rank / world injection from the launcher env --------------------------


def test_compute_injects_rank_and_world_from_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OMPI_COMM_WORLD_RANK", "0")
    monkeypatch.setenv("OMPI_COMM_WORLD_SIZE", "4")
    mod = _exec_module(_MPI_RUN_SRC)
    out = tmp_path / "out.json"
    # Note: rank/world_size are NOT on args — they come from the env.
    mod.compute(argparse.Namespace(alpha=2.0, output_file=str(out)))
    data = json.loads(out.read_text())
    assert data == {"rank": 0, "world_size": 4, "alpha": 2.0}


def test_only_rank_zero_writes_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SLURM_PROCID", "2")
    monkeypatch.setenv("SLURM_NTASKS", "4")
    mod = _exec_module(_MPI_RUN_SRC)
    out = tmp_path / "out.json"
    mod.compute(argparse.Namespace(alpha=1.0, output_file=str(out)))
    # Rank 2 must not write — the reducer expects exactly one metrics.json.
    assert not out.exists()


def test_explicit_rank_arg_still_wins(tmp_path: Path, monkeypatch) -> None:
    # setdefault semantics: an explicit rank on args is not overwritten by env,
    # but the OUTPUT gate keys off the env-derived rank (the real launcher rank).
    monkeypatch.setenv("OMPI_COMM_WORLD_RANK", "0")
    monkeypatch.setenv("OMPI_COMM_WORLD_SIZE", "8")
    mod = _exec_module(_MPI_RUN_SRC)
    out = tmp_path / "out.json"
    mod.compute(argparse.Namespace(rank=7, alpha=1.0, output_file=str(out)))
    data = json.loads(out.read_text())
    assert data["rank"] == 7  # explicit arg preserved
    assert data["world_size"] == 8  # injected


# --- non-mpi run is unaffected ---------------------------------------------


def test_non_mpi_run_does_not_inject_rank(tmp_path: Path, monkeypatch) -> None:
    # Even under a launcher env, a plain @register_run never gets rank/world,
    # and (being single-process rank 0) always writes its output.
    monkeypatch.setenv("OMPI_COMM_WORLD_RANK", "0")
    monkeypatch.setenv("OMPI_COMM_WORLD_SIZE", "4")
    mod = _exec_module(
        "from hpc_agent.experiment_kit import register_run\n"
        "@register_run\n"
        "def run(alpha: float = 1.0):\n"
        "    return {'alpha': alpha}\n"
    )
    out = tmp_path / "out.json"
    mod.compute(argparse.Namespace(alpha=3.0, output_file=str(out)))
    assert json.loads(out.read_text()) == {"alpha": 3.0}


# --- mpi_rank_world env detection ------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"OMPI_COMM_WORLD_RANK": "1", "OMPI_COMM_WORLD_SIZE": "3"}, (1, 3)),
        ({"PMI_RANK": "2", "PMI_SIZE": "5"}, (2, 5)),
        ({"SLURM_PROCID": "0", "SLURM_NTASKS": "8"}, (0, 8)),
        ({}, (0, 1)),  # no launcher → single-process default
        ({"OMPI_COMM_WORLD_RANK": "0"}, (0, 1)),  # rank without size → world 1
    ],
)
def test_mpi_rank_world(monkeypatch, env, expected) -> None:
    from hpc_agent.experiment_kit._runtime import mpi_rank_world

    for var in (
        "OMPI_COMM_WORLD_RANK",
        "OMPI_COMM_WORLD_SIZE",
        "PMI_RANK",
        "PMI_SIZE",
        "SLURM_PROCID",
        "SLURM_NTASKS",
    ):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert mpi_rank_world() == expected


# --- flag synthesis excludes injected params -------------------------------


def test_flags_exclude_rank_and_world_for_mpi() -> None:
    from hpc_agent.experiment_kit import flags_for_run

    def run(rank: int, world_size: int, alpha: float = 1.0):
        return {}

    names = {f.name for f in flags_for_run(run, mpi=True)}
    assert "rank" not in names
    assert "world_size" not in names
    assert "alpha" in names


def test_flags_keep_rank_when_not_mpi() -> None:
    # Without mpi, a param literally named ``rank`` is an ordinary flag.
    from hpc_agent.experiment_kit import flags_for_run

    def run(rank: int = 0):
        return {}

    names = {f.name for f in flags_for_run(run, mpi=False)}
    assert "rank" in names


# --- discover detects mpi from the decorator -------------------------------


def test_discover_runs_detects_mpi(tmp_path: Path) -> None:
    from hpc_agent.experiment_kit.discover import discover_runs

    (tmp_path / "solve.py").write_text(_MPI_RUN_SRC, encoding="utf-8")
    runs = discover_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].mpi is True
    # rank/world_size are not synthesised as flags.
    assert {f.name for f in runs[0].flags}.isdisjoint({"rank", "world_size"})
