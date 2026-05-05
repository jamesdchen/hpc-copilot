"""Tests for queue_wait_sec derivation in runtime_prior.append_sample."""

from __future__ import annotations

from claude_hpc.forecast import runtime_prior as rp


def _read_one(tmp_path):
    samples = rp.read_samples(
        tmp_path, profile="ml_ridge", cluster="discovery", only_successful=False
    )
    assert len(samples) == 1
    return samples[0]


class TestQueueWaitExplicit:
    def test_explicit_value_recorded(self, tmp_path):
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4150,
            queue_wait_sec=900,
        )
        assert _read_one(tmp_path)["queue_wait_sec"] == 900

    def test_explicit_zero_is_kept(self, tmp_path):
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=10,
            queue_wait_sec=0,
        )
        assert _read_one(tmp_path)["queue_wait_sec"] == 0

    def test_explicit_negative_rejected(self, tmp_path):
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=10,
            queue_wait_sec=-5,
        )
        assert _read_one(tmp_path)["queue_wait_sec"] is None


class TestQueueWaitDerived:
    def test_derived_from_iso_delta(self, tmp_path):
        # 2026-04-28T01:00:00Z submit; 2026-04-28T01:15:00Z start → 900s.
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4150,
            submitted_at_iso="2026-04-28T01:00:00+00:00",
            started_at="2026-04-28T01:15:00+00:00",
        )
        assert _read_one(tmp_path)["queue_wait_sec"] == 900

    def test_derived_accepts_z_shorthand(self, tmp_path):
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4150,
            submitted_at_iso="2026-04-28T01:00:00Z",
            started_at="2026-04-28T01:00:30Z",
        )
        assert _read_one(tmp_path)["queue_wait_sec"] == 30

    def test_negative_delta_records_none(self, tmp_path):
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4150,
            submitted_at_iso="2026-04-28T01:15:00+00:00",
            started_at="2026-04-28T01:00:00+00:00",
        )
        assert _read_one(tmp_path)["queue_wait_sec"] is None

    def test_missing_started_at_records_none(self, tmp_path):
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4150,
            submitted_at_iso="2026-04-28T01:00:00+00:00",
            started_at=None,
        )
        assert _read_one(tmp_path)["queue_wait_sec"] is None

    def test_missing_submitted_at_records_none(self, tmp_path):
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4150,
            submitted_at_iso=None,
            started_at="2026-04-28T01:15:00+00:00",
        )
        assert _read_one(tmp_path)["queue_wait_sec"] is None

    def test_unparseable_iso_records_none(self, tmp_path):
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4150,
            submitted_at_iso="not-a-date",
            started_at="2026-04-28T01:15:00+00:00",
        )
        assert _read_one(tmp_path)["queue_wait_sec"] is None

    def test_explicit_overrides_iso_derivation(self, tmp_path):
        # Explicit wins even when ISO would derive a different value.
        rp.append_sample(
            tmp_path,
            profile="ml_ridge",
            cluster="discovery",
            run_id="r1",
            task_id=0,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=4150,
            submitted_at_iso="2026-04-28T01:00:00+00:00",
            started_at="2026-04-28T01:15:00+00:00",
            queue_wait_sec=42,
        )
        assert _read_one(tmp_path)["queue_wait_sec"] == 42
