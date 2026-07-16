"""Run-13 finding 13-addendum: the per-task mirror's staleness fingerprint gate.

``_per_task_metrics_reduce`` mirrors the cluster's per-task summary sidecars into
a PERSISTENT local cache under ``_aggregated/<run_id>/_per_task_results/`` keyed
by task-id. A repair/graft re-run overwrites the SOURCE pieces under ``results/``
but the transport's size+mtime delta can leave the stale cached summary in place
(a torn overwrite whose size+mtime collide — there is no ``--delete``); the
reduce then faithfully reproduces WRONG numbers from it. This was run-13's
stale-table root cause.

The dispatcher stamps a ``.hpc_cmd_sha`` sidecar (the submission cmd_sha) into
each result dir on promote. The gate snapshots those fingerprints before the
pull, carries ``.hpc_cmd_sha`` in the include so it lands on both transports,
and after the pull evicts + clean-re-pulls any task dir whose fingerprint moved,
then invalidates the wave partials that task belongs to.

FIRES: a stale cached piece with a mismatched cmd_sha is evicted and re-pulled,
so the reduce reads the FRESH value (the OLD idempotent-by-task-id cache would
have reduced the stale blown copy).
PASSES: an unchanged piece is NOT re-pulled — the steady-state re-aggregate pays
a single pull.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from hpc_agent.ops import aggregate_flow as agg
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_RUN_ID = "20260101-000000-graftmirror"
_OLD_SHA = "a" * 64
_NEW_SHA = "b" * 64
_STALE = {"metric": 999.0, "n_samples": 1}
_FRESH = {"metric": 7.0, "n_samples": 1}


def _fake_record() -> SimpleNamespace:
    return SimpleNamespace(ssh_target="u@h", remote_path="/remote", total_tasks=2)


def _seed_stale_mirror(out: Path, *, sha: str, payload: dict[str, Any]) -> Path:
    """Pre-seed a prior harvest's cached ``task_0`` piece (summary + fingerprint)."""
    task = out / agg.PER_TASK_RESULTS_DIRNAME / "task_0"
    task.mkdir(parents=True, exist_ok=True)
    (task / "metrics.json").write_text(json.dumps(payload), encoding="utf-8")
    (task / agg.PER_TASK_CMD_SHA_FILENAME).write_text(sha, encoding="utf-8")
    return task


def _install_graft_pull(
    monkeypatch: pytest.MonkeyPatch, *, source_sha: str, source_payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Model the transport against a grafted source.

    Each pull refreshes the small ``.hpc_cmd_sha`` sidecar to *source_sha* (its
    mtime moved on the remote promote, so rsync/tar always re-fetch it). The
    summary models the torn-overwrite skip: an EXISTING cached copy is NOT
    overwritten by the delta; only a re-fetch into an EVICTED (absent) dir
    delivers the fresh value. Returns the per-call include lists.
    """
    calls: list[dict[str, Any]] = []

    def _fake_pull(*, local_dir: str, include: list[str] | None, **_kw: Any) -> SimpleNamespace:
        from pathlib import Path

        calls.append({"include": list(include) if include else None})
        task = Path(local_dir) / "task_0"
        task.mkdir(parents=True, exist_ok=True)
        if include and agg.PER_TASK_CMD_SHA_FILENAME in include:
            (task / agg.PER_TASK_CMD_SHA_FILENAME).write_text(source_sha, encoding="utf-8")
        summary = task / "metrics.json"
        if not summary.exists():
            summary.write_text(json.dumps(source_payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(agg, "rsync_pull", _fake_pull)
    return calls


def test_fires_stale_cached_piece_is_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIRES: a cached piece whose source cmd_sha moved is evicted + re-pulled.

    The cache carries the Jul-11 blown copy (999) under ``_OLD_SHA``; the source
    was grafted (``_NEW_SHA``, metric 7) with a summary the delta would skip. The
    gate detects the fingerprint move, evicts the dir, and re-pulls it clean, so
    the reduce reads the FRESH 7 — the old idempotent-by-task-id cache would have
    reduced the stale 999.
    """
    out = tmp_path / "agg_out"
    _seed_stale_mirror(out, sha=_OLD_SHA, payload=_STALE)
    calls = _install_graft_pull(monkeypatch, source_sha=_NEW_SHA, source_payload=_FRESH)

    result = agg._per_task_metrics_reduce(
        tmp_path,
        _RUN_ID,
        record=_fake_record(),
        out=out,
        results_subdir="results",
        summary_name="metrics.json",
    )

    # Fresh value reduced — NOT the stale 999 the idempotent cache held.
    assert result == {_RUN_ID: {"metric": 7.0, "n_samples": 1}}
    # A second SUMMARY pull fired: the evict-then-clean-re-pull of the refreshed
    # dir (the trailing trace pull for _trace.jsonl is a separate seam).
    summary_pulls = [c for c in calls if "metrics.json" in (c["include"] or [])]
    assert len(summary_pulls) == 2
    # The fingerprint sidecar was requested on BOTH transports (TRAP 1).
    assert all(agg.PER_TASK_CMD_SHA_FILENAME in (c["include"] or []) for c in summary_pulls)


def test_passes_unchanged_piece_not_repulled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PASSES: an unchanged piece (same cmd_sha) is not evicted — a single pull.

    The steady-state re-aggregate must not re-pull anything: the cached
    fingerprint matches the source, so the gate is a no-op and the reduce reads
    the cached value with exactly ONE pull.
    """
    out = tmp_path / "agg_out"
    _seed_stale_mirror(out, sha=_NEW_SHA, payload=_FRESH)
    calls = _install_graft_pull(monkeypatch, source_sha=_NEW_SHA, source_payload=_FRESH)

    result = agg._per_task_metrics_reduce(
        tmp_path,
        _RUN_ID,
        record=_fake_record(),
        out=out,
        results_subdir="results",
        summary_name="metrics.json",
    )

    assert result == {_RUN_ID: {"metric": 7.0, "n_samples": 1}}
    # No fingerprint moved → no evict → exactly one SUMMARY pull (no re-pull).
    summary_pulls = [c for c in calls if "metrics.json" in (c["include"] or [])]
    assert len(summary_pulls) == 1


def test_first_harvest_no_cache_is_single_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold first harvest (no prior mirror) has nothing to compare — one pull.

    The gate must be inert when the mirror does not exist yet: no cached copy,
    no comparison, no spurious re-pull.
    """
    out = tmp_path / "agg_out"
    calls = _install_graft_pull(monkeypatch, source_sha=_NEW_SHA, source_payload=_FRESH)

    result = agg._per_task_metrics_reduce(
        tmp_path,
        _RUN_ID,
        record=_fake_record(),
        out=out,
        results_subdir="results",
        summary_name="metrics.json",
    )

    assert result == {_RUN_ID: {"metric": 7.0, "n_samples": 1}}
    summary_pulls = [c for c in calls if "metrics.json" in (c["include"] or [])]
    assert len(summary_pulls) == 1


def test_fallback_mtime_size_when_no_fingerprint_sidecar(tmp_path: Path) -> None:
    """A legacy piece with no ``.hpc_cmd_sha`` falls back to an mtime|size token.

    ``_piece_fingerprint`` must still yield a comparable token for a dir a
    dispatcher predating the stamp wrote, so a torn overwrite is detectable even
    without the sidecar (the finding's fallback compare).
    """
    task = tmp_path / "task_0"
    task.mkdir()
    summary = task / "metrics.json"
    summary.write_text(json.dumps(_FRESH), encoding="utf-8")

    fp = agg._piece_fingerprint(task, summary)
    assert fp.startswith("meta:")  # not sha: — no sidecar present
    st = summary.stat()
    assert fp == f"meta:{st.st_mtime_ns}|{st.st_size}"


def test_refreshed_task_invalidates_its_wave_partial(
    journal_home: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refreshed task's wave partial is dropped from combined_waves.

    Run-13's graft re-ran tasks whose wave was already combined over the stale
    pieces. When the staleness gate evicts a refreshed task, the wave it belongs
    to must move ``combined_waves`` → ``failed_waves`` (the forced-recombine
    signal ``combine.py`` reads), mirroring the resubmit path's precedent.
    """
    experiment = tmp_path / "exp"
    experiment.mkdir()
    record = RunRecord(
        run_id=_RUN_ID,
        profile="ml_ridge",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml_ridge",
        job_ids=["12345678"],
        total_tasks=2,
        submitted_at="2026-04-26T17:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
        combined_waves=[0],
    )
    upsert_run(experiment, record)
    write_run_sidecar(
        experiment,
        run_id=_RUN_ID,
        cmd_sha=_NEW_SHA,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/task_{task_id}",
        task_count=2,
        tasks_py_sha="1" * 64,
        wave_map={"0": [0]},
        remote_path="/u/scratch/exp",
    )

    out = experiment / "agg_out"
    _seed_stale_mirror(out, sha=_OLD_SHA, payload=_STALE)
    _install_graft_pull(monkeypatch, source_sha=_NEW_SHA, source_payload=_FRESH)

    loaded = load_run(experiment, _RUN_ID)
    assert loaded is not None
    agg._per_task_metrics_reduce(
        experiment,
        _RUN_ID,
        record=loaded,
        out=out,
        results_subdir="results",
        summary_name="metrics.json",
    )

    after = load_run(experiment, _RUN_ID)
    assert after is not None
    assert 0 not in after.combined_waves  # dropped for a forced recombine
    assert 0 in after.failed_waves  # the durable "needs recombine" signal
