"""SSH aggregate path falls back to the deterministic weighted-mean (#352).

A ``@register_run`` SSH sweep submitted with NO reducer (no
``aggregate_defaults.aggregate_cmd``) and NO cluster-side combiner leaves
nothing under ``_combiner/`` to reduce. The historical behaviour was to
RAISE with a "configure a reducer at submit time" hint — which, in a live
demo, forced the aggregate skill to compute the mean BY HAND (and get the
arithmetic wrong while returning ``ok: true``). That is the exact "LLM in
the compute loop" failure the framework exists to prevent.

These tests prove the SSH path now mirrors the LOCAL / pure-API default
(#342): when ``_combiner/`` is absent and no ``aggregate_cmd`` is
configured, it pulls each task's ``metrics.json`` and runs the SAME
deterministic :func:`reduce_metrics` weighted-mean — reduction always
stays in code. And when there is genuinely nothing numeric to reduce, it
returns a typed failure rather than fabricating an aggregate.

``rsync_pull`` is mocked: the ``_combiner`` pull reports "No such file or
directory" (combiner never ran) and the ``results`` pull writes the
per-task ``metrics.json`` fixtures into the local destination.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.state import run_record
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    pass

_RUN_ID = "20260623-120000-pi0"

# The demo's ten Monte-Carlo pi estimates. The CORRECT sum is 31.413596
# (the live demo's prose-computed sum of 31.412596 was wrong by 0.001 and
# yielded 3.1412596 instead of the true 3.1413596). Unweighted mean here
# (n_samples=1 each) = 31.413596 / 10 = 3.1413596 -> the value the skill
# SHOULD have produced via the reducer.
_PI_VALUES = [
    3.1404,
    3.1421,
    3.1399,
    3.1430,
    3.1410,
    3.1418,
    3.1408,
    3.1425,
    3.1402,
    3.141896,
]
_EXPECTED_MEAN = sum(_PI_VALUES) / len(_PI_VALUES)


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


def _seed_run(experiment: Path) -> RunRecord:
    record = RunRecord(
        run_id=_RUN_ID,
        profile="monte_carlo_pi",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="monte_carlo_pi",
        job_ids=["12345678"],
        total_tasks=len(_PI_VALUES),
        submitted_at="2026-06-23T12:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
        status="complete",
    )
    upsert_run(experiment, record)
    return record


def _seed_sidecar_no_reducer(experiment: Path) -> None:
    """Sidecar with NO aggregate_defaults.aggregate_cmd — the no-reducer shape."""
    write_run_sidecar(
        experiment,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version="0.10.0",
        submitted_at="2026-06-23T12:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/task-{task_id}",
        task_count=len(_PI_VALUES),
        tasks_py_sha="1" * 64,
        wave_map={},
        remote_path="/u/scratch/exp",
    )


def _combiner_missing_rsync(metrics: list[dict] | None):
    """Build an rsync_pull stub: _combiner pull 404s, results pull writes sidecars.

    When *metrics* is None the ``results`` pull writes NOTHING (simulating a
    run where no task ever produced a metrics.json), so the fallback finds
    no numeric input.
    """

    def _stub(*_a, remote_subdir: str, local_dir: str, **_kw):
        if remote_subdir == "_combiner":
            return subprocess.CompletedProcess(
                args=[],
                returncode=23,
                stdout="",
                stderr=(
                    'rsync: link_stat "/u/scratch/exp/_combiner" failed: '
                    "No such file or directory (2)"
                ),
            )
        if remote_subdir == "results":
            dest = Path(local_dir)
            dest.mkdir(parents=True, exist_ok=True)
            for i, m in enumerate(metrics or []):
                td = dest / f"task-{i}"
                td.mkdir(parents=True, exist_ok=True)
                (td / "metrics.json").write_text(json.dumps(m), encoding="utf-8")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        # Any other subdir (summaries, etc.) — benign success.
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    return _stub


def test_ssh_no_reducer_no_combiner_falls_back_to_weighted_mean(
    journal_home, experiment, monkeypatch
):
    """No _combiner/ + no aggregate_cmd -> deterministic weighted-mean, not an error."""
    _seed_run(experiment)
    _seed_sidecar_no_reducer(experiment)

    metrics = [{"pi_estimate": v, "n_samples": 1} for v in _PI_VALUES]
    monkeypatch.setattr(af_module, "rsync_pull", _combiner_missing_rsync(metrics))

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    # Keyed by run_id like reduce_partials / the pure-API path.
    assert set(result.aggregated_metrics) == {_RUN_ID}
    agg = result.aggregated_metrics[_RUN_ID]
    # The reducer computed the CORRECT mean — not the demo's hand-rolled
    # 3.1412596. ~3.1413596.
    assert agg["pi_estimate"] == pytest.approx(_EXPECTED_MEAN)
    assert agg["pi_estimate"] == pytest.approx(3.1413596)
    assert agg["n_samples"] == len(_PI_VALUES)


def test_ssh_default_reducer_is_weighted_by_n_samples(journal_home, experiment, monkeypatch):
    """n_samples weighting matches reduce_metrics — the SAME machinery as #342."""
    _seed_run(experiment)
    _seed_sidecar_no_reducer(experiment)

    # loss weighted mean = (1*1 + 3*1 + 5*2) / 4 = 3.5; n_samples sums to 4.
    # The reducer means over whatever per-task metrics.json files were pulled;
    # the run record's total_tasks does not constrain the fallback.
    metrics = [
        {"loss": 1.0, "n_samples": 1},
        {"loss": 3.0, "n_samples": 1},
        {"loss": 5.0, "n_samples": 2},
    ]
    monkeypatch.setattr(af_module, "rsync_pull", _combiner_missing_rsync(metrics))

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    agg = result.aggregated_metrics[_RUN_ID]
    assert agg["loss"] == pytest.approx(3.5)
    assert agg["n_samples"] == 4


def test_ssh_no_reducer_no_metrics_returns_typed_failure(journal_home, experiment, monkeypatch):
    """No combiner AND no readable per-task metrics.json -> typed failure, not fabrication.

    The reducer has zero numeric input. Inventing a mean here is exactly
    the failure being closed, so the path raises a typed RemoteCommandFailed
    rather than returning a plausible-but-fabricated aggregate.
    """
    _seed_run(experiment)
    _seed_sidecar_no_reducer(experiment)

    # results pull "succeeds" but writes NO metrics.json sidecars.
    monkeypatch.setattr(af_module, "rsync_pull", _combiner_missing_rsync(None))

    with pytest.raises(errors.RemoteCommandFailed, match="no numeric input to reduce"):
        aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))


def test_ssh_no_reducer_results_pull_fails_returns_typed_failure(
    journal_home, experiment, monkeypatch
):
    """No combiner AND the per-task results pull itself fails -> typed failure."""
    _seed_run(experiment)
    _seed_sidecar_no_reducer(experiment)

    def _stub(*_a, remote_subdir: str, local_dir: str, **_kw):
        # Both _combiner and results pulls 404 / fail.
        return subprocess.CompletedProcess(
            args=[], returncode=23, stdout="", stderr="No such file or directory"
        )

    monkeypatch.setattr(af_module, "rsync_pull", _stub)

    with pytest.raises(errors.RemoteCommandFailed, match="fallback pull failed"):
        aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))


def test_ssh_no_combiner_but_aggregate_cmd_configured_still_raises_hint(
    journal_home, experiment, monkeypatch
):
    """When an aggregate_cmd IS configured, the no-combiner case keeps the
    original remediation hint (the caller chose a custom reducer; silently
    meaning the metrics.json would mask their intent)."""
    _seed_run(experiment)
    write_run_sidecar(
        experiment,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version="0.10.0",
        submitted_at="2026-06-23T12:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/task-{task_id}",
        task_count=len(_PI_VALUES),
        tasks_py_sha="1" * 64,
        wave_map={},
        remote_path="/u/scratch/exp",
        aggregate_defaults={"aggregate_cmd": "python3 reducer.py"},
    )

    # _combiner pull 404s; with an aggregate_cmd present the fallback must NOT
    # fire, so the results pull is never reached.
    def _stub(*_a, remote_subdir: str, local_dir: str, **_kw):
        if remote_subdir == "_combiner":
            return subprocess.CompletedProcess(
                args=[], returncode=23, stdout="", stderr="No such file or directory (2)"
            )
        raise AssertionError("per-task results fallback must NOT run when aggregate_cmd is set")

    monkeypatch.setattr(af_module, "rsync_pull", _stub)

    # mode='combiner-only' avoids the cluster-reduce short-circuit (which would
    # run the cmd) so we exercise the _combiner_only_reduce no-such branch with
    # an aggregate_cmd present.
    spec = AggregateFlowSpec(run_id=_RUN_ID, mode="combiner-only")
    with pytest.raises(errors.RemoteCommandFailed, match="combiner step never ran"):
        aggregate_flow(experiment, spec=spec)
