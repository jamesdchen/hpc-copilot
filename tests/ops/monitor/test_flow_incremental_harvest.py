"""Incremental harvest — the WATCH-side trigger (U4).

The forensic case this closes (``docs/plans/trainwreck-audit-2026-07-30.md``
U4): 2100/2100 tasks finished cleanly, and 1741 of those results sat on cluster
scratch unreadable for over two hours while the human asked "why aren't you
streaming the results back?". Harvest was all-or-nothing at array completion.

This seam pins the watch-side half: as the completion count grows, the poll
loop pulls the finished tasks' result files into the SAME local mirror the
terminal harvest re-verifies, batched and delta-only.

Deliberately distinct from ``test_flow_wave_prefetch.py``: that trigger keys on
a COMBINE burst and is a structural no-op for a run with no ``wave_map`` and no
cluster-side combiner — precisely the run that got hurt. This one keys on the
completion counts every tick already holds, so it covers plain arrays.

Contract pinned here:

* a completion trickle produces BATCHED pulls (not one per poll), each into the
  harvest's own destination, and never double-pulls an unchanged set;
* a breaker-open host PAUSES the stream — disclosed on the tick row — while the
  watch keeps polling to terminal;
* a streaming failure is disclosed and the run still settles normally;
* the disclosure rides ``MonitorFlowResult`` (and thus the S3 brief) with the
  honest ``tasks_mirrored`` count;
* the opt-outs (env and spec) suppress the stream entirely — zero round trips;
* a pure-API backend never streams (no remote tree to pull from).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._kernel.contract.vocabulary import LifecycleState
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.monitor import stream_harvest as sh
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260730-093000-u4flow"
_TOTAL = 100


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _tight_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Small batch, no spacing floor, and an effectively-infinite staleness
    interval — so these loop tests exercise the SIZE trigger alone without
    faking minutes of wall clock. Both triggers and the floor are pinned
    independently in ``test_stream_harvest.py``."""
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_BATCH_ENV, "10")
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_INTERVAL_ENV, "1000000")
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_FLOOR_ENV, "0")


def _seed_record(experiment_dir: Path, **overrides: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "myjob",
        "job_ids": ["9001"],
        "total_tasks": _TOTAL,
        "submitted_at": "2026-07-30T09:00:00+00:00",
        "experiment_dir": str(experiment_dir),
        "backend": "sge",
        "auto_resume_on_kill": False,
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def _harvest_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        monitor_flow_module,
        "harvest_on_terminal",
        lambda *a, **k: calls.append(k.get("terminal_cause", "?")),
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    return calls


def _present(complete: int, failed: int = 0, total: int = _TOTAL) -> dict[str, int]:
    announced = complete + failed
    return {
        "present": 1,
        "announced": announced,
        "complete": complete,
        "failed": failed,
        "missing": max(0, total - announced),
    }


def _stub_census(monkeypatch: pytest.MonkeyPatch, *census_seq: dict[str, int]) -> None:
    idx = {"n": 0}

    def _fake(*, ssh_target: str, remote_path: str, run_id: str, task_count: int) -> dict[str, int]:
        item = census_seq[min(idx["n"], len(census_seq) - 1)]
        idx["n"] += 1
        return dict(item)

    monkeypatch.setattr(monitor_flow_module, "read_announcements", _fake)


def _moving_clock() -> Any:
    """A monotonic clock that advances a second per read.

    Ticks must be *distinguishable* in time (the streaming gate reads elapsed
    seconds) but must stay well inside the reporter heartbeat — a clock that
    jumps past ``HPC_STATUS_REPORTER_HEARTBEAT_SEC`` sends the loop down the
    full reporter-walk leg, which dials for real and turns a unit test into a
    minute of SSH timeouts.
    """
    t = {"n": 0.0}

    def _now() -> float:
        t["n"] += 1.0
        return t["n"]

    return _now


def _spec(**overrides: Any) -> MonitorFlowSpec:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "poll_interval_seconds": 5,
        "wall_clock_budget_seconds": 10_000_000,
    }
    base.update(overrides)
    return MonitorFlowSpec(**base)


def _stream_pull_recorder(
    monkeypatch: pytest.MonkeyPatch, *, files: int = 5, raise_exc: Exception | None = None
) -> list[dict[str, Any]]:
    """Record the aggregate-side ``_pull`` the stream drives (engine-agnostic)."""
    calls: list[dict[str, Any]] = []

    def _fake(**kw: Any) -> Any:
        calls.append(kw)
        if raise_exc is not None:
            raise raise_exc
        return af_module._PullOutcome(
            returncode=0, stderr="", files_pulled=files, bytes_pulled=files * 100
        )

    monkeypatch.setattr(af_module, "_pull", _fake)
    return calls


def _tick_actions(experiment_dir: Path, kind: str) -> list[dict[str, Any]]:
    from hpc_agent.ops.monitor.tick_log import _tick_log_path

    rows: list[dict[str, Any]] = []
    log = _tick_log_path(experiment_dir, _RUN_ID)
    if not log.is_file():
        return rows
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for action in json.loads(line).get("actions") or []:
            if isinstance(action, dict) and action.get("kind") == kind:
                rows.append(action)
    return rows


# ---------------------------------------------------------------------------
# the trickle
# ---------------------------------------------------------------------------


def test_completion_trickle_streams_in_batches_not_once_per_poll(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticks at 0, 12, 15, 40, 100 complete. With a batch of 10 the stream fires
    on the ticks that ACCRUED a batch (12, 40, 100) and stays silent on the
    3-task tick — the difference between "results arrive as they land" and "one
    ssh round trip per poll against a login node"."""
    _seed_record(experiment)
    _harvest_recorder(monkeypatch)
    _stub_census(
        monkeypatch,
        _present(0),
        _present(12),
        _present(15),
        _present(40),
        _present(100),
    )
    pulls = _stream_pull_recorder(monkeypatch)

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=_moving_clock())

    assert result.lifecycle_state == LifecycleState.COMPLETE
    rows = _tick_actions(experiment, "incremental_harvest")
    assert [r["complete"] for r in rows] == [12, 40, 100]
    assert len(pulls) == 3

    # Every streamed pull is the terminal harvest's own shape.
    for kw in pulls:
        assert kw["remote_subdir"] == "results"
        assert kw["include"] == af_module.per_task_prefetch_include("metrics.json")
        assert kw["local_dir"] == str(af_module.per_task_results_mirror(experiment, _RUN_ID))


def test_an_idle_tail_never_re_pulls(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The never-double-pull invariant at loop level: once 40 tasks are streamed
    the count stops moving, and no number of further polls spends another round
    trip on the same set."""
    _seed_record(experiment)
    _harvest_recorder(monkeypatch)
    _stub_census(
        monkeypatch,
        _present(40),
        _present(40),
        _present(40),
        _present(40),
        _present(100),
    )
    pulls = _stream_pull_recorder(monkeypatch)

    monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=_moving_clock())

    rows = _tick_actions(experiment, "incremental_harvest")
    assert [r["complete"] for r in rows] == [40, 100]
    assert len(pulls) == 2


def test_streaming_bytes_never_exceed_the_bytes_produced(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end over a real content-hash delta model: across the whole watch,
    every task summary crosses the wire exactly ONCE, and the terminal state's
    disclosure reports the full mirror."""
    _seed_record(experiment)
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(20), _present(60), _present(100))

    remote: dict[int, str] = {}
    transferred: list[str] = []
    census_at = iter([20, 60, 100])

    def _fake_delta_pull(*, local_dir: str, **_kw: Any) -> Any:
        from pathlib import Path as _P

        # Materialize the tasks that have finished by now.
        for tid in range(next(census_at, 100)):
            remote.setdefault(tid, json.dumps({"n": 1, "task": tid}))
        dest = _P(local_dir)
        pulled = skipped = pulled_bytes = 0
        for tid, body in remote.items():
            target = dest / f"task_{tid}" / "metrics.json"
            if target.is_file() and target.read_text(encoding="utf-8") == body:
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            transferred.append(f"task_{tid}")
            pulled += 1
            pulled_bytes += len(body)
        return af_module._PullOutcome(
            returncode=0,
            stderr="",
            files_pulled=pulled,
            bytes_pulled=pulled_bytes,
            skipped_unchanged=skipped,
        )

    monkeypatch.setattr(af_module, "_pull", _fake_delta_pull)

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=_moving_clock())

    assert result.lifecycle_state == LifecycleState.COMPLETE
    # No task's summary was transferred twice.
    assert len(transferred) == len(set(transferred)) == 100
    produced = sum(len(b) for b in remote.values())
    assert result.incremental_harvest is not None
    assert result.incremental_harvest["bytes_pulled"] == produced
    assert result.incremental_harvest["tasks_mirrored"] == 100
    assert result.incremental_harvest["pulls"] == 3


def test_the_two_prefetches_coexist_and_pull_different_subtrees(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watch now runs TWO opportunistic pulls, and they must not be
    confused for each other or collapse into one.

    The wave prefetch caches sealed ``_combiner/`` partials on a combine burst;
    this one caches the RAW ``results/`` sidecars on a completion batch. They
    are deliberately BOTH live on a wave run, because the trainwreck's own
    failure was a run WITH waves whose cluster-side combiner was silently
    missing (U5) — the raw per-task mirror is exactly the input the harvest's
    no-combiner fallback then needs, and streaming it means that fallback is
    already warm instead of paying a fresh full pull at the worst moment.
    """
    from hpc_agent.state.runs import write_run_sidecar

    _seed_record(experiment)
    write_run_sidecar(
        experiment,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version=__import__("hpc_agent").__version__,
        submitted_at="2026-07-30T09:00:00Z",
        executor="python3 run.py",
        result_dir_template="results/{task_id}",
        task_count=_TOTAL,
        tasks_py_sha="1" * 64,
        wave_map={"0": list(range(_TOTAL))},
    )
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(_TOTAL))
    monkeypatch.setattr(
        monitor_flow_module, "_census_complete_task_ids", lambda *_a: set(range(_TOTAL))
    )
    monkeypatch.setattr(
        monitor_flow_module,
        "combine_waves",
        lambda *_a, waves, **_k: {w: (True, "", "") for w in waves},
    )
    pulls = _stream_pull_recorder(monkeypatch)

    monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=_moving_clock())

    subdirs = [kw["remote_subdir"] for kw in pulls]
    assert "_combiner" in subdirs, "the wave prefetch must still fire"
    assert "results" in subdirs, "the per-task stream must still fire"
    assert len(_tick_actions(experiment, "prefetch_wave_partials")) == 1
    assert len(_tick_actions(experiment, "incremental_harvest")) == 1


# ---------------------------------------------------------------------------
# breaker: pause the stream, never the watch
# ---------------------------------------------------------------------------


def test_open_breaker_pauses_streaming_without_killing_the_watch(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host's circuit is open for the whole watch. The stream records a
    DISCLOSED pause on every eligible tick, spends ZERO transport calls, and the
    run still polls through to a normal terminal — the watch outliving a flap is
    worth more than any single streamed batch."""
    _seed_record(experiment)
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(20), _present(60), _present(100))
    pulls = _stream_pull_recorder(monkeypatch)
    monkeypatch.setattr(monitor_flow_module, "stream_blocked_by", lambda _t: "ssh_circuit_open")

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=_moving_clock())

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert pulls == []  # the breaker's cooldown was RESPECTED, not probed
    paused = _tick_actions(experiment, "incremental_harvest_paused")
    assert paused and all(r["reason"] == "ssh_circuit_open" for r in paused)
    assert result.incremental_harvest is not None
    assert result.incremental_harvest["paused_reason"] == "ssh_circuit_open"
    assert result.incremental_harvest["pulls"] == 0


def test_streaming_resumes_once_the_breaker_closes(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pause is a pause. When the circuit closes the stream picks the backlog
    straight up, and the stale pause reason is CLEARED so the brief cannot keep
    reporting a pause that ended."""
    _seed_record(experiment)
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(20), _present(60), _present(100))
    pulls = _stream_pull_recorder(monkeypatch)
    states = iter(["ssh_circuit_open", None, None])
    monkeypatch.setattr(monitor_flow_module, "stream_blocked_by", lambda _t: next(states, None))

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=_moving_clock())

    assert len(_tick_actions(experiment, "incremental_harvest_paused")) == 1
    assert len(pulls) == 2
    assert result.incremental_harvest is not None
    assert result.incremental_harvest["paused_reason"] is None
    assert result.incremental_harvest["pulls"] == 2


def test_stream_failure_is_disclosed_and_the_run_still_settles(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport fault inside the stream is DATA on the tick row, never an
    exception into the poll loop."""
    _seed_record(experiment)
    harvests = _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(20), _present(100))
    _stream_pull_recorder(monkeypatch, raise_exc=errors.SshUnreachable("host down"))

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=_moving_clock())

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert harvests == ["complete"]
    failed = _tick_actions(experiment, "incremental_harvest_failed")
    assert failed and "host down" in failed[0]["error"]
    assert result.incremental_harvest is not None
    assert "host down" in (result.incremental_harvest["last_error"] or "")


# ---------------------------------------------------------------------------
# every terminal path must carry the disclosure
# ---------------------------------------------------------------------------


def test_timeout_envelope_carries_the_disclosure(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the REAL loop to budget exhaustion and read the envelope.

    The TIMEOUT return is the one the human meets at "keep watching or stop?" —
    the exact moment the pulled-count decides the answer — and it was the one
    construction site of six that omitted the block, so the relay arm rendered
    nothing. A test that hand-feeds a brief to ``render_relay`` cannot catch
    that: only driving ``monitor_flow`` itself to the budget can.
    """
    _seed_record(experiment)
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(30), _present(40), _present(50))
    _stream_pull_recorder(monkeypatch, files=3)

    # A budget small enough that the loop times out while tasks are still
    # pending — the genuine in-flight timeout, not a completed run.
    result = monitor_flow(
        experiment,
        spec=_spec(wall_clock_budget_seconds=5),
        _sleep=lambda s: None,
        _now=_moving_clock(),
    )

    assert result.lifecycle_state == LifecycleState.TIMEOUT
    assert result.incremental_harvest is not None, (
        "the TIMEOUT envelope must carry the pull-lag block — this is the "
        "return the 'keep watching or stop?' brief is built from"
    )
    assert result.incremental_harvest["enabled"] is True
    assert result.incremental_harvest["pulls"] >= 1
    assert result.to_envelope_data()["incremental_harvest"] == result.incremental_harvest


def test_the_timeout_brief_renders_the_lag_end_to_end(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the blocking fixture: the disclosure the real loop
    produced, fed to the real renderer, actually reaches the human's line."""
    from hpc_agent.ops.relay_render import render_relay

    _seed_record(experiment)
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(30), _present(40), _present(50))

    def _fake(**_kw: Any) -> Any:
        mirror = af_module.per_task_results_mirror(experiment, _RUN_ID)
        for i in range(7):
            d = mirror / f"task_{i}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "metrics.json").write_text("{}", encoding="utf-8")
        return af_module._PullOutcome(returncode=0, stderr="", files_pulled=7, bytes_pulled=700)

    monkeypatch.setattr(af_module, "_pull", _fake)

    result = monitor_flow(
        experiment,
        spec=_spec(wall_clock_budget_seconds=5),
        _sleep=lambda s: None,
        _now=_moving_clock(),
    )

    assert result.lifecycle_state == LifecycleState.TIMEOUT
    line = render_relay(
        "s3",
        "watching_timeout",
        {
            "cluster": "hoffman2",
            "main_run_id": _RUN_ID,
            "incremental_harvest": result.incremental_harvest,
        },
    )
    assert "7 pulled locally" in line
    assert "keep watching or stop?" in line


def test_every_result_construction_discloses_the_stream(
    journal_home: Path, experiment: Path
) -> None:
    """SOURCE CENSUS — the class, not just the one site that was missing it.

    ``incremental_harvest`` defaults to ``None``, so a construction site that
    forgets it fails SILENTLY: the envelope validates, the relay arm just
    renders nothing. Six sites exist today and one of them shipped without the
    field. Rather than trust the next one to remember, assert that every
    ``MonitorFlowResult(...)`` built inside the loop passes it.
    """
    import ast
    import inspect

    source = inspect.getsource(monitor_flow_module)
    tree = ast.parse(source)
    sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "MonitorFlowResult"
    ]
    assert len(sites) >= 6, "expected the loop's terminal returns; did the shape change?"
    missing = [
        node.lineno
        for node in sites
        if "incremental_harvest" not in {kw.arg for kw in node.keywords if kw.arg}
    ]
    assert not missing, (
        f"MonitorFlowResult built without incremental_harvest at line(s) {missing} — "
        "a terminal path that silently drops the pull-lag disclosure"
    )


# ---------------------------------------------------------------------------
# bytes, never verdicts
# ---------------------------------------------------------------------------


def test_the_stream_path_settles_nothing_and_reduces_nothing(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MECHANIZED bytes-never-verdicts.

    The whole safety claim of this unit is that it moves bytes and touches no
    verdict: partial-set aggregation stays gated exactly where it was
    (decide-partial-handling / aggregate-run's terminal-or-explicitly-partial
    invariant). That claim was PROSE — a ``mark_terminal`` inserted into the
    stream success path passed the entire suite. Spy on the verdict-writing and
    reducing seams and assert the stream never reaches them.
    """
    from hpc_agent.execution.mapreduce.reduce import metrics as metrics_module
    from hpc_agent.state.journal import load_run

    _seed_record(experiment)
    record = load_run(experiment, _RUN_ID)

    forbidden: list[str] = []
    # Spy on BOTH bindings of the verdict writer — the stream helper lives in
    # monitor_flow, the pull it drives lives in aggregate_flow, and a mutation
    # could be inserted in either. Scoping the spies to a DIRECT call of the
    # stream helper (rather than a whole monitor_flow run) is what makes them
    # sharp: the loop's own legitimate mark_terminal at the terminal branch
    # would otherwise mask a mutation planted in the stream path.
    for module in (monitor_flow_module, af_module):
        monkeypatch.setattr(
            module, "mark_terminal", lambda *a, **k: forbidden.append("mark_terminal")
        )
    monkeypatch.setattr(
        metrics_module, "reduce_metrics", lambda *a, **k: forbidden.append("reduce_metrics")
    )
    monkeypatch.setattr(
        metrics_module, "reduce_partials", lambda *a, **k: forbidden.append("reduce_partials")
    )
    monkeypatch.setattr(
        af_module, "reduce_metrics", lambda *a, **k: forbidden.append("reduce_metrics")
    )
    monkeypatch.setattr(
        af_module, "reduce_partials", lambda *a, **k: forbidden.append("reduce_partials")
    )
    monkeypatch.setattr(
        af_module,
        "_pull",
        lambda **_kw: af_module._PullOutcome(
            returncode=0, stderr="", files_pulled=4, bytes_pulled=400
        ),
    )

    state = monitor_flow_module._LoopState(stream_enabled=True)
    state.last_summary = {"complete": 60}
    action = monitor_flow_module._stream_finished_results(
        experiment, _RUN_ID, record=record, state=state, now_mono=1000.0
    )

    assert action is not None and action["kind"] == "incremental_harvest", (
        "the stream must actually have run, or this guard proves nothing"
    )
    assert forbidden == [], (
        f"the incremental harvest reached a verdict/reduce seam: {forbidden} — "
        "this unit moves BYTES; it must never settle a run or compute an aggregate"
    )


def test_the_stream_module_imports_no_reducer_or_journal_writer() -> None:
    """Import census on the policy module: the gate decides WHEN to move bytes
    and must not acquire the vocabulary to decide anything else."""
    import ast
    import inspect

    from hpc_agent.ops.monitor import stream_harvest

    tree = ast.parse(inspect.getsource(stream_harvest))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    banned = {"reduce", "aggregate", "journal", "decision", "combine"}
    offenders = [m for m in imported if any(b in m for b in banned)]
    assert not offenders, (
        f"stream_harvest imports {offenders} — the streaming GATE must hold no "
        "reduce/aggregate/journal vocabulary; it decides only when to pull"
    )


# ---------------------------------------------------------------------------
# loop-start seeding + the first-attempt sentinel
# ---------------------------------------------------------------------------


def test_a_rearmed_watch_reports_the_mirror_it_inherited(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watch that re-arms over a warm mirror must report what is genuinely
    readable, not a fictitious zero.

    The trainwreck's watch died and was re-driven nine times; a disclosure that
    reset to 0 on every re-arm would have told the human nothing had come home
    when in fact plenty had. Seeded at loop start from disk, zero SSH.
    """
    _seed_record(experiment)
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(100))

    mirror = af_module.per_task_results_mirror(experiment, _RUN_ID)
    for i in range(12):
        d = mirror / f"task_{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "metrics.json").write_text("{}", encoding="utf-8")

    # A stream that pulls NOTHING new (steady state): the reported count must
    # still be the inherited 12, which can only come from the loop-start seed.
    monkeypatch.setattr(
        af_module,
        "prefetch_per_task_results",
        lambda *a, **k: None,  # disabled underneath -> no pull, no count refresh
    )

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=_moving_clock())

    assert result.incremental_harvest is not None
    assert result.incremental_harvest["tasks_mirrored"] == 12, (
        "a re-armed watch must inherit the mirror count from disk; reporting 0 "
        "would understate what the human can already read"
    )


def test_the_first_stream_is_not_held_for_a_spacing_floor(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOOP-level pin of the never-streamed sentinel.

    A fresh watch has no "seconds since last stream" — it has an unbounded
    backlog age. With a spacing floor LONGER than the whole watch, the first
    batch must still go home immediately: a floor exists to space REPEATS, and
    holding the first bytes for one is the very latency this unit removes.
    """
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_FLOOR_ENV, "100000")
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_INTERVAL_ENV, "100000")
    _seed_record(experiment)
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(30), _present(100))
    pulls = _stream_pull_recorder(monkeypatch)

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=_moving_clock())

    assert len(pulls) == 1, (
        "the FIRST backlog must stream despite a floor longer than the watch; "
        "subsequent ones are correctly spaced out by it"
    )
    assert result.incremental_harvest is not None
    assert result.incremental_harvest["pulls"] == 1


# ---------------------------------------------------------------------------
# opt-outs
# ---------------------------------------------------------------------------


def test_env_opt_out_spends_no_round_trips(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metered-link escape hatch: nothing is pulled, nothing is disclosed,
    and the run behaves exactly as it does today (later, but identical)."""
    monkeypatch.setenv(sh.INCREMENTAL_HARVEST_ENV, "0")
    _seed_record(experiment)
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(50), _present(100))
    pulls = _stream_pull_recorder(monkeypatch)

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=_moving_clock())

    assert pulls == []
    assert _tick_actions(experiment, "incremental_harvest") == []
    assert result.incremental_harvest is not None
    assert result.incremental_harvest["enabled"] is False


def test_spec_knob_opts_out_over_an_enabling_env(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(sh.INCREMENTAL_HARVEST_ENV, raising=False)
    _seed_record(experiment)
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(50), _present(100))
    pulls = _stream_pull_recorder(monkeypatch)

    result = monitor_flow(
        experiment,
        spec=_spec(incremental_harvest=False),
        _sleep=lambda s: None,
        _now=_moving_clock(),
    )

    assert pulls == []
    assert result.incremental_harvest is not None
    assert result.incremental_harvest["enabled"] is False


def test_pure_api_backend_never_streams(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pure-API backend's results arrive through ``fetch_results``, not over
    ssh — there is no remote tree to stream from, so the gate is off by
    construction rather than by a failed pull."""
    import hpc_agent.infra.backends as backends_module

    _seed_record(experiment)
    # Patch the SOURCE module, which only the streaming gate's lazy lookup
    # reads. Patching the name ``monitor_flow`` bound at import time would
    # divert the whole poll loop down the pure-API status path and this test
    # would stop being about streaming at all.
    monkeypatch.setattr(backends_module, "backend_requires_ssh", lambda _n: False)
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(50), _present(100))
    pulls = _stream_pull_recorder(monkeypatch)

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=_moving_clock())

    assert pulls == []
    assert result.incremental_harvest is not None
    assert result.incremental_harvest["enabled"] is False


# ---------------------------------------------------------------------------
# disclosure reaches the human
# ---------------------------------------------------------------------------


def test_disclosure_rides_the_envelope(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The S3 brief reads this block off ``MonitorFlowResult``; if it never
    reaches the envelope the human is back to asking why nothing streamed."""
    _seed_record(experiment)
    _harvest_recorder(monkeypatch)
    _stub_census(monkeypatch, _present(30), _present(100))
    _stream_pull_recorder(monkeypatch, files=7)

    result = monitor_flow(experiment, spec=_spec(), _sleep=lambda s: None, _now=_moving_clock())

    envelope = result.to_envelope_data()
    assert envelope["incremental_harvest"] == result.incremental_harvest
    assert envelope["incremental_harvest"]["enabled"] is True
    assert envelope["incremental_harvest"]["files_pulled"] == 14  # 2 pulls x 7


def test_relay_line_names_the_pull_lag() -> None:
    """The S3 relay one-liner the agent forwards VERBATIM carries the lag, so a
    human deciding whether to keep waiting can see how much is already home."""
    from hpc_agent.ops.relay_render import render_relay

    line = render_relay(
        "s3",
        "watching_terminal",
        {
            "cluster": "hoffman2",
            "main_run_id": _RUN_ID,
            "total_tasks": 2100,
            "last_status": {"complete": 2100},
            "incremental_harvest": {"enabled": True, "tasks_mirrored": 2100},
        },
    )
    assert "2100/2100 tasks" in line
    assert "2100 pulled locally" in line


def test_relay_line_names_a_paused_stream() -> None:
    """A stalled byte mover must not read as a merely-behind one."""
    from hpc_agent.ops.relay_render import render_relay

    line = render_relay(
        "s3",
        "watching_timeout",
        {
            "cluster": "hoffman2",
            "main_run_id": _RUN_ID,
            "incremental_harvest": {
                "enabled": True,
                "tasks_mirrored": 359,
                "paused_reason": "ssh_circuit_open",
            },
        },
    )
    assert "359 pulled locally" in line
    assert "streaming PAUSED: ssh_circuit_open" in line


def test_relay_line_is_byte_identical_when_nothing_streamed() -> None:
    """A run that never streamed renders exactly today's line."""
    from hpc_agent.ops.relay_render import render_relay

    brief = {
        "cluster": "hoffman2",
        "main_run_id": _RUN_ID,
        "total_tasks": 20,
        "last_status": {"complete": 20},
    }
    with_block = dict(brief, incremental_harvest={"enabled": False, "tasks_mirrored": 0})
    assert render_relay("s3", "watching_terminal", brief) == render_relay(
        "s3", "watching_terminal", with_block
    )
