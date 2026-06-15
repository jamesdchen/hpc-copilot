"""Tests for cluster_history snapshot persistence and replay."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from hpc_agent._kernel.contract.layout import RepoLayout
from hpc_agent.infra import inspect as ic


def _mk_snap(cluster: str, *, now_iso: str, nodes: int = 1) -> ic.ClusterSnapshot:
    return ic.ClusterSnapshot(
        cluster=cluster,
        scheduler_kind="slurm",
        now_iso=now_iso,
        nodes=[ic.NodeSnapshot(name=f"d11-{i:02d}", state="MIXED") for i in range(nodes)],
    )


class TestPersistRoundTrip:
    def test_round_trip_via_history_dir(self, tmp_path):
        snap = _mk_snap("discovery", now_iso="2026-04-30T12:00:00", nodes=3)
        ic.persist_snapshot(tmp_path, snap)

        snaps = list(ic.read_cluster_history(tmp_path, "discovery"))
        assert len(snaps) == 1
        out = snaps[0]
        assert out.cluster == "discovery"
        assert out.scheduler_kind == "slurm"
        assert out.now_iso == "2026-04-30T12:00:00"
        assert [n.name for n in out.nodes] == ["d11-00", "d11-01", "d11-02"]

    def test_history_dir_is_under_repo_layout(self, tmp_path):
        snap = _mk_snap("discovery", now_iso="2026-04-30T12:00:00")
        path = ic.persist_snapshot(tmp_path, snap)
        expected_dir = RepoLayout(tmp_path).cluster_history("discovery")
        assert path.parent == expected_dir
        assert path.suffix == ".json"

    def test_atomic_write_is_complete_json(self, tmp_path):
        snap = _mk_snap("discovery", now_iso="2026-04-30T12:00:00")
        path = ic.persist_snapshot(tmp_path, snap)
        data = json.loads(path.read_text())
        assert data["cluster"] == "discovery"
        assert isinstance(data["nodes"], list)


class TestPruning:
    def test_prune_at_limit(self, tmp_path, monkeypatch):
        # Drop the cap to a small value so the test is fast.
        monkeypatch.setattr(ic, "MAX_HISTORY_SNAPSHOTS", 3)
        base = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(6):
            t = base + timedelta(seconds=i * 60)
            snap = _mk_snap(
                "discovery",
                now_iso=t.isoformat(timespec="seconds").replace("+00:00", ""),
            )
            ic.persist_snapshot(tmp_path, snap)
        snaps = list(ic.read_cluster_history(tmp_path, "discovery"))
        # Keeps most-recent 3 (oldest-first eviction).
        assert len(snaps) == 3
        # Order is reverse-chronological — latest first.
        nows = [s.now_iso for s in snaps]
        assert nows == sorted(nows, reverse=True)

    def test_per_cluster_isolation(self, tmp_path):
        ic.persist_snapshot(tmp_path, _mk_snap("alpha", now_iso="2026-04-30T12:00:00"))
        ic.persist_snapshot(tmp_path, _mk_snap("beta", now_iso="2026-04-30T13:00:00"))
        a = list(ic.read_cluster_history(tmp_path, "alpha"))
        b = list(ic.read_cluster_history(tmp_path, "beta"))
        assert len(a) == 1
        assert len(b) == 1
        assert a[0].cluster == "alpha"
        assert b[0].cluster == "beta"


class TestSinceFilter:
    def test_since_iso_excludes_older(self, tmp_path):
        for hour in (10, 12, 14):
            ic.persist_snapshot(
                tmp_path,
                _mk_snap("discovery", now_iso=f"2026-04-30T{hour:02d}:00:00"),
            )
        snaps = list(
            ic.read_cluster_history(tmp_path, "discovery", since_iso="2026-04-30T12:00:00")
        )
        # 12:00 and 14:00 included; 10:00 excluded.
        nows = sorted(s.now_iso for s in snaps)
        assert nows == ["2026-04-30T12:00:00", "2026-04-30T14:00:00"]

    def test_limit_caps_yield(self, tmp_path):
        for hour in (10, 11, 12, 13, 14):
            ic.persist_snapshot(
                tmp_path,
                _mk_snap("discovery", now_iso=f"2026-04-30T{hour:02d}:00:00"),
            )
        snaps = list(ic.read_cluster_history(tmp_path, "discovery", limit=2))
        assert len(snaps) == 2
        # Reverse-chronological: 14:00, 13:00.
        assert snaps[0].now_iso == "2026-04-30T14:00:00"
        assert snaps[1].now_iso == "2026-04-30T13:00:00"


class TestEdgeCases:
    def test_read_empty_dir(self, tmp_path):
        # Touch the dir lazily.
        RepoLayout(tmp_path).cluster_history("discovery")
        assert list(ic.read_cluster_history(tmp_path, "discovery")) == []

    def test_read_skips_unparseable_files(self, tmp_path):
        d = RepoLayout(tmp_path).cluster_history("discovery")
        (d / "garbage.json").write_text("not json {{{")
        ic.persist_snapshot(tmp_path, _mk_snap("discovery", now_iso="2026-04-30T12:00:00"))
        snaps = list(ic.read_cluster_history(tmp_path, "discovery"))
        assert len(snaps) == 1

    def test_persist_via_inspect_cluster_kwarg(self, tmp_path, monkeypatch):
        # inspect_cluster returns a synthetic snapshot when persist_dir is set;
        # we patch the SLURM driver to short-circuit external IO. The dispatch
        # (_engine.inspect_cluster) imports ``_slurm_inspect`` FRESH from the
        # ``.slurm`` submodule, so the patch MUST target that module — patching
        # the ``infra.inspect`` package re-export is a dead no-op that lets the
        # test fall through to a live SSH against a bogus host.
        from hpc_agent.infra import inspect as inspect_mod
        from hpc_agent.infra.inspect import slurm as slurm_mod

        captured = {}

        def fake_slurm(*args, **kwargs):
            snap = ic.ClusterSnapshot(
                cluster=args[0],
                scheduler_kind="slurm",
                now_iso="2026-04-30T12:00:00",
                nodes=[],
            )
            captured["snap"] = snap
            return snap

        monkeypatch.setattr(slurm_mod, "_slurm_inspect", fake_slurm)
        monkeypatch.setattr(
            inspect_mod,
            "load_clusters_config",
            lambda _path: {"discovery": {"scheduler": "slurm", "host": "h", "user": "u"}},
        )
        # Disable the in-process cache so we actually call _slurm_inspect.
        inspect_mod._CACHE.clear_all() if hasattr(inspect_mod._CACHE, "clear_all") else None
        snap = inspect_mod.inspect_cluster("discovery", persist_dir=tmp_path, use_cache=False)
        # The fake actually ran (no live SSH fallthrough) and its snapshot is
        # what came back and got persisted.
        assert captured.get("snap") is snap, "patched _slurm_inspect did not run"
        assert snap.cluster == "discovery"
        assert snap.now_iso == "2026-04-30T12:00:00"
        snaps = list(ic.read_cluster_history(tmp_path, "discovery"))
        assert len(snaps) == 1
        assert snaps[0].now_iso == "2026-04-30T12:00:00"
