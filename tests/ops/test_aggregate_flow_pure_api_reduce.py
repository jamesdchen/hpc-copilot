"""``aggregate-flow`` pure-API reduction is mode-aware (not mean-locked).

Before this, a ``requires_ssh=False`` backend's aggregate path short-circuited
straight to ``reduce_metrics`` (weighted mean over each task's ``metrics.json``)
and silently ignored ``mode`` / ``aggregate_cmd`` — so a pure-API backend could
NOT use the caller-owned reducer the SSH path offers via ``cluster-reduce``.
That baked an output-shape assumption (numeric, mean-able ``metrics.json``) into
core for a whole class of backends.

These tests prove the pure-API branch now mirrors the SSH ``mode`` dispatch:
the custom reducer runs LOCALLY over the fetched artifacts when selected, and
the weighted-mean is preserved as the fallback. A FAKE registered backend whose
``fetch_results`` writes both a raw ``value.txt`` and a ``metrics.json`` per
task stands in for a live API; ``rsync_pull`` is booby-trapped so the pure-API
path still proves ZERO rsync on every branch.
"""

from __future__ import annotations

import json
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.infra import backends
from hpc_agent.infra.backends import HPCBackend
from hpc_agent.ops import aggregate_flow as agg
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.state import run_record
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260623-100000-ccc"
_BACKEND_NAME = "fakepureapireduce"

# Per-task fixtures the fake backend writes. ``value.txt`` is the RAW artifact a
# custom reducer reads (the mean reducer can't); ``metrics.json`` drives the
# weighted-mean fallback. Weighted mean of loss = (1*1 + 3*1 + 5*2)/4 = 3.5;
# sum of value.txt = 1 + 2 + 3 = 6.0.
_TASKS = [
    {"value": 1.0, "metrics": {"loss": 1.0, "n_samples": 1}},
    {"value": 2.0, "metrics": {"loss": 3.0, "n_samples": 1}},
    {"value": 3.0, "metrics": {"loss": 5.0, "n_samples": 2}},
]
_MEAN_RESULT = {_RUN_ID: {"loss": 3.5, "n_samples": 4}}


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


def _record(**overrides) -> RunRecord:
    base = {
        "run_id": _RUN_ID,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "p",
        "job_ids": ["9001"],
        "total_tasks": 3,
        "submitted_at": "2026-06-23T10:00:00+00:00",
        "experiment_dir": "/tmp/exp",
        "status": "complete",
        "backend": _BACKEND_NAME,
    }
    base.update(overrides)
    return RunRecord(**base)


@pytest.fixture
def fake_backend(tmp_path: Path):
    """A ``requires_ssh=False`` backend whose ``fetch_results`` writes the FULL
    per-task artifacts (``value.txt`` + ``metrics.json``) into ``task-<i>``."""

    @backends.register(_BACKEND_NAME)
    class _Fake(HPCBackend):
        scheduler_name = _BACKEND_NAME
        requires_ssh = False
        log_dir = "logs"

        def __init__(self, *, remote_path: str) -> None:
            self.remote_path = remote_path

        @classmethod
        def from_build_context(cls, ctx: object) -> _Fake:
            return cls(remote_path=ctx.remote_path)  # type: ignore[attr-defined]

        def fetch_results(self, run_id: str, dest_dir: str) -> list[str]:
            from pathlib import Path as _P

            dest = _P(dest_dir)
            dirs: list[str] = []
            for i, t in enumerate(_TASKS):
                td = dest / f"task-{i}"
                td.mkdir(parents=True, exist_ok=True)
                (td / "value.txt").write_text(str(t["value"]), encoding="utf-8")
                (td / "metrics.json").write_text(json.dumps(t["metrics"]), encoding="utf-8")
                dirs.append(str(td))
            return dirs

        def _build_command(self, *a: object, **k: object) -> object:
            raise NotImplementedError

    try:
        yield _Fake
    finally:
        backends._REGISTRY.pop(_BACKEND_NAME, None)


@pytest.fixture(autouse=True)
def _no_rsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any ``rsync_pull`` on the pure-API path is a bug — blow up loudly."""

    def _explode(*_a: object, **_k: object):
        raise AssertionError("rsync_pull must NOT run on the pure-API path")

    monkeypatch.setattr(agg, "rsync_pull", _explode)


def _reducer(tmp_path: Path, body: str, name: str = "reducer.py") -> str:
    script = tmp_path / name
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    # Quote both paths: aggregate_cmd runs under shell=True, and an install
    # path with a space (e.g. sys.executable under "...\CC Allowed\...") would
    # otherwise split — `'C:\\...\\CC' is not recognized` on Windows cmd.exe.
    return f'"{sys.executable}" "{script}"'


# Sums the RAW value.txt files — a reduction the weighted-mean path cannot do,
# and whose result shape proves which path ran.
_SUM_REDUCER = """
import glob, json, os
d = os.environ["HPC_RESULTS_DIR"]
paths = glob.glob(os.path.join(d, "task-*", "value.txt"))
total = sum(float(open(p).read()) for p in paths)
out = os.environ["HPC_AGGREGATED_OUTPUT"]
json.dump({"total": total, "via": "custom"}, open(out, "w"))
"""


def test_auto_with_aggregate_cmd_runs_custom_reducer(
    journal_home, experiment, fake_backend, tmp_path, monkeypatch
):
    upsert_run(experiment, _record())
    cmd = _reducer(tmp_path, _SUM_REDUCER)

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID), aggregate_cmd=cmd)

    # The reducer's JSON is surfaced directly (NOT wrapped in {run_id: ...}),
    # matching the SSH cluster-reduce branch, and it read the RAW artifacts.
    assert result.aggregated_metrics == {"total": 6.0, "via": "custom"}


def test_auto_without_cmd_falls_back_to_weighted_mean(journal_home, experiment, fake_backend):
    upsert_run(experiment, _record())

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    # Historical pure-API behaviour preserved exactly.
    assert result.aggregated_metrics == _MEAN_RESULT


def test_combiner_only_forces_mean_even_when_cmd_present(
    journal_home, experiment, fake_backend, tmp_path
):
    upsert_run(experiment, _record())
    cmd = _reducer(tmp_path, _SUM_REDUCER)

    spec = AggregateFlowSpec(run_id=_RUN_ID, mode="combiner-only")
    result = aggregate_flow(experiment, spec=spec, aggregate_cmd=cmd)

    # combiner-only ignores the custom reducer and means the metrics.json.
    assert result.aggregated_metrics == _MEAN_RESULT


def test_cluster_reduce_mode_without_cmd_raises(journal_home, experiment, fake_backend):
    upsert_run(experiment, _record())

    spec = AggregateFlowSpec(run_id=_RUN_ID, mode="cluster-reduce")
    with pytest.raises(errors.SpecInvalid, match="aggregate_cmd"):
        aggregate_flow(experiment, spec=spec)


def test_sidecar_aggregate_cmd_used_when_no_kwarg(
    journal_home, experiment, fake_backend, tmp_path, monkeypatch
):
    upsert_run(experiment, _record())
    cmd = _reducer(tmp_path, _SUM_REDUCER)
    # No explicit kwarg: the cmd must come from the run sidecar's
    # aggregate_defaults. Stub the local sidecar read _pure_api_reduce performs.
    monkeypatch.setattr(
        agg, "read_run_sidecar", lambda *a, **k: {"aggregate_defaults": {"aggregate_cmd": cmd}}
    )

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    assert result.aggregated_metrics == {"total": 6.0, "via": "custom"}


def test_explicit_kwarg_overrides_sidecar_cmd(
    journal_home, experiment, fake_backend, tmp_path, monkeypatch
):
    upsert_run(experiment, _record())
    kwarg_cmd = _reducer(
        tmp_path,
        """
        import json, os
        json.dump({"src": "kwarg"}, open(os.environ["HPC_AGGREGATED_OUTPUT"], "w"))
        """,
        name="kwarg.py",
    )
    sidecar_cmd = _reducer(
        tmp_path,
        """
        import json, os
        json.dump({"src": "sidecar"}, open(os.environ["HPC_AGGREGATED_OUTPUT"], "w"))
        """,
        name="sidecar.py",
    )
    monkeypatch.setattr(
        agg,
        "read_run_sidecar",
        lambda *a, **k: {"aggregate_defaults": {"aggregate_cmd": sidecar_cmd}},
    )

    result = aggregate_flow(
        experiment, spec=AggregateFlowSpec(run_id=_RUN_ID), aggregate_cmd=kwarg_cmd
    )

    assert result.aggregated_metrics == {"src": "kwarg"}


def test_failing_reducer_propagates_remote_command_failed(
    journal_home, experiment, fake_backend, tmp_path
):
    upsert_run(experiment, _record())
    cmd = _reducer(
        tmp_path,
        """
        import sys
        print("reducer exploded", file=sys.stderr)
        sys.exit(2)
        """,
    )

    with pytest.raises(errors.RemoteCommandFailed, match="reducer exploded"):
        aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID), aggregate_cmd=cmd)
