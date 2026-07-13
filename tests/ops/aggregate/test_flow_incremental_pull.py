"""Tests for ``aggregate_flow``'s ``_combiner/`` rsync include shape.

The cluster-side ``_combiner/`` directory grows linearly with wave count
(one ``wave_<N>.json`` + one ``wave_<N>.runtime.json`` per wave), so the
pull passes a small ``--include`` filter rather than a per-wave argv that
scales with wave count.

F08/F09 re-point: an earlier version narrowed the include to the waves
ABSENT locally (a filename-only diff), which meant a force-recombined
REMOTE wave (F08) and a truncated LOCAL wave (F09) — both already present
by filename — were never re-pulled, so the local reduce read stale/torn
data for the rest of the campaign. The pull now emits the two-glob filter
``["wave_*.json", "wave_*.runtime.json"]`` once any wave is present
locally, letting rsync's own size/mtime delta re-transfer exactly the
changed files while keeping the argv tiny. First call (nothing local yet)
and the no-wave_map path still emit an unfiltered pull (``include=None``).

These tests mock ``rsync_pull`` so the assertions exercise the include
shape, not a live transfer.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops.aggregate_flow import (
    _incremental_include_patterns,
    aggregate_flow,
)
from hpc_agent.state import run_record
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_run(experiment: Path, *, combined_waves: list[int]) -> RunRecord:
    record = RunRecord(
        run_id="r1",
        profile="ml_ridge",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml_ridge",
        job_ids=["12345678"],
        total_tasks=2,
        submitted_at="2026-04-26T17:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
        combined_waves=list(combined_waves),
    )
    upsert_run(experiment, record)
    return record


def _seed_sidecar(experiment: Path, *, wave_map: dict[str, list[int]]) -> None:
    write_run_sidecar(
        experiment,
        run_id="r1",
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=2,
        tasks_py_sha="1" * 64,
        wave_map=wave_map,
        remote_path="/u/scratch/exp",
    )


def _ok_rsync(*_a, **_kw):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# Unit-level: the include-pattern helper
# ---------------------------------------------------------------------------


def test_include_patterns_first_call_returns_none(tmp_path: Path):
    """Local dir empty AND every combined wave is missing -> unfiltered pull.

    Equivalent to the original behaviour; no benefit to emitting a long
    argv on a cold cache.
    """
    local = tmp_path / "_combiner"
    assert _incremental_include_patterns(local, [0, 1, 2]) is None


def test_include_patterns_no_combined_waves_returns_none(tmp_path: Path):
    """Empty ``combined_waves`` -> caller falls back to unfiltered pull.

    Mirrors the documented 'no wave_map' path: there is no per-wave
    state to drive a narrowed include, so we trust whatever lives under
    cluster ``_combiner/``.
    """
    local = tmp_path / "_combiner"
    local.mkdir()
    (local / "wave_0.json").write_text("{}")
    assert _incremental_include_patterns(local, []) is None


def test_include_patterns_some_local_returns_two_globs(tmp_path: Path):
    """Any wave present locally -> the two-glob filter (F08/F09 re-point).

    The include no longer narrows to the filename-diff (which excluded a
    force-recombined remote wave); it re-checks every wave file so rsync's
    delta can re-pull exactly the changed ones.
    """
    local = tmp_path / "_combiner"
    local.mkdir()
    (local / "wave_0.json").write_text("{}")
    (local / "wave_1.json").write_text("{}")
    # Runtime sidecars next to the partials must NOT be misread as
    # ``wave_<N>.json`` partials.
    (local / "wave_0.runtime.json").write_text("{}")

    patterns = _incremental_include_patterns(local, [0, 1, 2, 3])
    assert patterns == ["wave_*.json", "wave_*.runtime.json"]


def test_include_patterns_all_present_still_rechecks(tmp_path: Path):
    """Every combined wave already local -> STILL emit the wave globs.

    F08: a force-recombined remote wave (whose local copy exists by
    filename) must be re-pulled, so the include must MATCH the local wave
    files — the old "non-matching sentinel that transfers nothing" is
    exactly the bug.
    """
    local = tmp_path / "_combiner"
    local.mkdir()
    (local / "wave_0.json").write_text("{}")
    (local / "wave_1.json").write_text("{}")

    patterns = _incremental_include_patterns(local, [0, 1])
    assert patterns == ["wave_*.json", "wave_*.runtime.json"]


# ---------------------------------------------------------------------------
# Integration: aggregate_flow drives the helper end-to-end
# ---------------------------------------------------------------------------


def _run_aggregate(experiment: Path) -> tuple[mock.MagicMock, object]:
    """Run ``aggregate_flow`` with ``rsync_pull`` mocked and return (mock, result)."""
    spec = AggregateFlowSpec(run_id="r1", ensure_all_combined=False)
    rsync_mock = mock.MagicMock(side_effect=_ok_rsync)
    with (
        mock.patch.object(af_module, "rsync_pull", rsync_mock),
        mock.patch.object(af_module, "reduce_partials", return_value={}),
        mock.patch.object(af_module, "collect_wave_errors", return_value=set()),
    ):
        result = aggregate_flow(experiment, spec=spec)
    return rsync_mock, result


def test_first_call_emits_unfiltered_pull(journal_home, experiment):
    """No local combiner files -> ``include=None`` (full ``_combiner/`` pull).

    Establishes the baseline behaviour so the second-call test below has
    something to compare against.
    """
    _seed_run(experiment, combined_waves=[0, 1, 2])
    _seed_sidecar(experiment, wave_map={"0": [0, 1], "1": [2, 3], "2": [4, 5]})

    rsync_mock, _ = _run_aggregate(experiment)

    # First positional/keyword call: the combiner pull.
    combiner_call = rsync_mock.call_args_list[0]
    assert combiner_call.kwargs["remote_subdir"] == "_combiner"
    assert combiner_call.kwargs.get("include") is None


def test_second_call_still_rechecks_all_waves(journal_home, experiment):
    """All waves already pulled -> the pull STILL re-checks every wave (F08).

    A force-recombined remote wave has a locally-present file by filename;
    the include must match it so rsync's delta can re-pull the changed
    bytes. The old "narrow so the second call transfers nothing" behavior
    is exactly what dropped the recovered/force-recombined data.
    """
    _seed_run(experiment, combined_waves=[0, 1, 2])
    _seed_sidecar(experiment, wave_map={"0": [0, 1], "1": [2, 3], "2": [4, 5]})

    # Pre-populate the local combiner dir to simulate a prior aggregate
    # run that already pulled every wave.
    out = experiment / "_aggregated" / "r1" / "_combiner"
    out.mkdir(parents=True)
    for w in (0, 1, 2):
        (out / f"wave_{w}.json").write_text("{}")
        (out / f"wave_{w}.runtime.json").write_text("{}")

    rsync_mock, _ = _run_aggregate(experiment)

    combiner_call = rsync_mock.call_args_list[0]
    assert combiner_call.kwargs["remote_subdir"] == "_combiner"
    includes = combiner_call.kwargs.get("include")
    # The wave globs MUST be present so rsync re-verifies every wave file.
    assert includes == ["wave_*.json", "wave_*.runtime.json"]


def test_second_call_with_new_waves_still_uses_wave_globs(journal_home, experiment):
    """Waves 0,1 already local + cluster has 0..3 -> the two-glob filter.

    rsync's delta fetches waves 2/3 (absent locally) AND re-verifies 0/1 —
    so a force-recombine of an already-local wave is not silently skipped.
    """
    _seed_run(experiment, combined_waves=[0, 1, 2, 3])
    _seed_sidecar(
        experiment,
        wave_map={
            "0": [0, 1],
            "1": [2, 3],
            "2": [4, 5],
            "3": [6, 7],
        },
    )

    # Pre-populate waves 0 and 1 only.
    out = experiment / "_aggregated" / "r1" / "_combiner"
    out.mkdir(parents=True)
    for w in (0, 1):
        (out / f"wave_{w}.json").write_text("{}")
        (out / f"wave_{w}.runtime.json").write_text("{}")

    rsync_mock, _ = _run_aggregate(experiment)

    combiner_call = rsync_mock.call_args_list[0]
    includes = combiner_call.kwargs.get("include")
    assert includes == ["wave_*.json", "wave_*.runtime.json"]


def test_include_argv_stays_bounded_regardless_of_wave_count(journal_home, experiment):
    """The argv-size guarantee survives F08: the filter is two globs, not

    a per-wave list that scales with wave count. Both the cold first call
    (unfiltered) and the warm re-check emit at most a constant-size filter.
    """
    _seed_run(experiment, combined_waves=list(range(5)))
    _seed_sidecar(
        experiment,
        wave_map={str(w): [2 * w, 2 * w + 1] for w in range(5)},
    )

    # Pass 1: cold cache -> unfiltered.
    rsync_mock_1, _ = _run_aggregate(experiment)
    pass1_include = rsync_mock_1.call_args_list[0].kwargs.get("include")
    assert pass1_include is None

    # Simulate the pull having succeeded: write the combiner files locally.
    out = experiment / "_aggregated" / "r1" / "_combiner"
    out.mkdir(parents=True, exist_ok=True)
    for w in range(5):
        (out / f"wave_{w}.json").write_text("{}")
        (out / f"wave_{w}.runtime.json").write_text("{}")

    # Pass 2: warm cache -> two globs (constant size), which re-verify all.
    rsync_mock_2, _ = _run_aggregate(experiment)
    pass2_include = rsync_mock_2.call_args_list[0].kwargs.get("include")
    assert pass2_include == ["wave_*.json", "wave_*.runtime.json"]
    assert len(pass2_include) == 2  # bounded regardless of the 5 waves


def test_failed_summary_rsync_labels_truthfully_and_carries_stderr(journal_home, experiment):
    """A failed summaries rsync must surface a dedicated ``summary_rsync_failed``
    escalation token carrying the real stderr — NOT a mislabeled
    ``combiner_failed_max_retries:waves=-1`` that discards the error, and NOT a
    non-None summaries_dir_local pointing at an empty/partial dir (bug-sweep #68).
    """
    _seed_run(experiment, combined_waves=[0])
    _seed_sidecar(experiment, wave_map={"0": [0, 1]})

    def _split_rsync(*_a, **kw):
        # The _combiner pull succeeds; the summaries pull fails hard.
        if kw.get("remote_subdir") == "_combiner":
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=[], returncode=23, stdout="", stderr="rsync: connection unexpectedly closed"
        )

    spec = AggregateFlowSpec(
        run_id="r1",
        ensure_all_combined=False,
        pull_summaries=True,
        summary_glob="summary_*.csv",
    )
    with (
        mock.patch.object(af_module, "rsync_pull", mock.MagicMock(side_effect=_split_rsync)),
        mock.patch.object(af_module, "reduce_partials", return_value={}),
        mock.patch.object(af_module, "collect_wave_errors", return_value=set()),
    ):
        result = aggregate_flow(experiment, spec=spec)

    reason = result.escalation_reason or ""
    assert "summary_rsync_failed:" in reason
    assert "connection unexpectedly closed" in reason
    # The mislabeling class is gone.
    assert "waves=-1" not in reason
    # And the empty/partial summaries dir is not offered for column validation.
    assert result.summaries_dir_local is None
