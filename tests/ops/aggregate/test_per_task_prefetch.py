"""Incremental harvest — the aggregate-side MECHANISM (U4).

``prefetch_per_task_results`` is the mid-flight byte-mover the monitor watch
drives as tasks finish. It is the sibling of ``prefetch_wave_partials`` one
tier down: that one caches COMBINED wave partials and is therefore a no-op for
exactly the runs the 2026-07-30 trainwreck hit (no ``wave_map``, no
cluster-side combiner, 1741 finished results unreadable for 2h+); this one
caches the RAW per-task sidecars every array writes.

Pinned here:

* **one pull shape, one definition** — the streamed (remote subdir, include
  filter, destination) triple is BYTE-IDENTICAL to the terminal harvest's, so
  the harvest's content-hash delta sees the whole streamed mirror. A streamed
  set that differed would leave the delta blind to part of the cache;
* **the final harvest is a no-op delta when streaming kept up** — and, on the
  content-hash engine, bytes pulled never exceed bytes produced: an identical
  file is served from the local mirror, never re-transferred;
* the best-effort contract — every transport fault returns as disclosed data,
  never raised into the watch;
* ``HPC_INCREMENTAL_HARVEST=0`` opts out entirely;
* the honest ``tasks_mirrored`` count is read off the LOCAL mirror.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent.ops import aggregate_flow as af_module

_RUN_ID = "20260730-090000-u4"


def _version() -> str:
    """This build's own version — a sidecar stamped with a FUTURE version warns
    on read, and a warning in an unrelated fixture is noise that trains readers
    to ignore warnings."""
    from hpc_agent import __version__

    return __version__


def _record(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "cluster": "no-such-cluster-key",  # resolve_ssh_target falls back to ssh_target
        "ssh_target": "user@host",
        "remote_path": "/remote/exp",
        "combined_waves": [],
        "total_tasks": 2100,
        "result_dir_template": "results/{run_id}/task_{task_id}",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _outcome(**overrides: Any) -> af_module._PullOutcome:
    base: dict[str, Any] = {
        "returncode": 0,
        "stderr": "",
        "files_pulled": 0,
        "bytes_pulled": 0,
        "skipped_unchanged": 0,
    }
    base.update(overrides)
    return af_module._PullOutcome(**base)


def _pull_recorder(
    monkeypatch: pytest.MonkeyPatch, result: Any = None, *, raise_exc: Exception | None = None
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake(**kw: Any) -> Any:
        calls.append(kw)
        if raise_exc is not None:
            raise raise_exc
        return result if result is not None else _outcome()

    monkeypatch.setattr(af_module, "_pull", _fake)
    return calls


# ---------------------------------------------------------------------------
# ONE pull shape, shared by definition with the terminal harvest
# ---------------------------------------------------------------------------


def test_include_triple_is_one_definition() -> None:
    """The streamed include list IS the terminal harvest's. The harvest builds
    its own ``include`` from this same function, so the two can never drift
    apart without this pin moving too."""
    assert af_module.per_task_prefetch_include("metrics.json") == [
        "metrics.json",
        af_module.PER_TASK_CMD_SHA_FILENAME,
        "_trace.jsonl",
    ]


def test_prefetch_pulls_the_harvest_shape_into_the_default_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streamed (subdir, include, destination) triple is the harvest's own
    — the run-SCOPED results subtree, the shared include triple, and the
    default ``_aggregated/<run_id>/_per_task_results`` mirror."""
    calls = _pull_recorder(monkeypatch)

    res = af_module.prefetch_per_task_results(
        tmp_path, _RUN_ID, record=_record(), summary_name="metrics.json"
    )

    assert res is not None and res["ok"] is True
    assert len(calls) == 1
    kw = calls[0]
    assert kw["ssh_target"] == "user@host"
    assert kw["remote_path"] == "/remote/exp"
    # Run-scoped, not the shared ``results`` root (run-15 pull root-scoping):
    # the template's known placeholders render before the cut.
    assert kw["remote_subdir"] == f"results/{_RUN_ID}"
    assert kw["include"] == af_module.per_task_prefetch_include("metrics.json")
    assert kw["local_dir"] == str(af_module.per_task_results_mirror(tmp_path, _RUN_ID))


def test_destination_matches_the_terminal_harvest_default_out() -> None:
    """``_aggregate_flow_impl``'s default ``out`` is
    ``<experiment>/_aggregated/<run_id>``, and the mirror hangs off it. If this
    moved, the harvest would pull into a directory the stream never filled."""
    assert (
        af_module.per_task_results_mirror(Path("/exp"), _RUN_ID)
        == Path("/exp") / "_aggregated" / _RUN_ID / af_module.PER_TASK_RESULTS_DIRNAME
    )


def test_non_run_scoped_template_degrades_to_the_results_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same degradation the harvest takes — never a DIFFERENT scope, which
    would make the harvest's delta re-pull everything the stream landed."""
    calls = _pull_recorder(monkeypatch)
    af_module.prefetch_per_task_results(
        tmp_path,
        _RUN_ID,
        record=_record(result_dir_template=None),
        summary_name="metrics.json",
    )
    assert calls[0]["remote_subdir"] == "results"


def test_summary_name_is_resolved_from_the_sidecar_when_not_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose executor emits ``results_reduce.json`` must stream THAT, or
    the mirror would hold none of the files the harvest reduces."""
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version=_version(),
        submitted_at="2026-07-30T09:00:00Z",
        executor="python3 run.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=4,
        tasks_py_sha="1" * 64,
        summary_artifact="results_reduce.json",
    )
    calls = _pull_recorder(monkeypatch)

    af_module.prefetch_per_task_results(tmp_path, _RUN_ID, record=_record())

    assert calls[0]["include"][0] == "results_reduce.json"


def test_absent_sidecar_degrades_to_the_default_summary_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streaming is an optimization: a missing sidecar must degrade, never
    raise into the watch."""
    calls = _pull_recorder(monkeypatch)
    res = af_module.prefetch_per_task_results(tmp_path, _RUN_ID, record=_record())
    assert res is not None and res["ok"] is True
    assert calls[0]["include"][0] == "metrics.json"


# ---------------------------------------------------------------------------
# best-effort: every fault is DATA
# ---------------------------------------------------------------------------


def test_nonzero_returncode_is_disclosed_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pull_recorder(monkeypatch, _outcome(returncode=23, stderr="rsync: connection reset"))
    res = af_module.prefetch_per_task_results(
        tmp_path, _RUN_ID, record=_record(), summary_name="metrics.json"
    )
    assert res == {
        "ok": False,
        "error": "rsync: connection reset",
        "dir": str(af_module.per_task_results_mirror(tmp_path, _RUN_ID)),
    }


@pytest.mark.parametrize(
    "exc",
    [
        errors.SshUnreachable("host down"),
        errors.SshCircuitOpen("breaker open"),
        TimeoutError("client deadline"),
        OSError("broken pipe"),
        ValueError("bad target"),
    ],
)
def test_transport_exceptions_are_disclosed_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """A transport fault must never unwind the poll loop — the watch outliving
    a flap is worth more than any single streamed batch."""
    _pull_recorder(monkeypatch, raise_exc=exc)
    res = af_module.prefetch_per_task_results(
        tmp_path, _RUN_ID, record=_record(), summary_name="metrics.json"
    )
    assert res is not None and res["ok"] is False
    assert res["error"]


def test_opt_out_env_returns_none_without_touching_the_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(af_module.PER_TASK_PREFETCH_ENV, "0")
    calls = _pull_recorder(monkeypatch)
    assert (
        af_module.prefetch_per_task_results(
            tmp_path, _RUN_ID, record=_record(), summary_name="metrics.json"
        )
        is None
    )
    assert calls == []


# ---------------------------------------------------------------------------
# honest accounting
# ---------------------------------------------------------------------------


def _land(mirror: Path, task_id: int, payload: dict[str, Any]) -> None:
    d = mirror / _RUN_ID / f"task_{task_id}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "metrics.json").write_text(json.dumps(payload), encoding="utf-8")


def test_tasks_mirrored_counts_the_local_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``M pulled locally`` is read off DISK, so it reports what is genuinely
    readable now rather than what some earlier tick claimed to have pulled."""
    mirror = af_module.per_task_results_mirror(tmp_path, _RUN_ID)

    def _fake(**_kw: Any) -> Any:
        for i in range(3):
            _land(mirror, i, {"n": 1, "acc": 0.5})
        return _outcome(files_pulled=3, bytes_pulled=300)

    monkeypatch.setattr(af_module, "_pull", _fake)

    res = af_module.prefetch_per_task_results(
        tmp_path, _RUN_ID, record=_record(), summary_name="metrics.json"
    )
    assert res is not None
    assert res["tasks_mirrored"] == 3
    assert res["files_pulled"] == 3
    assert res["bytes_pulled"] == 300


def test_delta_accounting_passes_through_skipped_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent cache must never masquerade as a fresh pull: the row says how
    much was served from the mirror versus transferred."""
    _pull_recorder(monkeypatch, _outcome(files_pulled=0, bytes_pulled=0, skipped_unchanged=900))
    res = af_module.prefetch_per_task_results(
        tmp_path, _RUN_ID, record=_record(), summary_name="metrics.json"
    )
    assert res is not None
    assert res["bytes_pulled"] == 0
    assert res["skipped_unchanged"] == 900


def test_mirrored_count_survives_an_unreadable_mirror(tmp_path: Path) -> None:
    """A disclosure counter never raises: an absent mirror reads as 0."""
    assert af_module._mirrored_task_count(tmp_path / "nope", "metrics.json") == 0


# ---------------------------------------------------------------------------
# the pin: streaming keeps up -> the terminal harvest is a no-op delta, and
# no byte is pulled twice
# ---------------------------------------------------------------------------


def test_streamed_mirror_makes_the_terminal_harvest_a_noop_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point. A watch that streamed N tasks leaves the terminal
    harvest with an EMPTY delta over the same tree — bytes pulled across the
    run equal the bytes the tasks produced, never a multiple of them.

    Modelled on the content-hash engine's real contract (pinned at the
    transport level by ``tests/infra/test_transport_pull.py``
    ``::test_delta_nothing_to_pull_when_all_identical``): a file already
    identical locally is SKIPPED, not re-transferred.
    """
    mirror = af_module.per_task_results_mirror(tmp_path, _RUN_ID)
    remote: dict[int, dict[str, Any]] = {}
    transferred: list[tuple[str, int]] = []

    def _fake_delta_pull(*, local_dir: str, **_kw: Any) -> Any:
        """A content-hash delta: land only the files not already identical."""
        dest = Path(local_dir)
        pulled = skipped = pulled_bytes = 0
        for tid, payload in remote.items():
            body = json.dumps(payload)
            target = dest / _RUN_ID / f"task_{tid}" / "metrics.json"
            if target.is_file() and target.read_text(encoding="utf-8") == body:
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            pulled += 1
            pulled_bytes += len(body)
            transferred.append((f"task_{tid}", len(body)))
        return _outcome(files_pulled=pulled, bytes_pulled=pulled_bytes, skipped_unchanged=skipped)

    monkeypatch.setattr(af_module, "_pull", _fake_delta_pull)

    # A completion trickle: three streaming pulls as tasks land.
    produced_bytes = 0
    for batch in (range(0, 4), range(4, 9), range(9, 12)):
        for tid in batch:
            remote[tid] = {"n": 1, "task": tid}
            produced_bytes += len(json.dumps(remote[tid]))
        res = af_module.prefetch_per_task_results(
            tmp_path, _RUN_ID, record=_record(), summary_name="metrics.json"
        )
        assert res is not None and res["ok"] is True

    assert af_module._mirrored_task_count(mirror, "metrics.json") == 12
    streamed_bytes = sum(n for _, n in transferred)
    # Never double-pulled: each task's summary crossed the wire exactly once.
    assert [name for name, _ in transferred] == [f"task_{i}" for i in range(12)]
    assert streamed_bytes == produced_bytes

    # The terminal harvest re-runs the SAME pull over the SAME destination.
    final = af_module.prefetch_per_task_results(
        tmp_path, _RUN_ID, record=_record(), summary_name="metrics.json"
    )
    assert final is not None
    assert final["files_pulled"] == 0  # no-op delta
    assert final["bytes_pulled"] == 0
    assert final["skipped_unchanged"] == 12
    # And nothing crossed the wire a second time.
    assert sum(n for _, n in transferred) == produced_bytes


def test_a_changed_source_piece_is_re_pulled_by_the_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streamed pieces are a CACHE, never an authority. A graft/repair that
    rewrote a task's summary must still reach the mirror — the content hash
    moved, so the delta re-pulls it."""
    remote: dict[int, dict[str, Any]] = {0: {"n": 1, "acc": 0.1}}
    hits: list[str] = []

    def _fake_delta_pull(*, local_dir: str, **_kw: Any) -> Any:
        dest = Path(local_dir)
        pulled = skipped = 0
        for tid, payload in remote.items():
            body = json.dumps(payload)
            target = dest / _RUN_ID / f"task_{tid}" / "metrics.json"
            if target.is_file() and target.read_text(encoding="utf-8") == body:
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            hits.append(f"task_{tid}")
            pulled += 1
        return _outcome(files_pulled=pulled, skipped_unchanged=skipped)

    monkeypatch.setattr(af_module, "_pull", _fake_delta_pull)

    af_module.prefetch_per_task_results(
        tmp_path, _RUN_ID, record=_record(), summary_name="metrics.json"
    )
    remote[0] = {"n": 1, "acc": 0.9}  # the graft
    res = af_module.prefetch_per_task_results(
        tmp_path, _RUN_ID, record=_record(), summary_name="metrics.json"
    )

    assert hits == ["task_0", "task_0"]
    assert res is not None and res["files_pulled"] == 1
