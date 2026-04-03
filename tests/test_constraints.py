"""Tests for ClusterConstraints and parse_constraints."""

from __future__ import annotations

import pytest

from hpc_mapreduce.job.constraints import ClusterConstraints, parse_constraints


class TestClusterConstraints:
    def test_default_values(self):
        c = ClusterConstraints()
        assert c.max_array_size == 1000
        assert c.max_walltime == "24:00:00"
        assert c.max_concurrent_jobs == 10
        assert c.est_spin_up == "5m"

    def test_walltime_seconds(self):
        c = ClusterConstraints(max_walltime="2:00:00")
        assert c.walltime_seconds() == 7200

    def test_walltime_seconds_default(self):
        c = ClusterConstraints()
        assert c.walltime_seconds() == 86400

    def test_spin_up_seconds_minutes(self):
        c = ClusterConstraints(est_spin_up="5m")
        assert c.spin_up_seconds() == 300

    def test_spin_up_seconds_seconds(self):
        c = ClusterConstraints(est_spin_up="30s")
        assert c.spin_up_seconds() == 30

    def test_spin_up_seconds_hours(self):
        c = ClusterConstraints(est_spin_up="1h")
        assert c.spin_up_seconds() == 3600


class TestParseConstraints:
    def test_all_fields(self):
        raw = {
            "max_array_size": 500,
            "max_walltime": "8:00:00",
            "max_concurrent_jobs": 20,
            "est_spin_up": "10m",
        }
        c = parse_constraints(raw)
        assert c.max_array_size == 500
        assert c.max_walltime == "8:00:00"
        assert c.max_concurrent_jobs == 20
        assert c.est_spin_up == "10m"

    def test_partial_fields(self):
        raw = {"max_array_size": 200}
        c = parse_constraints(raw)
        assert c.max_array_size == 200
        # Rest should be defaults
        assert c.max_walltime == "24:00:00"
        assert c.max_concurrent_jobs == 10
        assert c.est_spin_up == "5m"

    def test_unknown_keys_ignored(self):
        raw = {
            "max_array_size": 100,
            "unknown_key": "should_be_ignored",
            "another_unknown": 42,
        }
        c = parse_constraints(raw)
        assert c.max_array_size == 100
        # Should not raise, unknown keys are silently ignored
