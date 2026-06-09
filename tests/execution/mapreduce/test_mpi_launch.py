"""Dispatcher MPI launcher prefix (#293 PR3).

``_mpi_launch_prefix`` reads HPC_MPI_RANKS / HPC_MPI_LAUNCHER from the job env
and returns the ``srun``/``mpirun``/``aprun`` prefix the dispatcher prepends to
the per-task command — so one bookkeeping process fans the compute out to N
ranks. An ordinary task (no MPI env) gets an empty prefix and is unaffected.
"""

from __future__ import annotations

import pytest

from hpc_agent.execution.mapreduce.dispatch import _mpi_launch_prefix


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"HPC_MPI_RANKS": "8", "HPC_MPI_LAUNCHER": "srun"}, "srun --ntasks=8"),
        ({"HPC_MPI_RANKS": "16", "HPC_MPI_LAUNCHER": "mpirun"}, "mpirun -np 16"),
        ({"HPC_MPI_RANKS": "4", "HPC_MPI_LAUNCHER": "aprun"}, "aprun -n 4"),
        # Launcher omitted defaults to srun.
        ({"HPC_MPI_RANKS": "2"}, "srun --ntasks=2"),
    ],
)
def test_prefix_per_launcher(env, expected) -> None:
    assert _mpi_launch_prefix(env) == expected


def test_no_mpi_env_yields_empty_prefix() -> None:
    # The common (non-MPI) path: nothing prepended.
    assert _mpi_launch_prefix({}) == ""
    assert _mpi_launch_prefix({"HPC_MPI_RANKS": ""}) == ""


def test_single_rank_needs_no_launcher() -> None:
    assert _mpi_launch_prefix({"HPC_MPI_RANKS": "1", "HPC_MPI_LAUNCHER": "srun"}) == ""


def test_unknown_launcher_is_skipped_not_fatal() -> None:
    # An unrecognised launcher runs un-launched rather than emitting a broken
    # command — the warning goes to stderr; the prefix is empty.
    assert _mpi_launch_prefix({"HPC_MPI_RANKS": "4", "HPC_MPI_LAUNCHER": "poe"}) == ""


def test_non_integer_ranks_is_skipped() -> None:
    assert _mpi_launch_prefix({"HPC_MPI_RANKS": "lots", "HPC_MPI_LAUNCHER": "srun"}) == ""
