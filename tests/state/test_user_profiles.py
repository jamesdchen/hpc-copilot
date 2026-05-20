"""Tests for ``hpc_agent.state.user_profiles``."""

from __future__ import annotations

from hpc_agent.state import user_profiles as up


class TestUpdateAndRead:
    def test_first_observation_creates_profile(self, tmp_path):
        up.update_profile(
            tmp_path,
            cluster="discovery",
            observed_jobs=[
                {
                    "user": "alice",
                    "submitted_at_iso": "2026-04-28T10:00:00",
                    "walltime_ask_sec": 7200,
                    "elapsed_sec": 6000,
                    "exit_code": 0,
                    "gpu_type": "a100",
                }
            ],
        )
        prof = up.read_profile(tmp_path, cluster="discovery", user="alice")
        assert prof is not None
        assert prof.user == "alice"
        assert prof.n_observations == 1
        # First observation of walltime_ask snaps the estimator from 0.
        assert prof.median_walltime_ask_sec == 7200
        # median_actual_over_ask seeds at 1.0 so first observation
        # blends toward 6000/7200 by FACTOR_SMOOTHING (5%) — direction
        # matters more than magnitude on a single sample.
        assert prof.median_actual_over_ask < 1.0
        assert prof.failure_rate == 0.0
        assert "a100" in prof.typical_gpu_types

    def test_unknown_user_returns_none(self, tmp_path):
        assert up.read_profile(tmp_path, cluster="discovery", user="nope") is None

    def test_second_observation_updates_failure_rate(self, tmp_path):
        for code in (0, 1):
            up.update_profile(
                tmp_path,
                cluster="discovery",
                observed_jobs=[{"user": "alice", "exit_code": code}],
            )
        prof = up.read_profile(tmp_path, cluster="discovery", user="alice")
        assert prof is not None
        assert prof.n_observations == 2
        assert prof.failure_rate == 0.5


class TestMultipleUsers:
    def test_two_users_diverge(self, tmp_path):
        # Alice: many submissions, mostly Tuesday morning, light overshoot.
        for hour in (9, 10, 11):
            up.update_profile(
                tmp_path,
                cluster="discovery",
                observed_jobs=[
                    {
                        "user": "alice",
                        "submitted_at_iso": f"2026-04-28T{hour:02d}:00:00",
                        "walltime_ask_sec": 3600,
                        "elapsed_sec": 3600,
                        "exit_code": 0,
                        "gpu_type": "a100",
                    }
                ],
            )
        # Bob: few submissions, weekend.
        up.update_profile(
            tmp_path,
            cluster="discovery",
            observed_jobs=[
                {
                    "user": "bob",
                    "submitted_at_iso": "2026-05-02T03:00:00",  # Sat 03:00
                    "walltime_ask_sec": 7200,
                    "elapsed_sec": 14400,  # massive overshoot
                    "exit_code": 0,
                    "gpu_type": "v100",
                }
            ],
        )
        profiles = up.all_profiles(tmp_path, cluster="discovery")
        assert set(profiles) == {"alice", "bob"}
        a = profiles["alice"]
        b = profiles["bob"]
        assert a.n_observations == 3
        assert b.n_observations == 1
        # Alice is on a 1-hour walltime-ask schedule; Bob is on 2h.
        # Different medians, with running blend.
        assert a.median_walltime_ask_sec < b.median_walltime_ask_sec
        # Bob: elapsed 14400 / ask 7200 = 2.0; running blend pushes
        # the estimator above 1.0 (the seed) but well below the raw
        # observation given the conservative smoothing factor.
        assert b.median_actual_over_ask > 1.0
        # Alice's hour-of-week distribution is concentrated on Tue.
        assert a.submit_hour_of_week_distribution
        tue_keys = [k for k in a.submit_hour_of_week_distribution if 1 * 24 <= k < 2 * 24]
        assert len(tue_keys) == 3


class TestConvergence:
    def test_running_median_converges_on_steady_walltime(self, tmp_path):
        # Feed many observations of the same walltime ask; the running
        # estimator should land within 5% of the true value after ~30
        # iterations because of the snap-on-zero seed.
        for _ in range(40):
            up.update_profile(
                tmp_path,
                cluster="discovery",
                observed_jobs=[
                    {
                        "user": "alice",
                        "walltime_ask_sec": 3600,
                        "elapsed_sec": 3000,
                    }
                ],
            )
        prof = up.read_profile(tmp_path, cluster="discovery", user="alice")
        assert prof is not None
        assert 3500 <= prof.median_walltime_ask_sec <= 3700
        # actual_over_ask: 3000/3600 ≈ 0.833.
        assert abs(prof.median_actual_over_ask - 0.833) < 0.05


class TestPersistence:
    def test_atomic_write_idempotent(self, tmp_path):
        up.update_profile(
            tmp_path,
            cluster="discovery",
            observed_jobs=[{"user": "alice", "exit_code": 0}],
        )
        path = up.user_profiles_path(tmp_path, "discovery")
        first = path.read_text()
        # No-op call (no users) does not blow up the file.
        up.update_profile(tmp_path, cluster="discovery", observed_jobs=[])
        assert path.read_text() == first

    def test_no_user_skipped_silently(self, tmp_path):
        up.update_profile(
            tmp_path,
            cluster="discovery",
            observed_jobs=[{"exit_code": 0}, {"user": "", "exit_code": 0}],
        )
        # File may not exist if no observation produced a write — that
        # is a valid no-op outcome.
        assert up.all_profiles(tmp_path, cluster="discovery") == {}
