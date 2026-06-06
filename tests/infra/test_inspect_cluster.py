"""Tests for hpc_agent.infra.inspect — pure parsers + injected runner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hpc_agent.infra import inspect as ins

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
        line = f"99001|alice|RUNNING|24|128G|{_ago_iso(19)}|19:00:00|cpu=24,mem=128G,gres/gpu=1"
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
            "HOSTNAME                ARCH         NCPU NSOC NCOR NTHR  LOAD  MEMTOT  MEMUSE  SWAPTO  SWAPUS\n"  # noqa: E501
            "----------------------------------------------------------------------------------------------\n"  # noqa: E501
            "global                  -               -    -    -    -     -       -       -       -       -\n"  # noqa: E501
            "compute-001             lx-amd64       16    2    8   16  3.50  256.0G   64.0G   16.0G    1.0G\n"  # noqa: E501
            "    gpu=4\n"
            "    gpu_used=1\n"
        )
        nodes = ins._parse_qhost(text)
        assert len(nodes) == 1
        assert nodes[0].gres == "gpu:4"
        assert nodes[0].gres_used == "gpu:1"

    def test_prefixed_gpu_still_works(self):
        text = (
            "HOSTNAME                ARCH         NCPU NSOC NCOR NTHR  LOAD  MEMTOT  MEMUSE  SWAPTO  SWAPUS\n"  # noqa: E501
            "----------------------------------------------------------------------------------------------\n"  # noqa: E501
            "compute-002             lx-amd64       32    2   16   32  5.20  512.0G  100.0G   16.0G    1.0G\n"  # noqa: E501
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


def _sge_combined(
    qhost_out: str, qhost_rc: int, qstat_out: str, qstat_rc: int, qconf_out: str = ""
) -> str:
    """Build the marker-framed stdout the merged qhost+qstat(+qconf) ssh emits.

    qhost+qstat are #295 Fix 3; the qconf section is #293 PR1's PE enumeration.
    """
    return (
        f"__HPC_QHOST__\n{qhost_out}\n__HPC_QHOST_RC__={qhost_rc}\n"
        f"__HPC_QSTAT__\n{qstat_out}\n__HPC_QSTAT_RC__={qstat_rc}\n"
        f"__HPC_QCONF__\n{qconf_out}\n__HPC_QCONF_RC__=0\n"
    )


def _slurm_combined(node_out: str, node_rc: int, part_out: str = "") -> str:
    """Marker-framed stdout the merged ``scontrol show node`` + ``show partition`` emits."""
    return (
        f"__HPC_SCONTROL_NODE__\n{node_out}\n__HPC_SCONTROL_NODE_RC__={node_rc}\n"
        f"__HPC_SCONTROL_PART__\n{part_out}\n__HPC_SCONTROL_PART_RC__=0\n"
    )


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
                # scontrol node + partition now share ONE merged round-trip (#293).
                "echo __HPC_SCONTROL_NODE__": (0, _slurm_combined(_SCONTROL_FIXTURE, 0), ""),
                "sacct": (
                    0,
                    f"99001|alice|RUNNING|24|128G|{_ago_iso(19)}|19:00:00|gres/gpu=1|d11-03",
                    "",
                ),
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
        runner = _FakeRunner(
            {
                "echo __HPC_SCONTROL_NODE__": (0, _slurm_combined(_SCONTROL_FIXTURE, 0), ""),
                "sacct": (0, "", ""),
            }
        )
        ins.inspect_cluster("discovery", runner=runner, use_cache=True)
        first_calls = len(runner.calls)
        ins.inspect_cluster("discovery", runner=runner, use_cache=True)
        assert len(runner.calls) == first_calls

    def test_unknown_cluster_raises(self, tmp_path, monkeypatch):
        # Regression: inspect_cluster used to raise a bare KeyError, which
        # the CLI envelope translator surfaced as `error_code:
        # internal`. The primitive doc declares `cluster_unknown` as the
        # correct code, so the raise is now typed.
        from hpc_agent import errors

        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        ins._CACHE.clear()
        try:
            ins.inspect_cluster("nope", use_cache=False)
        except errors.ClusterUnknown as exc:
            assert "nope" in str(exc)
            assert exc.error_code == "cluster_unknown"
        else:
            raise AssertionError("expected ClusterUnknown")

    def test_scontrol_failure_returns_errors(self, tmp_path, monkeypatch):
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        ins._CACHE.clear()
        # scontrol-node section exits non-zero (captured inline) even though the
        # merged shell ran → still routes to the scontrol_failed error.
        runner = _FakeRunner({"echo __HPC_SCONTROL_NODE__": (0, _slurm_combined("", 1), "")})
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
            "HOSTNAME                ARCH         NCPU NSOC NCOR NTHR  LOAD  MEMTOT  MEMUSE  SWAPTO  SWAPUS\n"  # noqa: E501
            "----------------------------------------------------------------------------------------------\n"  # noqa: E501
            "global                  -               -    -    -    -     -       -       -       -       -\n"  # noqa: E501
            "compute-001             lx-amd64       16    2    8   16  3.50  256.0G   64.0G   16.0G    1.0G\n"  # noqa: E501
            "    hl:gpu=4\n"
            "    gl:gpu_used=1\n"
        )
        # #295 Fix 3: qhost + qstat now arrive over ONE merged ssh round-trip,
        # so the fake returns the marker-framed combined stdout.
        runner = _FakeRunner({"echo __HPC_QHOST__": (0, _sge_combined(qhost_out, 0, "", 0), "")})
        snap = ins.inspect_cluster("discovery", runner=runner, use_cache=False)
        assert snap.scheduler_kind == "sge"
        assert {n.name for n in snap.nodes} == {"compute-001"}
        assert snap.nodes[0].gres == "gpu:4"
        assert snap.nodes[0].gres_used == "gpu:1"
        # exactly one ssh call carried both probes
        assert len(runner.calls) == 1
        assert "qhost" in runner.calls[0] and "qstat" in runner.calls[0]

    def test_runner_invocation_shape_recorded(self, tmp_path, monkeypatch):
        # Defense-in-depth: confirm the SUT actually issues the expected
        # commands rather than relying on the substring-match fake to
        # silently accept whatever the SUT sends. Catches regressions
        # where someone renames `scontrol show node` to `scontrol list`.
        cfg = _write_clusters(tmp_path)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        ins._CACHE.clear()
        runner = _FakeRunner(
            {
                "echo __HPC_SCONTROL_NODE__": (0, _slurm_combined(_SCONTROL_FIXTURE, 0), ""),
                "sacct": (0, "", ""),
            }
        )
        ins.inspect_cluster("discovery", runner=runner, use_cache=False)
        # node + partition ride one merged call; sacct stays its own dependent call.
        assert any(
            "scontrol show node" in c and "scontrol show partition" in c for c in runner.calls
        )
        assert any(c.startswith("sacct -N") for c in runner.calls)

    def test_sge_qhost_qstat_share_one_ssh_call(self) -> None:
        # #295 Fix 3: qhost + qstat now ride ONE ssh round-trip (was #289's
        # concurrent two-call fan). Assert exactly one runner.run, carrying both
        # commands, and that both sections parse cleanly (no spurious errors).
        runner = _FakeRunner({"echo __HPC_QHOST__": (0, _sge_combined("", 0, "", 0), "")})
        snap = ins._sge_inspect(
            "c",
            {},
            stress_alloc_mem_pct=0.8,
            stress_cpu_load_frac=0.8,
            runner=runner,
        )
        assert snap.scheduler_kind == "sge"
        assert len(runner.calls) == 1
        assert "qhost" in runner.calls[0] and "qstat" in runner.calls[0]
        assert snap.errors == []

    def test_sge_qhost_failure_parsed_from_merged_rc(self) -> None:
        # The combined ssh exit code is qstat's; qhost's real exit code is
        # captured inline (echo $?) and must still drive the qhost_failed branch.
        runner = _FakeRunner(
            {"echo __HPC_QHOST__": (0, _sge_combined("error: cannot reach qmaster", 1, "", 0), "")}
        )
        snap = ins._sge_inspect(
            "c", {}, stress_alloc_mem_pct=0.8, stress_cpu_load_frac=0.8, runner=runner
        )
        assert snap.nodes == []
        assert snap.errors and snap.errors[0]["code"] == "qhost_failed"

    def test_split_section_extracts_rc_and_body(self) -> None:
        from hpc_agent.infra.inspect.sge import _split_section

        out = _sge_combined("NODE-LINE", 0, "JOB-LINE", 2)
        assert _split_section(out, "__HPC_QHOST__", "__HPC_QHOST_RC__") == (0, "NODE-LINE")
        assert _split_section(out, "__HPC_QSTAT__", "__HPC_QSTAT_RC__") == (2, "JOB-LINE")
        # Absent markers (round-trip died before the shell ran) → (None, "").
        assert _split_section("", "__HPC_QHOST__", "__HPC_QHOST_RC__") == (None, "")

    def test_sge_enumerates_parallel_environments(self, tmp_path, monkeypatch) -> None:
        # #293 PR1: inspect-cluster surfaces SGE parallel environments, classified
        # by allocation_rule into single-node (smp) vs multi-node (mpi) capability.
        cfg = _write_clusters(tmp_path, scheduler="sge")
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        ins._CACHE.clear()
        qconf = (
            "@@PE@@ make\n"
            "pe_name            make\n"
            "slots              999\n"
            "allocation_rule    $round_robin\n"
            "@@PE@@ smp\n"
            "pe_name            smp\n"
            "slots              512\n"
            "allocation_rule    $pe_slots\n"
            "@@PE@@ mpi\n"
            "pe_name            mpi\n"
            "slots              9999\n"
            "allocation_rule    $fill_up\n"
        )
        runner = _FakeRunner({"echo __HPC_QHOST__": (0, _sge_combined("", 0, "", 0, qconf), "")})
        snap = ins.inspect_cluster("discovery", runner=runner, use_cache=False)
        pes = {pe["name"]: pe for pe in snap.parallel_environments}
        assert set(pes) == {"make", "smp", "mpi"}
        assert all(pe["source"] == "pe" for pe in pes.values())
        assert pes["smp"]["kind"] == "smp" and pes["smp"]["max_nodes"] == 1
        assert pes["smp"]["raw"]["slots"] == 512
        assert pes["mpi"]["kind"] == "mpi" and pes["mpi"]["max_nodes"] is None
        assert pes["make"]["kind"] == "mpi"  # $round_robin → multi-node capable
        assert pes["make"]["raw"]["allocation_rule"] == "$round_robin"
        # surfaced on the serialized envelope too, and the SGE-emitted entries
        # validate against the pinned _ParallelEnvironment def (additionalProperties
        # = False) — symmetric with the PBS conformance check.
        assert "parallel_environments" in snap.to_dict()
        from hpc_agent._kernel.contract.schema import _output_schema_for, validate

        validate(snap.to_dict(), _output_schema_for("inspect-cluster"))

    def test_parse_parallel_environments_unit(self) -> None:
        from hpc_agent.infra.inspect.sge import _classify_pe, _parse_parallel_environments

        assert _classify_pe("$pe_slots") == "smp"
        assert _classify_pe("$round_robin") == "mpi"
        assert _classify_pe("$fill_up") == "mpi"
        assert _classify_pe("4") == "mpi"
        assert _classify_pe("$something_weird") == "other"
        assert _parse_parallel_environments("") == []
        pes = _parse_parallel_environments(
            "@@PE@@ orte\nallocation_rule    $round_robin\nslots              16\n"
        )
        assert pes == [
            {
                "name": "orte",
                "source": "pe",
                "kind": "mpi",
                "max_nodes": None,
                "raw": {"allocation_rule": "$round_robin", "slots": 16},
            }
        ]

    def test_slurm_enumerates_partitions_as_parallel_environments(
        self, tmp_path, monkeypatch
    ) -> None:
        # #293: SLURM partitions surface in parallel_environments, classified
        # mpi (multi-node) vs smp (MaxNodes=1) — riding the node probe's round-trip.
        cfg = _write_clusters(tmp_path, scheduler="slurm")
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        ins._CACHE.clear()
        partitions = (
            "PartitionName=batch\n"
            "   MaxNodes=UNLIMITED TotalCPUs=512 State=UP\n"
            "\n"
            "PartitionName=single\n"
            "   MaxNodes=1 TotalCPUs=64 State=UP\n"
        )
        runner = _FakeRunner(
            {
                "echo __HPC_SCONTROL_NODE__": (
                    0,
                    _slurm_combined(_SCONTROL_FIXTURE, 0, partitions),
                    "",
                ),
                "sacct": (0, "", ""),
            }
        )
        snap = ins.inspect_cluster("discovery", runner=runner, use_cache=False)
        pes = {pe["name"]: pe for pe in snap.parallel_environments}
        assert set(pes) == {"batch", "single"}
        assert all(pe["source"] == "partition" for pe in pes.values())
        assert pes["batch"]["kind"] == "mpi" and pes["batch"]["max_nodes"] is None  # UNLIMITED
        assert pes["batch"]["raw"]["slots"] == 512
        assert pes["single"]["kind"] == "smp" and pes["single"]["max_nodes"] == 1
        # SLURM-emitted entries validate against the pinned _ParallelEnvironment def.
        from hpc_agent._kernel.contract.schema import _output_schema_for, validate

        validate(snap.to_dict(), _output_schema_for("inspect-cluster"))

    def test_parse_scontrol_show_partition_unit(self) -> None:
        from hpc_agent.infra.inspect.slurm import parse_scontrol_show_partition

        text = (
            "PartitionName=gpu\n   MaxNodes=8 TotalCPUs=256\n\n"
            "PartitionName=debug\n   MaxNodes=1 TotalCPUs=16\n"
        )
        pes = {p["name"]: p for p in parse_scontrol_show_partition(text)}
        assert pes["gpu"]["kind"] == "mpi" and pes["gpu"]["max_nodes"] == 8
        assert pes["gpu"]["raw"]["slots"] == 256 and pes["gpu"]["source"] == "partition"
        assert pes["debug"]["kind"] == "smp" and pes["debug"]["max_nodes"] == 1
        assert parse_scontrol_show_partition("") == []
