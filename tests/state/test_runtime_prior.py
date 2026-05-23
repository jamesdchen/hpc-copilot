"""Tests for hpc_agent.state.runtime_prior — quantile rollups + atomic appends."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent.state import runtime_prior as rp

if TYPE_CHECKING:
    from pathlib import Path


class TestAppendSample:
    def test_creates_file(self, tmp_path):
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4150,
        )
        path = rp.runtime_path(tmp_path, "ml_ridge", "discovery")
        assert path.exists()
        samples = rp.read_samples(tmp_path, profile="ml_ridge", cluster="discovery")
        assert len(samples) == 1

    def test_idempotent_on_run_and_task(self, tmp_path):
        for elapsed in (4000, 4200):
            rp.append_sample(
                tmp_path,
                profile="ml_ridge",
                cluster="discovery",
                run_id="r1",
                task_id=0,
                gpu_type="a100",
                node="d11-07",
                elapsed_sec=elapsed,
            )
        samples = rp.read_samples(tmp_path, profile="ml_ridge", cluster="discovery")
        assert len(samples) == 1
        # Latest write wins.
        assert samples[0]["elapsed_sec"] == 4200


class TestRollUp:
    def test_empty_yields_needs_canary(self, tmp_path):
        out = rp.roll_up_quantiles(tmp_path, profile="x", cluster="y")
        assert out["needs_canary"] is True
        assert out["quantiles"] == {}
        assert out["total_samples"] == 0

    def test_single_sample_degenerate_quantiles(self, tmp_path):
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4150,
        )
        out = rp.roll_up_quantiles(tmp_path, profile="ml_ridge", cluster="discovery")
        assert out["needs_canary"] is False
        a100 = out["quantiles"]["a100"]
        assert a100["n_samples"] == 1
        assert a100["p50"] == 4150
        assert a100["p95"] == 4150
        assert a100["p99"] == 4150

    def test_multi_sample_quantile_ordering(self, tmp_path):
        for i, elapsed in enumerate([1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000]):
            rp.append_sample(
                tmp_path,
                profile="ml_ridge",
                cluster="discovery",
                run_id="r1",
                task_id=i,
                gpu_type="a100",
                node="d11-07",
                elapsed_sec=elapsed,
            )
        out = rp.roll_up_quantiles(tmp_path, profile="ml_ridge", cluster="discovery")
        a100 = out["quantiles"]["a100"]
        assert a100["n_samples"] == 10
        assert a100["min_sec"] == 1000
        assert a100["max_sec"] == 10000
        assert a100["p50"] <= a100["p95"] <= a100["p99"]

    def test_groups_by_gpu_type(self, tmp_path):
        for tid, gpu in enumerate(["a100", "a100", "v100"]):
            rp.append_sample(
                tmp_path,
                profile="ml_ridge",
                cluster="discovery",
                run_id="r1",
                task_id=tid,
                gpu_type=gpu,
                node=f"node-{tid}",
                elapsed_sec=1000 + tid,
            )
        out = rp.roll_up_quantiles(tmp_path, profile="ml_ridge", cluster="discovery")
        assert set(out["quantiles"].keys()) == {"a100", "v100"}
        assert out["quantiles"]["a100"]["n_samples"] == 2
        assert out["quantiles"]["v100"]["n_samples"] == 1

    def test_filter_by_cmd_sha(self, tmp_path):
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=1000,
            cmd_sha="sha-old",
        )
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r2",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=2000,
            cmd_sha="sha-new",
        )
        out = rp.roll_up_quantiles(
            tmp_path, profile="ml_ridge", cluster="discovery", cmd_sha="sha-new"
        )
        assert out["quantiles"]["a100"]["n_samples"] == 1
        assert out["quantiles"]["a100"]["p50"] == 2000

    def test_failed_samples_excluded(self, tmp_path):
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4000,
            exit_code=1,
        )
        out = rp.roll_up_quantiles(tmp_path, profile="ml_ridge", cluster="discovery")
        assert out["needs_canary"] is True


class TestBoundedGrowth:
    def test_max_samples_caps_oldest_first(self, tmp_path, monkeypatch):
        # Force a small cap so the test is fast and deterministic.
        monkeypatch.setattr(rp, "MAX_SAMPLES", 5)
        for tid in range(8):
            rp.append_sample(
                tmp_path,
                profile="p",
                cluster="c",
                run_id="r1",
                task_id=tid,
                gpu_type="a100",
                node="n1",
                elapsed_sec=100 + tid,
            )
        samples = rp.read_samples(tmp_path, profile="p", cluster="c")
        assert len(samples) == 5
        # FIFO: oldest (tid=0..2) dropped, newest (tid=3..7) survive.
        assert {s["task_id"] for s in samples} == {3, 4, 5, 6, 7}


class TestIdempotencyUnderCap:
    def test_replace_after_eviction(self, tmp_path, monkeypatch):
        # The dedup-by-(run_id, task_id) and the MAX_SAMPLES cap interact:
        # if many newer samples push an old (r1, 0) record off the back,
        # a re-write of (r1, 0) should append (it's no longer present),
        # not silently fail. Confirms the two contracts compose correctly.
        monkeypatch.setattr(rp, "MAX_SAMPLES", 3)
        # Seed: (r1, 0).
        rp.append_sample(
            tmp_path,
            profile="p",
            cluster="c",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="n1",
            elapsed_sec=100,
        )
        # Push it off the back with three newer samples.
        for tid in (1, 2, 3):
            rp.append_sample(
                tmp_path,
                profile="p",
                cluster="c",
                run_id="r1",
                task_id=tid,
                gpu_type="a100",
                node="n1",
                elapsed_sec=200 + tid,
            )
        # Now (r1, 0) is gone. Re-writing it should append a fresh copy.
        rp.append_sample(
            tmp_path,
            profile="p",
            cluster="c",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="n1",
            elapsed_sec=999,
        )
        samples = rp.read_samples(tmp_path, profile="p", cluster="c")
        # Cap is 3; the just-appended (r1, 0) plus the two most recent.
        assert len(samples) == 3
        assert any(s["task_id"] == 0 and s["elapsed_sec"] == 999 for s in samples)


class TestPathNormalization:
    def test_relative_and_absolute_resolve_to_same_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Write via relative path, read via absolute path — must be the
        # same file.
        rp.append_sample(
            ".",
            profile="p",
            cluster="c",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="n1",
            elapsed_sec=100,
        )
        from_abs = rp.read_samples(tmp_path, profile="p", cluster="c")
        assert len(from_abs) == 1


class TestDocFileShape:
    def test_round_trip_via_disk(self, tmp_path):
        rp.append_sample(
            tmp_path,
            profile="p",
            cluster="c",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="n1",
            elapsed_sec=100,
        )
        path = rp.runtime_path(tmp_path, "p", "c")
        doc = json.loads(path.read_text())
        assert doc["schema_version"] == rp.SCHEMA_VERSION
        assert doc["profile"] == "p"
        assert doc["cluster"] == "c"
        assert isinstance(doc["samples"], list)


# ---------------------------------------------------------------------------
# ingest_runtime_samples_from_combiner_dir
# ---------------------------------------------------------------------------


class TestIngestFromCombinerDir:
    def test_walks_wave_runtime_files_and_appends(self, tmp_path: Path) -> None:
        from hpc_agent.state.runtime_prior import (
            ingest_runtime_samples_from_combiner_dir,
            read_samples,
        )

        combiner_dir = tmp_path / "_combiner"
        combiner_dir.mkdir()
        (combiner_dir / "wave_0.runtime.json").write_text(
            json.dumps(
                {
                    "wave": 0,
                    "run_id": "r1",
                    "samples": [
                        {
                            "task_id": 0,
                            "run_id": "r1",
                            "started_at": "2026-05-01T00:00:00+00:00",
                            "ended_at": "2026-05-01T01:00:00+00:00",
                            "elapsed_sec": 3600,
                            "exit_code": 0,
                            "node": "d11-07",
                            "gpu_type": "a100",
                            "axis_bindings": {"model": "lgbm", "window": 5},
                        },
                        {
                            "task_id": 1,
                            "run_id": "r1",
                            "started_at": "2026-05-01T00:00:00+00:00",
                            "ended_at": "2026-05-01T02:00:00+00:00",
                            "elapsed_sec": 7200,
                            "exit_code": 0,
                            "node": "d11-08",
                            "gpu_type": "a100",
                            "axis_bindings": {"model": "xgb", "window": 5},
                        },
                    ],
                }
            )
        )

        n = ingest_runtime_samples_from_combiner_dir(
            combiner_dir,
            experiment_dir=tmp_path,
            profile="p",
            cluster="c",
            cmd_sha="abc123",
        )
        assert n == 2

        samples = read_samples(tmp_path, profile="p", cluster="c")
        assert len(samples) == 2
        # The warm picker keys on axis_bindings — check it survived.
        bindings = sorted((s["axis_bindings"]["model"], s["elapsed_sec"]) for s in samples)
        assert bindings == [("lgbm", 3600), ("xgb", 7200)]

    def test_idempotent_on_rerun(self, tmp_path: Path) -> None:
        """append_sample dedups (run_id, task_id), so re-ingest is safe."""
        from hpc_agent.state.runtime_prior import (
            ingest_runtime_samples_from_combiner_dir,
            read_samples,
        )

        combiner_dir = tmp_path / "_combiner"
        combiner_dir.mkdir()
        (combiner_dir / "wave_0.runtime.json").write_text(
            json.dumps(
                {
                    "wave": 0,
                    "run_id": "r1",
                    "samples": [
                        {
                            "task_id": 0,
                            "run_id": "r1",
                            "elapsed_sec": 100,
                            "exit_code": 0,
                            "node": "d11-07",
                            "gpu_type": "a100",
                            "axis_bindings": {"model": "lgbm"},
                        },
                    ],
                }
            )
        )

        ingest_runtime_samples_from_combiner_dir(
            combiner_dir, experiment_dir=tmp_path, profile="p", cluster="c"
        )
        ingest_runtime_samples_from_combiner_dir(
            combiner_dir, experiment_dir=tmp_path, profile="p", cluster="c"
        )
        samples = read_samples(tmp_path, profile="p", cluster="c")
        assert len(samples) == 1  # not 2

    def test_missing_dir_returns_zero(self, tmp_path: Path) -> None:
        from hpc_agent.state.runtime_prior import (
            ingest_runtime_samples_from_combiner_dir,
        )

        n = ingest_runtime_samples_from_combiner_dir(
            tmp_path / "does_not_exist",
            experiment_dir=tmp_path,
            profile="p",
            cluster="c",
        )
        assert n == 0

    def test_malformed_runtime_file_skipped(self, tmp_path: Path) -> None:
        """A bad JSON file shouldn't tank the whole ingest."""
        from hpc_agent.state.runtime_prior import (
            ingest_runtime_samples_from_combiner_dir,
            read_samples,
        )

        combiner_dir = tmp_path / "_combiner"
        combiner_dir.mkdir()
        (combiner_dir / "wave_0.runtime.json").write_text("not valid json")
        (combiner_dir / "wave_1.runtime.json").write_text(
            json.dumps(
                {
                    "wave": 1,
                    "run_id": "r1",
                    "samples": [
                        {
                            "task_id": 5,
                            "run_id": "r1",
                            "elapsed_sec": 50,
                            "exit_code": 0,
                            "node": "d11-07",
                            "gpu_type": "a100",
                            "axis_bindings": {"model": "lgbm"},
                        },
                    ],
                }
            )
        )

        n = ingest_runtime_samples_from_combiner_dir(
            combiner_dir, experiment_dir=tmp_path, profile="p", cluster="c"
        )
        assert n == 1
        assert len(read_samples(tmp_path, profile="p", cluster="c")) == 1

    def test_warm_picker_picks_up_after_ingest(self, tmp_path: Path) -> None:
        """End-to-end: ingest → warm picker can rank axes by CV."""
        from hpc_agent.state.axes import (
            pick_array_axis_warm,
            write_axes,
        )
        from hpc_agent.state.runtime_prior import (
            ingest_runtime_samples_from_combiner_dir,
        )

        write_axes(
            tmp_path,
            axes=[
                {"name": "model", "size": 3},
                {"name": "window", "size": 5},
            ],
        )
        combiner_dir = tmp_path / "_combiner"
        combiner_dir.mkdir()
        # window varies cheaply (constant per model); model varies wildly.
        samples = []
        for tid, (model, base) in enumerate(
            [("A", 100.0)] * 5 + [("B", 200.0)] * 5 + [("C", 300.0)] * 5
        ):
            samples.append(
                {
                    "task_id": tid,
                    "run_id": "r1",
                    "elapsed_sec": int(base),
                    "exit_code": 0,
                    "node": "d11-07",
                    "gpu_type": "a100",
                    "axis_bindings": {"model": model, "window": tid % 5},
                }
            )
        (combiner_dir / "wave_0.runtime.json").write_text(
            json.dumps({"wave": 0, "run_id": "r1", "samples": samples})
        )

        n = ingest_runtime_samples_from_combiner_dir(
            combiner_dir, experiment_dir=tmp_path, profile="p", cluster="c"
        )
        assert n == 15

        name, reason = pick_array_axis_warm(tmp_path, min_samples=5)
        # Window has 0-CV within each model (constant runtime); model has high CV.
        assert name == "window", f"expected window (low-CV), got {name!r} ({reason})"
