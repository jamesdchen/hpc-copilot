"""Tests for hpc_mapreduce.job.blacklist — TTL, atomic writes, idempotency."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from hpc_mapreduce.job import blacklist as bl


def _read_file(experiment_dir, cluster):
    path = bl.blacklist_path(experiment_dir, cluster)
    return json.loads(path.read_text())


class TestRecordSegv:
    def test_creates_file_with_one_entry(self, tmp_path):
        bl.record_segv(
            tmp_path,
            cluster="discovery",
            node="d11-03",
            run_id="r1",
            job_id="999",
            task_id=7,
            exit_code=139,
            signal=-11,
        )
        doc = _read_file(tmp_path, "discovery")
        assert doc["schema_version"] == bl.SCHEMA_VERSION
        assert len(doc["entries"]) == 1
        assert doc["entries"][0]["node"] == "d11-03"
        assert len(doc["entries"][0]["evidence"]) == 1

    def test_second_segv_same_task_dedups_evidence(self, tmp_path):
        for _ in range(2):
            bl.record_segv(
                tmp_path,
                cluster="discovery",
                node="d11-03",
                run_id="r1",
                job_id="999",
                task_id=7,
            )
        doc = _read_file(tmp_path, "discovery")
        assert len(doc["entries"]) == 1
        assert len(doc["entries"][0]["evidence"]) == 1

    def test_second_segv_different_task_appends(self, tmp_path):
        for tid in (7, 8):
            bl.record_segv(
                tmp_path,
                cluster="discovery",
                node="d11-03",
                run_id="r1",
                job_id="999",
                task_id=tid,
            )
        entry = _read_file(tmp_path, "discovery")["entries"][0]
        assert len(entry["evidence"]) == 2

    def test_evidence_capped_at_max(self, tmp_path):
        for tid in range(bl.MAX_EVIDENCE_PER_NODE + 3):
            bl.record_segv(
                tmp_path,
                cluster="discovery",
                node="d11-03",
                run_id="r1",
                job_id="999",
                task_id=tid,
            )
        entry = _read_file(tmp_path, "discovery")["entries"][0]
        assert len(entry["evidence"]) == bl.MAX_EVIDENCE_PER_NODE
        # Most recent ones survive (FIFO drop of oldest).
        kept_tids = {ev["task_id"] for ev in entry["evidence"]}
        assert max(kept_tids) == bl.MAX_EVIDENCE_PER_NODE + 2

    def test_ttl_refreshed_on_repeat(self, tmp_path):
        t0 = datetime.now(timezone.utc) - timedelta(days=3)
        bl.record_segv(
            tmp_path,
            cluster="discovery",
            node="d11-03",
            run_id="r1",
            job_id="999",
            task_id=7,
            now=t0,
        )
        # TTL initially t0 + 7d.
        doc1 = _read_file(tmp_path, "discovery")
        e1 = doc1["entries"][0]
        # Second SEGV "now" should push expires_at into the future.
        bl.record_segv(
            tmp_path,
            cluster="discovery",
            node="d11-03",
            run_id="r2",
            job_id="998",
            task_id=4,
        )
        doc2 = _read_file(tmp_path, "discovery")
        e2 = doc2["entries"][0]
        assert e2["expires_at"] > e1["expires_at"]
        # added_at should NOT change.
        assert e2["added_at"] == e1["added_at"]

    def test_concurrent_jobs_evidence_recorded(self, tmp_path):
        bl.record_segv(
            tmp_path,
            cluster="discovery",
            node="d11-03",
            run_id="r1",
            job_id="999",
            task_id=7,
            host_allocmem_pct=0.86,
            concurrent_jobs=[
                {"user": "alice", "job_id": "1", "cpus": 24, "started_h_ago": 19}
            ],
        )
        ev = _read_file(tmp_path, "discovery")["entries"][0]["evidence"][0]
        assert ev["host_allocmem_pct"] == 0.86
        assert ev["concurrent_jobs"][0]["user"] == "alice"


class TestGetActive:
    def test_filters_expired(self, tmp_path):
        # Record at t = now - 8d so the default 7d TTL has elapsed.
        long_ago = datetime.now(timezone.utc) - timedelta(days=8)
        bl.record_segv(
            tmp_path,
            cluster="discovery",
            node="d11-03",
            run_id="r1",
            job_id="999",
            task_id=7,
            now=long_ago,
        )
        active = bl.get_active(tmp_path, "discovery")
        assert active == []

    def test_returns_unexpired(self, tmp_path):
        bl.record_segv(
            tmp_path,
            cluster="discovery",
            node="d11-03",
            run_id="r1",
            job_id="999",
            task_id=7,
        )
        active = bl.get_active(tmp_path, "discovery")
        assert len(active) == 1
        assert active[0]["node"] == "d11-03"

    def test_missing_file_returns_empty(self, tmp_path):
        assert bl.get_active(tmp_path, "discovery") == []

    def test_corrupt_file_returns_empty(self, tmp_path):
        path = bl.blacklist_path(tmp_path, "discovery")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert bl.get_active(tmp_path, "discovery") == []


class TestPathNormalization:
    def test_relative_and_absolute_resolve_to_same_file(self, tmp_path, monkeypatch):
        # Writer invoked from a child dir (relative path) and reader
        # invoked from the project root (absolute path) must land on
        # the same blacklist file.
        monkeypatch.chdir(tmp_path)
        bl.record_segv(
            ".",
            cluster="discovery",
            node="d11-03",
            run_id="r1",
            job_id="999",
            task_id=7,
        )
        active_via_abs = bl.get_active(tmp_path, "discovery")
        assert len(active_via_abs) == 1
        assert active_via_abs[0]["node"] == "d11-03"


class TestPruneExpired:
    def test_record_segv_prunes_expired_inline(self, tmp_path):
        # Recording a fresh SEGV implicitly drops every expired entry on
        # disk. This is the documented contract — a separate prune_expired
        # call is for read-only paths that want to reclaim disk space.
        long_ago = datetime.now(timezone.utc) - timedelta(days=10)
        bl.record_segv(
            tmp_path,
            cluster="discovery",
            node="d11-99",
            run_id="r1",
            job_id="999",
            task_id=7,
            now=long_ago,
        )
        # Sanity: the expired entry is on disk before the second call.
        assert len(_read_file(tmp_path, "discovery")["entries"]) == 1
        bl.record_segv(
            tmp_path,
            cluster="discovery",
            node="d11-03",
            run_id="r2",
            job_id="998",
            task_id=4,
        )
        nodes = [e["node"] for e in _read_file(tmp_path, "discovery")["entries"]]
        assert nodes == ["d11-03"]

    def test_prune_expired_standalone_drops_entries(self, tmp_path):
        # Hand-craft a doc with two entries: one expired, one not. Check
        # that prune_expired removes only the expired one.
        long_ago = datetime.now(timezone.utc) - timedelta(days=10)
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        # First write the expired entry. Then hand-craft a second entry
        # in the on-disk JSON without going through record_segv (which
        # prunes inline).
        bl.record_segv(
            tmp_path,
            cluster="discovery",
            node="d11-99",
            run_id="r1",
            job_id="999",
            task_id=7,
            now=long_ago,
        )
        # Append a second entry directly to the file.
        path = bl.blacklist_path(tmp_path, "discovery")
        import json

        doc = json.loads(path.read_text())
        from datetime import timedelta as _td

        doc["entries"].append(
            {
                "node": "d11-03",
                "cluster": "discovery",
                "added_at": recent.isoformat(),
                "expires_at": (recent + _td(days=7)).isoformat(),
                "evidence": [],
            }
        )
        path.write_text(json.dumps(doc))
        removed = bl.prune_expired(tmp_path, "discovery")
        assert removed == 1
        nodes = [e["node"] for e in _read_file(tmp_path, "discovery")["entries"]]
        assert nodes == ["d11-03"]
