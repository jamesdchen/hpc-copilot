"""Ingestion-at-harvest — data-trace **T4** (docs/design/data-trace.md).

The per-task pull seam (``_per_task_metrics_reduce`` — the no-combiner
weighted-mean fallback) folds each task's ``_trace.jsonl`` into the SAME pull as
the metrics + ``.hpc_cmd_sha`` (F1 include-fold — one pull cycle, not a metrics
pull then a separate trace pull over the identical ``results/`` subtree), then
moves each trace into the one canonical store via T1's ``ingest_trace`` (scope
``("run", run_id)``) reading it straight off the folded mirror. The trace is
EVIDENCE, not a gate:

* a cluster run with per-task ``_trace.jsonl`` → folded-in + ingested + journaled;
* absent trace files → silent, the harvest is byte-identical;
* a torn / schema-invalid trace → ``ingest_trace`` refuses it (T1 strict) →
  a DISCLOSED skip, the harvest stays green;
* no double-ingest on re-harvest (the persistent cluster copy is re-pulled
  every harvest; the store-existence guard makes the second ingest a no-op);
* the ingest reads from the already-pulled mirror AFTER the metrics reduce and
  never blocks it.

``rsync_pull`` is mocked exactly as ``test_flow_ssh_default_reducer``: the
``_combiner`` pull 404s (combiner never ran); the ``results`` pull is the ONE
folded cycle whose ``include`` carries ``metrics.json``, ``.hpc_cmd_sha`` AND
``_trace.jsonl``, and it materialises both the per-task ``metrics.json`` and
``_trace.jsonl`` fixtures.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.execution.mapreduce.data_trace_contract import TRACE_TRANSPORT_FILENAME
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.state.data_trace import make_record, read_trace, trace_store_path
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    pass

_RUN_ID = "20260623-120000-pi0"

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


def _valid_trace_text() -> str:
    """Two valid stage-exit records — a load then a filter dropping 10 rows."""
    recs = [
        make_record("load", 0, {"row_count": {"rows": 100, "dropped": 0}}),
        make_record("filter", 1, {"row_count": {"rows": 90, "dropped": 10}}),
    ]
    return "".join(json.dumps(r) + "\n" for r in recs)


_TORN_TRACE_TEXT = '{"stage": "load", "seq": 0, "atoms":\n'  # truncated JSON


def _rsync_stub(
    metrics: list[dict] | None,
    traces: dict[int, str] | None,
    *,
    results_pull_rc: int = 0,
    results_pull_raises: bool = False,
):
    """rsync_pull stub: _combiner 404s; the ONE folded results pull writes both.

    ``metrics`` — one metrics.json per list index (task-<i>). ``traces`` — a
    ``{task_index: file_text}`` map written as ``task-<i>/_trace.jsonl``. Both
    ride the SAME ``results`` pull (F1 include-fold): its ``include`` names
    ``metrics.json``, ``.hpc_cmd_sha`` and ``_trace.jsonl`` together, so the stub
    materialises every fixture in one cycle rather than splitting on include.
    ``results_pull_rc`` non-zero simulates the folded pull failing;
    ``results_pull_raises`` makes it raise OSError.
    """

    def _stub(*_a, remote_subdir: str, local_dir: str, include=None, **_kw):
        dest = Path(local_dir)
        if remote_subdir == "_combiner":
            return subprocess.CompletedProcess(
                args=[], returncode=23, stdout="", stderr="No such file or directory (2)"
            )
        if remote_subdir == "results":
            if results_pull_raises:
                raise OSError("simulated transport explosion")
            if results_pull_rc != 0:
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=results_pull_rc,
                    stdout="",
                    stderr="No such file or directory (2)",
                )
            dest.mkdir(parents=True, exist_ok=True)
            for i, m in enumerate(metrics or []):
                td = dest / f"task-{i}"
                td.mkdir(parents=True, exist_ok=True)
                (td / "metrics.json").write_text(json.dumps(m), encoding="utf-8")
            for i, text in (traces or {}).items():
                td = dest / f"task-{i}"
                td.mkdir(parents=True, exist_ok=True)
                (td / TRACE_TRANSPORT_FILENAME).write_text(text, encoding="utf-8")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        dest.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    return _stub


def _data_trace_records(experiment: Path) -> list[dict]:
    return [d for d in read_decisions(experiment, "run", _RUN_ID) if d.get("block") == "data-trace"]


def _metrics() -> list[dict]:
    return [{"pi_estimate": v, "n_samples": 1} for v in _PI_VALUES]


def test_cluster_traces_pulled_ingested_and_journaled(journal_home, experiment, monkeypatch):
    """Per-task _trace.jsonl → pulled, ingested into the store, journaled per task."""
    _seed_run(experiment)
    _seed_sidecar_no_reducer(experiment)

    traces = {i: _valid_trace_text() for i in range(len(_PI_VALUES))}
    monkeypatch.setattr(af_module, "rsync_pull", _rsync_stub(_metrics(), traces))

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    # Harvest still computes the correct deterministic mean.
    assert result.aggregated_metrics[_RUN_ID]["pi_estimate"] == pytest.approx(_EXPECTED_MEAN)

    # Every task's trace landed in the store under ("run", run_id).
    for i in range(len(_PI_VALUES)):
        assert trace_store_path(experiment, "run", _RUN_ID, i).exists()
        assert len(read_trace(experiment, "run", _RUN_ID, i)) == 2

    # One journaled sha record per task, block="data-trace".
    journaled = _data_trace_records(experiment)
    assert len(journaled) == len(_PI_VALUES)
    tasks = {d["resolved"]["task"] for d in journaled}
    assert tasks == set(range(len(_PI_VALUES)))
    for d in journaled:
        assert d["resolved"]["scope"] == "run"
        assert d["resolved"]["stage_count"] == 2
        assert d["resolved"]["trace_sha"]


def test_absent_traces_are_silent_harvest_identical(journal_home, experiment, monkeypatch):
    """No _trace.jsonl in the tree (the folded include matched none) → no store, no journal."""
    _seed_run(experiment)
    _seed_sidecar_no_reducer(experiment)

    monkeypatch.setattr(af_module, "rsync_pull", _rsync_stub(_metrics(), None))

    result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    assert result.aggregated_metrics[_RUN_ID]["pi_estimate"] == pytest.approx(_EXPECTED_MEAN)
    # Nothing ingested, nothing journaled — the non-emitting run is silent.
    assert not (experiment / ".hpc" / "traces").exists()
    assert _data_trace_records(experiment) == []


def test_torn_trace_is_disclosed_skip_harvest_green(journal_home, experiment, monkeypatch, caplog):
    """A torn trace file → ingest refuses (T1 strict) → disclosed skip; harvest green."""
    _seed_run(experiment)
    _seed_sidecar_no_reducer(experiment)

    traces = {i: _valid_trace_text() for i in range(len(_PI_VALUES))}
    traces[0] = _TORN_TRACE_TEXT  # task-0 is torn
    monkeypatch.setattr(af_module, "rsync_pull", _rsync_stub(_metrics(), traces))

    with caplog.at_level(logging.WARNING, logger="hpc_agent.ops.aggregate_flow"):
        result = aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    # Harvest unaffected.
    assert result.aggregated_metrics[_RUN_ID]["pi_estimate"] == pytest.approx(_EXPECTED_MEAN)

    # Torn task-0 refused (not in store, not journaled); the other 9 ingested.
    assert not trace_store_path(experiment, "run", _RUN_ID, 0).exists()
    for i in range(1, len(_PI_VALUES)):
        assert trace_store_path(experiment, "run", _RUN_ID, i).exists()
    journaled = _data_trace_records(experiment)
    assert {d["resolved"]["task"] for d in journaled} == set(range(1, len(_PI_VALUES)))

    # The skip was disclosed.
    assert any("disclosed skip" in r.message or "invalid" in r.message for r in caplog.records)


def test_no_double_ingest_on_reharvest(journal_home, experiment, monkeypatch):
    """Re-harvest re-pulls the persistent cluster _trace.jsonl; the store guard is a no-op."""
    _seed_run(experiment)
    _seed_sidecar_no_reducer(experiment)

    traces = {i: _valid_trace_text() for i in range(len(_PI_VALUES))}
    monkeypatch.setattr(af_module, "rsync_pull", _rsync_stub(_metrics(), traces))

    aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))
    aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    # Store not doubled — still two records per task, not four.
    for i in range(len(_PI_VALUES)):
        assert len(read_trace(experiment, "run", _RUN_ID, i)) == 2
    # Journal not doubled — still one record per task.
    assert len(_data_trace_records(experiment)) == len(_PI_VALUES)


def test_per_task_fallback_folds_metrics_trace_cmd_sha_into_one_pull_cycle(
    journal_home, experiment, monkeypatch
):
    """F1 acceptance: metrics + ``.hpc_cmd_sha`` + ``_trace.jsonl`` ride ONE pull
    cycle (was two — a metrics pull then a separate trace pull over the identical
    ``results/`` subtree). All three families land in the single mirror, and the
    trace is ingested from that mirror with NO extra round-trip.
    """
    from types import SimpleNamespace

    pull_includes: list[list[str]] = []

    def _counting_pull(*_a, remote_subdir: str, local_dir: str, include=None, **_kw):
        pull_includes.append(list(include or []))
        dest = Path(local_dir)
        td = dest / "task-0"
        td.mkdir(parents=True, exist_ok=True)
        # The ONE folded pull delivers every family named in its include.
        if include and "metrics.json" in include:
            (td / "metrics.json").write_text(
                json.dumps({"pi_estimate": 3.14159, "n_samples": 1}), encoding="utf-8"
            )
        if include and af_module.PER_TASK_CMD_SHA_FILENAME in include:
            (td / af_module.PER_TASK_CMD_SHA_FILENAME).write_text("a" * 64, encoding="utf-8")
        if include and TRACE_TRANSPORT_FILENAME in include:
            (td / TRACE_TRANSPORT_FILENAME).write_text(_valid_trace_text(), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(af_module, "rsync_pull", _counting_pull)

    record = SimpleNamespace(ssh_target="u@h", remote_path="/remote", total_tasks=1)
    out = experiment / "agg_out"
    result = af_module._per_task_metrics_reduce(
        experiment,
        _RUN_ID,
        record=record,
        out=out,
        results_subdir="results",
        summary_name="metrics.json",
    )

    # Exactly ONE pull cycle — the fold — not a metrics pull THEN a trace pull.
    assert len(pull_includes) == 1
    # Its include names all three artifact families.
    assert set(pull_includes[0]) == {
        "metrics.json",
        af_module.PER_TASK_CMD_SHA_FILENAME,
        TRACE_TRANSPORT_FILENAME,
    }
    # All three families landed locally from the single cycle: the summary +
    # fingerprint stay in the mirror; the trace was MOVED on into the canonical
    # store by the T4 ingest ("emission is transport"), so it lives there now.
    task0 = out / af_module.PER_TASK_RESULTS_DIRNAME / "task-0"
    assert (task0 / "metrics.json").is_file()
    assert (task0 / af_module.PER_TASK_CMD_SHA_FILENAME).is_file()
    # Metrics reduced AND the trace ingested from the folded mirror (no 2nd pull).
    assert result[_RUN_ID]["pi_estimate"] == pytest.approx(3.14159)
    assert trace_store_path(experiment, "run", _RUN_ID, 0).exists()


def test_canary_sibling_trace_is_excluded(journal_home, experiment, monkeypatch):
    """A <run_id>-canary/_trace.jsonl shares the results subtree — excluded like its metrics."""
    _seed_run(experiment)
    _seed_sidecar_no_reducer(experiment)

    traces = {i: _valid_trace_text() for i in range(len(_PI_VALUES))}
    monkeypatch.setattr(af_module, "rsync_pull", _rsync_stub(_metrics(), traces))

    # Inject a canary sibling trace into the pulled tree via a wrapping stub.
    base = _rsync_stub(_metrics(), traces)

    def _with_canary(*a, remote_subdir: str, local_dir: str, include=None, **kw):
        res = base(*a, remote_subdir=remote_subdir, local_dir=local_dir, include=include, **kw)
        if remote_subdir == "results" and include and TRACE_TRANSPORT_FILENAME in include:
            cdir = Path(local_dir) / f"{_RUN_ID}-canary" / "task-0"
            cdir.mkdir(parents=True, exist_ok=True)
            (cdir / TRACE_TRANSPORT_FILENAME).write_text(_valid_trace_text(), encoding="utf-8")
        return res

    monkeypatch.setattr(af_module, "rsync_pull", _with_canary)

    aggregate_flow(experiment, spec=AggregateFlowSpec(run_id=_RUN_ID))

    # The main run's 10 tasks ingested; the canary sibling's trace did NOT
    # journal a foreign record (still exactly 10).
    assert len(_data_trace_records(experiment)) == len(_PI_VALUES)
