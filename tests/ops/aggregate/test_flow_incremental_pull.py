"""Tests that ``aggregate_flow`` narrows its ``_combiner/`` rsync to the
waves not already pulled locally.

The cluster-side ``_combiner/`` directory grows linearly with wave count
(one ``wave_<N>.json`` + one ``wave_<N>.runtime.json`` per wave). Even
with rsync's ``-az`` short-circuit on unchanged files, walking 1000+
files on every aggregate-flow call dominates the latency budget. The
incremental pull restricts each rsync to the diff between the run
record's ``combined_waves`` and locally-present ``wave_<N>.json`` files,
so a second call with no new waves transfers nothing.

These tests mock ``rsync_pull`` so the assertions exercise the include
shape, not a live transfer.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from hpc_agent._internal import session
from hpc_agent._internal.session import RunRecord, run_record
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops.aggregate import flow as af_module
from hpc_agent.ops.aggregate.flow import (
    _incremental_include_patterns,
    aggregate_flow,
)
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    monkeypatch.setattr(session, "HPC_HOMEDIR", home)
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
    session.upsert_run(experiment, record)
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


def test_include_patterns_partial_overlap_targets_only_missing(tmp_path: Path):
    """Waves 0,1 already pulled + cluster has 0..3 -> include 2 and 3 only."""
    local = tmp_path / "_combiner"
    local.mkdir()
    (local / "wave_0.json").write_text("{}")
    (local / "wave_1.json").write_text("{}")
    # Runtime sidecars next to the partials must NOT be misread as
    # ``wave_<N>.json`` partials.
    (local / "wave_0.runtime.json").write_text("{}")

    patterns = _incremental_include_patterns(local, [0, 1, 2, 3])
    assert patterns == [
        "wave_2.json",
        "wave_2.runtime.json",
        "wave_3.json",
        "wave_3.runtime.json",
    ]


def test_include_patterns_all_present_returns_sentinel(tmp_path: Path):
    """Every combined wave already local -> non-matching pattern, no transfer."""
    local = tmp_path / "_combiner"
    local.mkdir()
    (local / "wave_0.json").write_text("{}")
    (local / "wave_1.json").write_text("{}")

    patterns = _incremental_include_patterns(local, [0, 1])
    # Caller still issues an rsync (cheap connectivity check) but the
    # filter must exclude every file. Any non-matching include is fine.
    assert patterns is not None
    for w in (0, 1):
        assert f"wave_{w}.json" not in patterns
        assert f"wave_{w}.runtime.json" not in patterns


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


def test_second_call_with_no_new_waves_uses_non_matching_include(journal_home, experiment):
    """All waves already pulled -> include narrows so rsync transfers nothing.

    This is the optimization's core: on a re-run of aggregate_flow over
    an already-aggregated terminal run, the cluster's ``_combiner/``
    walk is skipped entirely (rsync still issues a stat round-trip, but
    the file list is empty).
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
    assert includes is not None, "second call must pass --include= filters"
    # None of the include patterns should match a real wave file.
    for w in (0, 1, 2):
        assert f"wave_{w}.json" not in includes
        assert f"wave_{w}.runtime.json" not in includes


def test_second_call_with_new_waves_pulls_only_the_diff(journal_home, experiment):
    """Waves 0,1 already pulled + cluster has waves 0..3 -> include 2 and 3 only.

    Proves the strict-subset case: an aggregate run that races ahead of
    monitor still picks up the latest combined waves without paying for
    a re-walk of the prefix already on disk.
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
    assert includes == [
        "wave_2.json",
        "wave_2.runtime.json",
        "wave_3.json",
        "wave_3.runtime.json",
    ]


def test_second_call_pulls_strictly_fewer_files_than_first(journal_home, experiment):
    """The headline guarantee: pass 2 narrows to a subset of pass 1.

    Pass 1 (cold cache) sends no include filter (effectively unbounded —
    matches every file in ``_combiner/``). Pass 2 (warm cache, no new
    waves) sends a finite, non-matching include set.

    Counting *target* wave files on each side:

    * Pass 1 effective set: every ``wave_<N>.json`` for N in
      combined_waves (since rsync would transfer all of them).
    * Pass 2 effective set: zero (the include list is a non-matching
      sentinel).

    Asserting ``len(pass2) < len(pass1)`` proves the optimization at
    the level the user cares about (file transfers avoided).
    """
    _seed_run(experiment, combined_waves=[0, 1, 2, 3, 4])
    _seed_sidecar(
        experiment,
        wave_map={str(w): [2 * w, 2 * w + 1] for w in range(5)},
    )

    # Pass 1: cold cache.
    rsync_mock_1, _ = _run_aggregate(experiment)
    pass1_include = rsync_mock_1.call_args_list[0].kwargs.get("include")

    # Simulate the pull having succeeded: write the combiner files
    # locally so pass 2 sees them.
    out = experiment / "_aggregated" / "r1" / "_combiner"
    out.mkdir(parents=True, exist_ok=True)
    for w in range(5):
        (out / f"wave_{w}.json").write_text("{}")
        (out / f"wave_{w}.runtime.json").write_text("{}")

    # Pass 2: warm cache.
    rsync_mock_2, _ = _run_aggregate(experiment)
    pass2_include = rsync_mock_2.call_args_list[0].kwargs.get("include")

    # Effective file count: pass 1 has no filter (unbounded). Use the
    # cluster-side combined_waves count as the cardinality proxy.
    pass1_effective = 5 if pass1_include is None else len(pass1_include) // 2
    # Pass 2's include is the non-matching sentinel; effective transfers = 0.
    pass2_effective = 0
    for w in range(5):
        if pass2_include and (
            f"wave_{w}.json" in pass2_include or f"wave_{w}.runtime.json" in pass2_include
        ):
            pass2_effective += 1

    assert pass2_effective < pass1_effective, (
        f"second call should transfer fewer waves than first; "
        f"pass1={pass1_effective}, pass2={pass2_effective}"
    )
    assert pass2_effective == 0
