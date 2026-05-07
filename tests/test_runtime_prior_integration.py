"""Integration tests for ``claude_hpc.state.runtime_prior``.

The runtime-prior file at
``<exp>/.hpc/runtimes/<profile>.<cluster>.json`` is an append-only
sample log feeding the planner's quantile-based runtime predictions.
Two contracts that must not drift:

1. **Atomicity under concurrent writers.** The monitor session
   ingesting completed tasks runs alongside in-flight submits.
   ``atomic_locked_update`` is supposed to serialise the
   read-filter-append-write under flock; any drift here loses
   samples silently.
2. **Permissive read.** The file lives on shared NFS that can drop
   bytes mid-write or be wiped between writes; the planner reads
   it on every submission. A corrupt file or a future-schema-version
   file must surface as "no samples" rather than crash the planner.

Test pattern: real ``tmp_path`` filesystem, ``multiprocessing`` for
the concurrent-writer test, no mocks.
"""

from __future__ import annotations

import json
import multiprocessing
from typing import TYPE_CHECKING

from claude_hpc.state import runtime_prior as rp

if TYPE_CHECKING:
    from pathlib import Path


_PROFILE = "ml_ridge"
_CLUSTER = "discovery"


def _append(tmp_path: Path, *, run_id: str, task_id: int, **overrides) -> None:
    rp.append_sample(
        tmp_path,
        profile=_PROFILE,
        cluster=_CLUSTER,
        run_id=run_id,
        task_id=task_id,
        gpu_type=overrides.pop("gpu_type", "a100"),
        node=overrides.pop("node", "d11-07"),
        elapsed_sec=overrides.pop("elapsed_sec", 4150),
        **overrides,
    )


# ─── Layer 1: append/read roundtrip ────────────────────────────────────


def test_append_read_roundtrips_single_sample(tmp_path: Path) -> None:
    _append(tmp_path, run_id="r1", task_id=0, elapsed_sec=4150)
    samples = rp.read_samples(tmp_path, profile=_PROFILE, cluster=_CLUSTER)
    assert len(samples) == 1
    s = samples[0]
    assert s["run_id"] == "r1"
    assert s["task_id"] == 0
    assert s["elapsed_sec"] == 4150
    assert s["gpu_type"] == "a100"


def test_append_idempotent_on_run_id_task_id(tmp_path: Path) -> None:
    """Duplicate ``(run_id, task_id)`` replaces the existing record
    rather than appending a second copy. Documented contract — protects
    against monitor-replay scenarios where the same task gets ingested
    twice."""
    _append(tmp_path, run_id="r1", task_id=0, elapsed_sec=4150)
    _append(tmp_path, run_id="r1", task_id=0, elapsed_sec=9999)  # same key, new value
    samples = rp.read_samples(tmp_path, profile=_PROFILE, cluster=_CLUSTER)
    assert len(samples) == 1
    assert samples[0]["elapsed_sec"] == 9999


def test_distinct_keys_accumulate(tmp_path: Path) -> None:
    for tid in range(5):
        _append(tmp_path, run_id="r1", task_id=tid, elapsed_sec=4000 + tid)
    samples = rp.read_samples(tmp_path, profile=_PROFILE, cluster=_CLUSTER)
    assert len(samples) == 5
    assert {s["task_id"] for s in samples} == {0, 1, 2, 3, 4}


def test_axis_bindings_round_trip(tmp_path: Path) -> None:
    """v2 axis_bindings dict (axis_name → value) must survive the
    JSON round-trip — the warm-axis-picker reads these to score
    homogeneity."""
    _append(
        tmp_path,
        run_id="r1",
        task_id=0,
        axis_bindings={"horizon": 5, "model": "ridge"},
    )
    samples = rp.read_samples(tmp_path, profile=_PROFILE, cluster=_CLUSTER)
    assert samples[0]["axis_bindings"] == {"horizon": 5, "model": "ridge"}


# ─── Layer 2: concurrency safety ───────────────────────────────────────


def _worker_append_batch(tmp_path: str, run_id: str, n: int) -> None:
    """Append n samples from a child process. Imported via spawn so it
    must be top-level."""
    from pathlib import Path as _Path

    from claude_hpc.state import runtime_prior as _rp

    for tid in range(n):
        _rp.append_sample(
            _Path(tmp_path),
            profile="ml_ridge",
            cluster="discovery",
            run_id=run_id,
            task_id=tid,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=1000 + tid,
        )


def test_concurrent_writers_lose_no_samples(tmp_path: Path) -> None:
    """Two child processes each append 10 distinct samples. Under
    ``atomic_locked_update`` no append should be lost — the post-state
    has all 20 samples. Without the flock the lost-update race shows
    up here (final count drops to ~10 on race)."""
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_worker_append_batch, args=(str(tmp_path), "rA", 10)),
        ctx.Process(target=_worker_append_batch, args=(str(tmp_path), "rB", 10)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=20)
        assert p.exitcode == 0, (p, p.exitcode)

    samples = rp.read_samples(tmp_path, profile=_PROFILE, cluster=_CLUSTER)
    by_run = {(s["run_id"], s["task_id"]) for s in samples}
    assert len(by_run) == 20, by_run
    assert {("rA", i) for i in range(10)} <= by_run
    assert {("rB", i) for i in range(10)} <= by_run


# ─── Layer 3: corruption recovery ─────────────────────────────────────


def test_corrupt_json_treated_as_empty(tmp_path: Path) -> None:
    """A non-JSON file (truncated write, NFS hiccup) must surface as
    "no samples" not crash the planner. ``read_samples`` is called on
    every submission; a swallowed crash here would block submits
    cluster-wide."""
    target = rp.runtime_path(tmp_path, _PROFILE, _CLUSTER)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{this is not json")
    samples = rp.read_samples(tmp_path, profile=_PROFILE, cluster=_CLUSTER)
    assert samples == []


def test_future_schema_version_treated_as_empty(tmp_path: Path) -> None:
    """If a writer with a wider schema poisons the file, the reader
    must treat it as empty rather than mis-shape the rollup. The
    cross-domain compatibility check in ``_read_doc`` enforces this."""
    target = rp.runtime_path(tmp_path, _PROFILE, _CLUSTER)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "profile": _PROFILE,
                "cluster": _CLUSTER,
                "schema_version": 99999,  # impossibly future
                "samples": [{"run_id": "r1", "task_id": 0, "elapsed_sec": 4150}],
            }
        )
    )
    assert rp.read_samples(tmp_path, profile=_PROFILE, cluster=_CLUSTER) == []


def test_missing_file_treated_as_empty(tmp_path: Path) -> None:
    """No file on disk → empty list, no exception. Planner relies on
    this to skip the prior path on cold campaigns."""
    assert rp.read_samples(tmp_path, profile=_PROFILE, cluster=_CLUSTER) == []


# ─── Layer 4: bounded growth ───────────────────────────────────────────


def test_max_samples_bound_enforced(tmp_path: Path, monkeypatch) -> None:
    """Append more than ``MAX_SAMPLES`` and verify oldest-first
    eviction keeps the file bounded."""
    monkeypatch.setattr(rp, "MAX_SAMPLES", 5)
    for i in range(8):
        _append(tmp_path, run_id="r1", task_id=i, elapsed_sec=1000 + i)
    samples = rp.read_samples(tmp_path, profile=_PROFILE, cluster=_CLUSTER)
    assert len(samples) == 5
    # Oldest-first eviction → tasks 0/1/2 are gone, 3..7 remain.
    assert {s["task_id"] for s in samples} == {3, 4, 5, 6, 7}


# ─── Layer 5: roll_up_quantiles end-to-end ─────────────────────────────


def test_roll_up_quantiles_returns_canary_signal_when_empty(tmp_path: Path) -> None:
    """No samples → ``needs_canary=True``. The planner reads this to
    short-circuit into a 1-task canary submission before scoring."""
    out = rp.roll_up_quantiles(tmp_path, profile=_PROFILE, cluster=_CLUSTER)
    assert out["needs_canary"] is True
    assert out["quantiles"] == {}
    assert out["total_samples"] == 0


def test_roll_up_quantiles_groups_by_gpu_type(tmp_path: Path) -> None:
    """Distinct gpu_types produce distinct quantile buckets; the
    planner uses these to score per-GPU candidates."""
    for tid, elapsed in enumerate([3000, 3500, 4000, 4500, 5000]):
        _append(tmp_path, run_id="r_a", task_id=tid, gpu_type="a100", elapsed_sec=elapsed)
    for tid, elapsed in enumerate([6000, 7000]):
        _append(tmp_path, run_id="r_v", task_id=tid, gpu_type="v100", elapsed_sec=elapsed)

    out = rp.roll_up_quantiles(tmp_path, profile=_PROFILE, cluster=_CLUSTER)
    assert out["needs_canary"] is False
    assert set(out["quantiles"]) == {"a100", "v100"}
    a100 = out["quantiles"]["a100"]
    assert a100["n_samples"] == 5
    assert a100["min_sec"] == 3000
    assert a100["max_sec"] == 5000
    assert a100["p50"] == 4000


def test_roll_up_quantiles_filters_by_cmd_sha(tmp_path: Path) -> None:
    """When ``cmd_sha`` is supplied, only samples tagged with that sha
    contribute. Lets the planner score only against the current
    campaign's history rather than mixing unrelated runs."""
    _append(tmp_path, run_id="r1", task_id=0, cmd_sha="a" * 64, elapsed_sec=4000)
    _append(tmp_path, run_id="r2", task_id=0, cmd_sha="b" * 64, elapsed_sec=8000)

    out = rp.roll_up_quantiles(
        tmp_path, profile=_PROFILE, cluster=_CLUSTER, cmd_sha="a" * 64
    )
    assert out["filtered_by_cmd_sha"] == "a" * 64
    a100 = out["quantiles"]["a100"]
    assert a100["n_samples"] == 1
    assert a100["min_sec"] == 4000
    assert a100["max_sec"] == 4000  # only the matching sample


def test_failed_samples_excluded_from_quantiles(tmp_path: Path) -> None:
    """Quantiles must reflect successful runtimes only — a failed task
    that crashed at 30s would otherwise pull p50 down and the planner
    would under-allocate walltime."""
    _append(tmp_path, run_id="r1", task_id=0, exit_code=0, elapsed_sec=4000)
    _append(tmp_path, run_id="r1", task_id=1, exit_code=137, elapsed_sec=30)  # OOM

    out = rp.roll_up_quantiles(tmp_path, profile=_PROFILE, cluster=_CLUSTER)
    a100 = out["quantiles"]["a100"]
    assert a100["n_samples"] == 1
    assert a100["min_sec"] == 4000
