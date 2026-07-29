"""The #352 no-combiner fallback keys on "no partial for THIS run", not on ENOENT.

Root cause of the 2026-07-29 sandbox-proving ``s4.table`` failure ("harvested;
results table non-empty" → ``brief.results_table is empty — nothing harvested``).

The harvest's no-combiner fallback — pull the per-task ``metrics.json`` sidecars
and weighted-mean them, the ``@register_run``-sweep-with-no-reducer shape — used
to fire ONLY when rsync 404'd on ``<remote_path>/_combiner/``. That made the
whole path depend on a directory being ABSENT, which is not evidence anyone
owns:

* §10.S4's ``seal_code_tree`` ``mkdir -p``'d every shared path at the base while
  wiring a code tree's symlinks, so from the first tree-pinned submission
  ``<base>/_combiner/`` existed, empty, on every cluster;
* the ``_combiner/`` is ``--delete``-protected, so it also outlives the run that
  made it and a LATER run at the same ``remote_path`` finds only foreign
  partials (F05 filters them out at reduce time).

Either way the pull returned rc 0, ``reduce_partials`` returned ``{}``, and
aggregate-flow reported a SUCCESSFUL harvest with an empty table. An empty
aggregate is never a harvest.

The fallback now fires on "no wave partial for this run" for the runs it was
written for — the wave_map-LESS ones, where no combiner was ever in the picture
so ``_combiner/`` cannot hold their aggregate whatever it contains. A run that
DOES declare a wave_map keeps the (empty) combiner verdict: its empty
``_combiner/`` is a combine failure that ``_combine_missing`` already reports,
and substituting per-task metrics would mask it.

Companion: ``tests/infra/test_code_tree.py`` (the seal no longer creates the
base dirs no job writes through — the other half of the same fix).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

_RUN_ID = "20260729-120000-s4t"
_PI = [3.14, 3.15, 3.16]
_MEAN = sum(_PI) / len(_PI)


@pytest.fixture(autouse=True)
def _legacy_pull_path(monkeypatch):
    """Pin the legacy ``rsync_pull`` seam these tests mock (see the cluster-final
    suite's identical fixture) — this is a REDUCE-policy suite, not transport."""
    monkeypatch.setenv("HPC_AGGREGATE_TAR_PULL", "0")
    monkeypatch.delenv("HPC_CLUSTER_FINAL_REDUCE", raising=False)


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed(experiment: Path, *, aggregate_defaults: dict | None = None) -> None:
    """A terminal, wave_map-less run — the ``@register_run`` no-combiner shape
    the sandbox-proving fixture submits."""
    upsert_run(
        experiment,
        RunRecord(
            run_id=_RUN_ID,
            profile="monte_carlo_pi",
            cluster="hoffman2",
            ssh_target="user@hoffman2.idre.ucla.edu",
            remote_path="/u/scratch/exp",
            job_name="monte_carlo_pi",
            job_ids=["12345678"],
            total_tasks=len(_PI),
            submitted_at="2026-07-29T12:00:00+00:00",
            experiment_dir=str(experiment.resolve()),
            status="complete",
        ),
    )
    write_run_sidecar(
        experiment,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version="0.11.0",
        submitted_at="2026-07-29T12:00:00Z",
        executor="python3 train.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=len(_PI),
        tasks_py_sha="1" * 64,
        wave_map={},
        remote_path="/u/scratch/exp",
        **({"aggregate_defaults": aggregate_defaults} if aggregate_defaults else {}),
    )


def _pull_stub(*, combiner_files: dict[str, dict] | None = None, results: bool = True):
    """A ``rsync_pull`` that SUCCEEDS on ``_combiner`` — the post-S4 cluster shape.

    *combiner_files* maps a filename under the pulled ``_combiner/`` to its JSON
    body; ``None``/empty is the empty-but-present directory. *results* writes the
    per-task ``metrics.json`` sidecars the fallback reduces.
    """

    def _stub(*_a, remote_subdir: str, local_dir: str, include=None, **_kw):
        dest = Path(local_dir)
        dest.mkdir(parents=True, exist_ok=True)
        if remote_subdir == "_combiner":
            for name, body in (combiner_files or {}).items():
                (dest / name).write_text(json.dumps(body), encoding="utf-8")
        elif remote_subdir.startswith("results") and results:
            for i, v in enumerate(_PI):
                td = dest / _RUN_ID / f"task_{i}"
                td.mkdir(parents=True, exist_ok=True)
                (td / "metrics.json").write_text(
                    json.dumps({"pi_estimate": v, "n_samples": 1}), encoding="utf-8"
                )
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    return _stub


def test_empty_but_present_combiner_dir_still_harvests(journal_home, experiment, monkeypatch):
    """THE s4.table repro. ``<base>/_combiner/`` exists (the code-tree seal made
    it) and is empty; the run still harvests its per-task metrics rather than
    reporting a successful, empty table."""
    _seed(experiment)
    monkeypatch.setattr(af_module, "rsync_pull", _pull_stub())

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    assert result.reduce_path == "per_task_fallback"
    assert result.aggregated_metrics, "an empty aggregate is never a harvest"
    assert result.aggregated_metrics[_RUN_ID]["pi_estimate"] == pytest.approx(_MEAN)


def test_a_combiner_holding_only_a_foreign_runs_partial_still_harvests(
    journal_home, experiment, monkeypatch
):
    """F05, latent long before S4: ``_combiner/`` is ``--delete``-protected and
    shared across runs at one ``remote_path``, so a second run finds a directory
    full of partials the run_id filter correctly discards — leaving zero of its
    own. Same evidence as an absent dir, so the same fallback."""
    _seed(experiment)
    foreign = {
        "wave_0.json": {
            "run_id": "20260101-000000-old",
            "grid_points": {"old": {"pi_estimate": 99.0, "n_samples": 1}},
        }
    }
    monkeypatch.setattr(af_module, "rsync_pull", _pull_stub(combiner_files=foreign))

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    assert result.reduce_path == "per_task_fallback"
    assert result.aggregated_metrics[_RUN_ID]["pi_estimate"] == pytest.approx(_MEAN)
    assert "old" not in result.aggregated_metrics, "the foreign partial stays discarded"


def test_this_runs_partials_still_take_the_local_reduce(journal_home, experiment, monkeypatch):
    """No regression: a ``_combiner/`` that DOES carry this run's partials
    reduces locally and never pays the per-task fallback pull."""
    _seed(experiment)
    mine = {
        "wave_0.json": {
            "run_id": _RUN_ID,
            "grid_points": {"g0": {"pi_estimate": 3.14159, "n_samples": 10}},
        }
    }
    pulled: list[str] = []

    inner = _pull_stub(combiner_files=mine)

    def _spy(*a, remote_subdir: str, **kw):
        pulled.append(remote_subdir)
        return inner(*a, remote_subdir=remote_subdir, **kw)

    monkeypatch.setattr(af_module, "rsync_pull", _spy)

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    assert result.reduce_path == "local_reduce"
    assert result.aggregated_metrics["g0"]["pi_estimate"] == pytest.approx(3.14159)
    assert pulled == ["_combiner"], "the per-task fallback must not run"


def test_an_empty_combiner_with_a_custom_reducer_refuses_instead_of_meaning(
    journal_home, experiment, monkeypatch
):
    """A caller who configured ``aggregate_cmd`` gets the cluster-reduce
    remediation, not a silently-meaned metrics.json — the same rule the absent
    ``_combiner/`` already followed, now stated over the same condition."""
    _seed(experiment, aggregate_defaults={"aggregate_cmd": "python3 reduce.py"})
    monkeypatch.setattr(af_module, "rsync_pull", _pull_stub())

    with pytest.raises(errors.RemoteCommandFailed, match="carries no wave partial for this run"):
        aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID, mode="combiner-only"))


def test_a_genuine_pull_failure_is_still_a_pull_failure(journal_home, experiment, monkeypatch):
    """A transport hiccup (not ENOENT) must stay a loud rsync failure — never be
    re-read as 'the combiner never ran'."""
    _seed(experiment)

    def _boom(*_a, remote_subdir: str, local_dir: str, include=None, **_kw):
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(
            args=[], returncode=12, stdout="", stderr="rsync: connection unexpectedly closed"
        )

    monkeypatch.setattr(af_module, "rsync_pull", _boom)

    with pytest.raises(errors.RemoteCommandFailed, match="rsync_pull of _combiner failed"):
        aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))
