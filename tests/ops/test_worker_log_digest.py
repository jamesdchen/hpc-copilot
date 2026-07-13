"""Tests for the ``worker-log-digest`` query verb (run-#10 finding G2).

Pins the mechanized raw-log scan the premortem used to ask the LLM to do by
hand: marker counts over the engine's own bracket vocabulary, a verbatim tail,
a fail-open envelope on a missing file, and determinism.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.worker_log_digest import WorkerLogDigestSpec
from hpc_agent.ops.worker_log_digest import KNOWN_MARKERS, worker_log_digest

_SYNTHETIC = """\
[dispatch] task_id=0 run_id=r result_dir=results/0
[dispatch] cmd=python -m src.train
engine connect [throttle]: TimeoutError: connect timed out
[dispatch] WARN: ignoring non-integer HPC_MPI_RANKS='x'
[dispatch] ERROR: HPC_TASK_ID env var not set
[dispatch] FAILED (exit 1), partial output preserved in wip/
[dispatch] FATAL: could not clean stale WIP wip/: PermissionError
plain narrative line with no marker
another [throttle] retry line
"""


def _write(tmp_path: Path, name: str, text: str) -> str:
    (tmp_path / name).write_text(text, encoding="utf-8")
    return name


def test_marker_counts_on_synthetic_log(tmp_path: Path) -> None:
    rel = _write(tmp_path, "worker.log", _SYNTHETIC)
    result = worker_log_digest(experiment_dir=tmp_path, spec=WorkerLogDigestSpec(log_path=rel))

    assert result.readable is True
    assert result.exists is True
    assert result.error is None
    assert result.total_lines == 9
    assert result.marker_counts == {
        "[throttle]": 2,
        "[dispatch] FATAL": 1,
        "[dispatch] FAILED": 1,
        "[dispatch] ERROR": 1,
        "[dispatch] WARN": 1,
    }
    # Every known marker is present in the shape (stability), even at 0.
    assert set(result.marker_counts) == set(KNOWN_MARKERS)


def test_tail_is_verbatim_and_bounded(tmp_path: Path) -> None:
    rel = _write(tmp_path, "worker.log", _SYNTHETIC)
    result = worker_log_digest(
        experiment_dir=tmp_path, spec=WorkerLogDigestSpec(log_path=rel, tail_lines=3)
    )
    assert result.tail_lines_requested == 3
    assert result.tail == [
        "[dispatch] FATAL: could not clean stale WIP wip/: PermissionError",
        "plain narrative line with no marker",
        "another [throttle] retry line",
    ]
    # The verbatim tail rides inside the rendered markdown, fenced.
    assert "another [throttle] retry line" in result.render
    assert "~~~~text" in result.render


def test_tail_lines_zero_echoes_nothing_but_still_counts(tmp_path: Path) -> None:
    rel = _write(tmp_path, "worker.log", _SYNTHETIC)
    result = worker_log_digest(
        experiment_dir=tmp_path, spec=WorkerLogDigestSpec(log_path=rel, tail_lines=0)
    )
    assert result.tail == []
    assert result.total_lines == 9
    assert result.marker_counts["[throttle]"] == 2
    assert "no verbatim tail requested" in result.render


def test_missing_file_fails_open(tmp_path: Path) -> None:
    result = worker_log_digest(
        experiment_dir=tmp_path, spec=WorkerLogDigestSpec(log_path=".hpc/_detached/nope.log")
    )
    assert result.exists is False
    assert result.readable is False
    assert result.error is not None and "no such file" in result.error
    assert result.total_lines == 0
    assert result.marker_counts == {}
    assert result.tail == []
    assert "unreadable" in result.render


def test_absolute_path_within_experiment_dir_accepted(tmp_path: Path) -> None:
    _write(tmp_path, "worker.log", _SYNTHETIC)
    abspath = str((tmp_path / "worker.log").resolve())
    result = worker_log_digest(experiment_dir=tmp_path, spec=WorkerLogDigestSpec(log_path=abspath))
    assert result.readable is True
    assert result.total_lines == 9


def test_path_escaping_experiment_dir_is_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        worker_log_digest(
            experiment_dir=tmp_path, spec=WorkerLogDigestSpec(log_path="../../etc/passwd")
        )


def test_deterministic(tmp_path: Path) -> None:
    rel = _write(tmp_path, "worker.log", _SYNTHETIC)
    a = worker_log_digest(experiment_dir=tmp_path, spec=WorkerLogDigestSpec(log_path=rel))
    b = worker_log_digest(experiment_dir=tmp_path, spec=WorkerLogDigestSpec(log_path=rel))
    assert a.model_dump() == b.model_dump()


def test_empty_log_all_zero_counts(tmp_path: Path) -> None:
    rel = _write(tmp_path, "worker.log", "")
    result = worker_log_digest(experiment_dir=tmp_path, spec=WorkerLogDigestSpec(log_path=rel))
    assert result.readable is True
    assert result.total_lines == 0
    assert all(v == 0 for v in result.marker_counts.values())
