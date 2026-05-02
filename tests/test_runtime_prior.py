"""Tests for hpc_mapreduce.job.runtime_prior — quantile rollups + atomic appends."""

from __future__ import annotations

import json

from hpc_mapreduce.job import runtime_prior as rp


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
        out = rp.roll_up_quantiles(
            tmp_path, profile="ml_ridge", cluster="discovery"
        )
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
        out = rp.roll_up_quantiles(
            tmp_path, profile="ml_ridge", cluster="discovery"
        )
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
        out = rp.roll_up_quantiles(
            tmp_path, profile="ml_ridge", cluster="discovery"
        )
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
        out = rp.roll_up_quantiles(
            tmp_path, profile="ml_ridge", cluster="discovery"
        )
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
