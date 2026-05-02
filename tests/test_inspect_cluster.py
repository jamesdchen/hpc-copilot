"""Tests for hpc_mapreduce.infra.inspect — pure parsers + injected runner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hpc_mapreduce.infra import inspect as ins


# --- scontrol parser ------------------------------------------------------


_SCONTROL_FIXTURE = """\
NodeName=d11-03 Arch=x86_64 CoresPerSocket=16
   CPUAlloc=24 CPUTot=32 CPULoad=22.10
   AvailableFeatures=v100 ActiveFeatures=v100
   Gres=gpu:v100:2
   GresUsed=gpu:v100:1
   NodeAddr=d11-03 NodeHostName=d11-03 Version=21.08.6
   OS=Linux 5.4.0
   RealMemory=192000 AllocMem=163840 FreeMem=20000 Sockets=2 Boards=1
   State=MIXED ThreadsPerCore=1 TmpDisk=100000 Weight=1 Owner=N/A MCS_label=N/A

NodeName=d11-07 Arch=x86_64 CoresPerSocket=16
   CPUAlloc=4 CPUTot=32 CPULoad=3.20
   AvailableFeatures=v100 ActiveFeatures=v100
   Gres=gpu:v100:2
   GresUsed=gpu:v100:0
   RealMemory=192000 AllocMem=64000 FreeMem=120000
   State=MIXED ThreadsPerCore=1
"""


class TestScontrolParser:
    def test_parses_two_nodes(self):
        snaps = ins.parse_scontrol_show_node(_SCONTROL_FIXTURE)
        assert [s.name for s in snaps] == ["d11-03", "d11-07"]

    def test_alloc_mem_pct_computed(self):
        snaps = ins.parse_scontrol_show_node(_SCONTROL_FIXTURE)
        d11_03 = next(s for s in snaps if s.name == "d11-03")
        # 163840 / 192000 ≈ 0.8533
        assert d11_03.alloc_mem_pct is not None
        assert abs(d11_03.alloc_mem_pct - 0.8533) < 0.01

    def test_cpu_load_frac_computed(self):
        snaps = ins.parse_scontrol_show_node(_SCONTROL_FIXTURE)
        d11_03 = next(s for s in snaps if s.name == "d11-03")
        # 22.10 / 32 ≈ 0.69
        assert d11_03.cpu_load_frac is not None
        assert abs(d11_03.cpu_load_frac - 0.69) < 0.02

    def test_gres_and_features(self):
        snaps = ins.parse_scontrol_show_node(_SCONTROL_FIXTURE)
        d11_03 = next(s for s in snaps if s.name == "d11-03")
        assert d11_03.gres == "gpu:v100:2"
        assert d11_03.gres_used == "gpu:v100:1"
        assert "v100" in d11_03.active_features

    def test_state_drain_flag(self):
        text = _SCONTROL_FIXTURE.replace("State=MIXED", "State=DRAIN", 1)
        snaps = ins.parse_scontrol_show_node(text)
        assert snaps[0].is_drained is True

    def test_empty_input(self):
        assert ins.parse_scontrol_show_node("") == []

    def test_malformed_block_skipped(self):
        # Missing NodeName — block should be ignored, not raise.
        snaps = ins.parse_scontrol_show_node("RealMemory=1000\n\n" + _SCONTROL_FIXTURE)
        assert {s.name for s in snaps} == {"d11-03", "d11-07"}


# --- sacct co-tenant parser ----------------------------------------------


def _ago_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class TestSacctParser:
    def test_parses_live_co_tenant(self):
        # 19h-old job, still RUNNING.
        line = (
            f"99001|alice|RUNNING|24|128G|{_ago_iso(19)}|19:00:00|"
            f"cpu=24,mem=128G,gres/gpu=1"
        )
        rows = ins.parse_sacct_node_jobs(line)
        assert len(rows) == 1
        r = rows[0]
        assert r["user"] == "alice"
        assert r["cpus"] == 24
        assert r["mem_gb"] == 128.0
        assert r["gpus"] == 1
        assert r["started_h_ago"] is not None
        assert r["started_h_ago"] >= 18

    def test_terminal_state_dropped_by_default(self):
        line = f"99002|bob|COMPLETED|8|32G|{_ago_iso(2)}|01:00:00|"
        rows = ins.parse_sacct_node_jobs(line)
        assert rows == []

    def test_terminal_state_kept_when_recent_only_false(self):
        line = f"99002|bob|COMPLETED|8|32G|{_ago_iso(2)}|01:00:00|"
        rows = ins.parse_sacct_node_jobs(line, recent_only=False)
        assert len(rows) == 1
        assert rows[0]["state"] == "COMPLETED"

    def test_step_rows_dropped(self):
        # Two rows for the same job — main + .batch step. We keep one.
        text = (
            f"99003|carol|RUNNING|4|16G|{_ago_iso(1)}|01:00:00|cpu=4\n"
            f"99003.batch|carol|RUNNING|4|16G|{_ago_iso(1)}|01:00:00|cpu=4"
        )
        rows = ins.parse_sacct_node_jobs(text)
        assert len(rows) == 1


# --- nodelist expansion ---------------------------------------------------


class TestQhostBareGpu:
    def test_bare_gpu_without_prefix(self):
        # Some SGE installs emit `gpu=2` directly under the host without
        # the `hl:` / `gl:` prefix. The parser must still capture it.
        text = (
            "HOSTNAME                ARCH         NCPU NSOC NCOR NTHR  LOAD  MEMTOT  MEMUSE  SWAPTO  SWAPUS\n"
            "----------------------------------------------------------------------------------------------\n"
            "global                  -               -    -    -    -     -       -       -       -       -\n"
            "compute-001             lx-amd64       16    2    8   16  3.50  256.0G   64.0G   16.0G    1.0G\n"
            "    gpu=4\n"
            "    gpu_used=1\n"
        )
        nodes = ins._parse_qhost(text)
        assert len(nodes) == 1
        assert nodes[0].gres == "gpu:4"
        assert nodes[0].gres_used == "gpu:1"

    def test_prefixed_gpu_still_works(self):
        text = (
            "HOSTNAME                ARCH         NCPU NSOC NCOR NTHR  LOAD  MEMTOT  MEMUSE  SWAPTO  SWAPUS\n"
            "----------------------------------------------------------------------------------------------\n"
            "compute-002             lx-amd64       32    2   16   32  5.20  512.0G  100.0G   16.0G    1.0G\n"
            "    hl:gpu=8\n"
            "    gl:gpu_used=2\n"
        )
        nodes = ins._parse_qhost(text)
        assert nodes[0].gres == "gpu:8"
        assert nodes[0].gres_used == "gpu:2"


class TestNodelistExpansion:
    def test_single_node(self):
        assert ins._expand_slurm_nodelist("d11-03") == ["d11-03"]

    def test_comma_separated(self):
        assert ins._expand_slurm_nodelist("d11-03,d11-07") == ["d11-03", "d11-07"]

    def test_range(self):
        assert ins._expand_slurm_nodelist("d11-[03-05]") == ["d11-03", "d11-04", "d11-05"]

    def test_mixed_alternation(self):
        assert ins._expand_slurm_nodelist("d11-[03,07-08]") == [
            "d11-03",
            "d11-07",
            "d11-08",
        ]

    def test_empty_returns_empty(self):
        assert ins._expand_slurm_nodelist("") == []


# --- public entry with mocked runner -------------------------------------


class _FakeRunner:
    def __init__(self, responses: dict[str, tuple[int, str, str]]):
        self._responses = responses
        self.calls: list[str] = []

    def run(self, cmd: str) -> tuple[int, str, str]:
        self.calls.append(cmd)
        for needle, response in self._responses.items():
            if cmd.startswith(needle):
                return response
        return 0, "", ""


def _write_clusters(tmp_path, scheduler="slurm"):
    """Write a minimal clusters.yaml and point the env var at it."""
    p = tmp_path / "clusters.yaml"
    p.write_text(
        "discovery:\n"
        "  host: example.invalid\n"
        "  user: tester\n"
        f"  scheduler: {scheduler}\n"
        "  scratch: /tmp\n"
        "  gpu_types: [v100, a100]\n"
    )
    return p


class TestInspectClusterEntry:
    def test_slurm_happy_path(self, tmp_path, monkeypatch):
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        ins._CACHE.clear()
        runner = _FakeRunner(
            {
                "scontrol show node": (0, _SCONTROL_FIXTURE, ""),
                "sacct": (0, f"99001|alice|RUNNING|24|128G|{_ago_iso(19)}|19:00:00|gres/gpu=1|d11-03", ""),
            }
        )
        snap = ins.inspect_cluster("discovery", runner=runner, use_cache=False)
        assert snap.scheduler_kind == "slurm"
        assert {n.name for n in snap.nodes} == {"d11-03", "d11-07"}
        d11_03 = next(n for n in snap.nodes if n.name == "d11-03")
        # AllocMem 86% triggers stress flag.
        assert d11_03.is_stressed is True
        # Co-tenant attribution made it through bucketing.
        assert any(t["user"] == "alice" for t in d11_03.co_tenants)

    def test_cache_short_circuits_repeated_calls(self, tmp_path, monkeypatch):
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        ins._CACHE.clear()
        runner = _FakeRunner({"scontrol show node": (0, _SCONTROL_FIXTURE, ""), "sacct": (0, "", "")})
        ins.inspect_cluster("discovery", runner=runner, use_cache=True)
        first_calls = len(runner.calls)
        ins.inspect_cluster("discovery", runner=runner, use_cache=True)
        assert len(runner.calls) == first_calls

    def test_unknown_cluster_raises(self, tmp_path, monkeypatch):
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        ins._CACHE.clear()
        try:
            ins.inspect_cluster("nope", use_cache=False)
        except KeyError as exc:
            assert "nope" in str(exc)
        else:
            raise AssertionError("expected KeyError")

    def test_scontrol_failure_returns_errors(self, tmp_path, monkeypatch):
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        ins._CACHE.clear()
        runner = _FakeRunner({"scontrol show node": (1, "", "auth failed")})
        snap = ins.inspect_cluster("discovery", runner=runner, use_cache=False)
        assert snap.nodes == []
        assert snap.errors and snap.errors[0]["code"] == "scontrol_failed"

    def test_sge_happy_path(self, tmp_path, monkeypatch):
        # Verify the SGE branch end-to-end: inspect_cluster routes to
        # _sge_inspect, which invokes qhost + qstat. Mirrors the SLURM
        # happy-path coverage above.
        cfg = _write_clusters(tmp_path, scheduler="sge")
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        ins._CACHE.clear()
        qhost_out = (
            "HOSTNAME                ARCH         NCPU NSOC NCOR NTHR  LOAD  MEMTOT  MEMUSE  SWAPTO  SWAPUS\n"
            "----------------------------------------------------------------------------------------------\n"
            "global                  -               -    -    -    -     -       -       -       -       -\n"
            "compute-001             lx-amd64       16    2    8   16  3.50  256.0G   64.0G   16.0G    1.0G\n"
            "    hl:gpu=4\n"
            "    gl:gpu_used=1\n"
        )
        runner = _FakeRunner(
            {
                "qhost": (0, qhost_out, ""),
                "qstat": (0, "", ""),
            }
        )
        snap = ins.inspect_cluster("discovery", runner=runner, use_cache=False)
        assert snap.scheduler_kind == "sge"
        assert {n.name for n in snap.nodes} == {"compute-001"}
        assert snap.nodes[0].gres == "gpu:4"
        assert snap.nodes[0].gres_used == "gpu:1"

    def test_runner_invocation_shape_recorded(self, tmp_path, monkeypatch):
        # Defense-in-depth: confirm the SUT actually issues the expected
        # commands rather than relying on the substring-match fake to
        # silently accept whatever the SUT sends. Catches regressions
        # where someone renames `scontrol show node` to `scontrol list`.
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        ins._CACHE.clear()
        runner = _FakeRunner(
            {"scontrol show node": (0, _SCONTROL_FIXTURE, ""), "sacct": (0, "", "")}
        )
        ins.inspect_cluster("discovery", runner=runner, use_cache=False)
        assert any(c.startswith("scontrol show node") for c in runner.calls)
        assert any(c.startswith("sacct -N") for c in runner.calls)
