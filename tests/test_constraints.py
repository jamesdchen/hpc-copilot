"""Tests for ClusterConstraints, parse_constraints, and load_constraints."""

from __future__ import annotations

import pytest

from hpc_mapreduce.job.constraints import ClusterConstraints, parse_constraints
from hpc_mapreduce.infra.clusters import load_constraints


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


class TestLoadConstraints:
    """Tests for load_constraints merging cluster and profile configs."""

    def test_merge_cluster_and_profile(self):
        cluster = {"constraints": {"max_array_size": 100}}
        profile = {"constraints": {"max_walltime": "1:00:00"}}
        c = load_constraints(cluster, profile)
        assert c.max_array_size == 100
        assert c.max_walltime == "1:00:00"

    def test_cluster_only_no_profile(self):
        cluster = {"constraints": {"max_array_size": 100, "max_concurrent_jobs": 5}}
        c = load_constraints(cluster)
        assert c.max_array_size == 100
        assert c.max_concurrent_jobs == 5
        # Remaining fields use ClusterConstraints defaults
        assert c.max_walltime == "24:00:00"
        assert c.est_spin_up == "5m"

    def test_empty_cluster_constraints(self):
        cluster = {"constraints": {}}
        c = load_constraints(cluster)
        # All fields should be ClusterConstraints defaults
        assert c.max_array_size == 1000
        assert c.max_walltime == "24:00:00"
        assert c.max_concurrent_jobs == 10
        assert c.est_spin_up == "5m"

    def test_profile_overrides_cluster(self):
        cluster = {"constraints": {"max_array_size": 100, "max_walltime": "24:00:00"}}
        profile = {"constraints": {"max_walltime": "1:00:00"}}
        c = load_constraints(cluster, profile)
        assert c.max_array_size == 100
        assert c.max_walltime == "1:00:00"
