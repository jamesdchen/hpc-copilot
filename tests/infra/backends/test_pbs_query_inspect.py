"""PBS history query (qstat -xf -> Exit_status) + minimal inspect snapshot."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from hpc_agent.infra.backends import get_backend_class
from hpc_agent.infra.backends.query import _parse_qstat_full_pbs, query_pbs
from hpc_agent.infra.inspect.pbs import parse_pbsnodes

# A realistic ``qstat -xf -t`` buffer: two finished subjobs (ok + failed),
# one still running, and the array parent (no index → skipped).
_QSTAT_XF = """Job Id: 12345[1].pbsserver
    Job_Name = cpu_array
    job_state = F
    Exit_status = 0
    resources_used.walltime = 01:00:00
    resources_used.ncpus = 4
Job Id: 12345[2].pbsserver
    Job_Name = cpu_array
    job_state = F
    Exit_status = 1
    resources_used.walltime = 00:00:12
    resources_used.ncpus = 4
Job Id: 12345[3].pbsserver
    Job_Name = cpu_array
    job_state = R
    resources_used.ncpus = 4
Job Id: 12345[].pbsserver
    Job_Name = cpu_array
    job_state = B
"""


def test_parse_qstat_full_pbs_states_and_exit_status():
    tasks: dict[int, dict] = {}
    _parse_qstat_full_pbs(_QSTAT_XF, tasks)

    assert tasks[1]["state"] == "COMPLETED" and tasks[1]["exit_code"] == "0"
    assert tasks[2]["state"] == "FAILED" and tasks[2]["exit_code"] == "1"
    assert tasks[3]["state"] == "RUNNING" and tasks[3]["exit_code"] is None
    # The array parent ``[]`` carries no task index → no entry.
    assert set(tasks) == {1, 2, 3}
    # Resource usage parsed: 01:00:00 -> 3600s, ncpus=4 -> cpu_s = 4*3600.
    assert tasks[1]["elapsed_s"] == 3600
    assert tasks[1]["cpu_s"] == 4 * 3600
    assert tasks[1]["job_id"] == "12345"


def test_query_pbs_pbspro_uses_x_flag():
    captured: list[list[str]] = []

    def _run(cmd, **kwargs):
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stdout=_QSTAT_XF, stderr="")

    with patch("hpc_agent.infra.backends.query.subprocess.run", side_effect=_run):
        out = query_pbs(["12345"], fork="pbspro")

    assert captured[0][:4] == ["qstat", "-x", "-f", "-t"]  # pbspro needs -x for history
    assert out["tasks"][2]["state"] == "FAILED"
    assert out["errors"] == []


def test_query_pbs_torque_omits_x_flag():
    def _run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=_QSTAT_XF, stderr="")

    with patch("hpc_agent.infra.backends.query.subprocess.run", side_effect=_run):
        captured: list[list[str]] = []

        def _run2(cmd, **kwargs):
            captured.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("hpc_agent.infra.backends.query.subprocess.run", side_effect=_run2):
            query_pbs(["9"], fork="torque")
        assert captured[0][:3] == ["qstat", "-f", "-t"] and "-x" not in captured[0]


def test_engine_query_jobs_dispatches_to_pbs():
    def _run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=_QSTAT_XF, stderr="")

    with patch("hpc_agent.infra.backends.query.subprocess.run", side_effect=_run):
        result = get_backend_class("pbspro").query_jobs(["12345"])
    assert result["tasks"][1]["state"] == "COMPLETED"


def test_pbs_inspect_returns_valid_minimal_snapshot():
    # No runner threaded in → can't probe pbsnodes → safe minimal fallback.
    snap = get_backend_class("torque").inspect_cluster("mycluster", {})
    d = snap.to_dict()
    assert d["scheduler_kind"] == "torque"
    assert d["cluster"] == "mycluster"
    assert d["nodes"] == []  # node-level backfill data intentionally unpopulated
    assert any(e["code"] == "pbs_inspect_minimal" for e in d["errors"])


# --- pbsnodes parsers (the backfill/throughput enrichment, #215) ----------

# Curated from the PBS Pro/OpenPBS ``pbsnodes -av`` man-page stanza shape.
# Memory tokens use clean GiB multiples (1 GiB = 1048576 kb) so the
# kb→MB conversion lands on exact values. Node 3 is down+offline.
_PBSPRO_AV = """gpu-node-01
     Mom = gpu-node-01.hpc.example.org
     ntype = PBS
     state = job-busy
     pcpus = 128
     resources_available.arch = linux
     resources_available.mem = 536870912kb
     resources_available.ncpus = 128
     resources_available.ngpus = 4
     resources_assigned.mem = 268435456kb
     resources_assigned.ncpus = 64
     resources_assigned.ngpus = 2
     jobs = 41[].server/0, 41[].server/1

cpu-node-02
     Mom = cpu-node-02.hpc.example.org
     state = free
     pcpus = 48
     resources_available.mem = 201326592kb
     resources_available.ncpus = 48
     resources_assigned.mem = 0kb
     resources_assigned.ncpus = 0

dead-node-03
     Mom = dead-node-03.hpc.example.org
     state = down,offline
     pcpus = 48
     resources_available.mem = 201326592kb
     resources_available.ncpus = 48
"""

# Curated from the TORQUE ``pbsnodes -a`` stanza shape: ``np`` core count
# plus a packed ``status = k=v,...`` line carrying physmem/availmem/loadave.
_TORQUE_A = """node-t1
     state = job-exclusive
     power_state = Running
     np = 16
     ntype = cluster
     status = state=free,ncpus=16,physmem=536870912kb,availmem=268435456kb,loadave=8.00
     gpus = 2
     mom_service_port = 15002

node-t2
     state = free
     np = 8
     ntype = cluster
     status = ncpus=8,physmem=201326592kb,availmem=201326592kb,loadave=0.05

node-t3
     state = down,offline
     np = 8
     ntype = cluster
"""


def test_parse_pbsnodes_pbspro_populates_capacity_and_alloc():
    nodes = {n.name: n for n in parse_pbsnodes(_PBSPRO_AV, family="pbspro")}
    assert set(nodes) == {"gpu-node-01", "cpu-node-02", "dead-node-03"}

    gpu = nodes["gpu-node-01"]
    assert gpu.state == "job-busy"
    assert gpu.is_drained is False  # busy is "up", not unusable
    assert gpu.cpu_tot == 128 and gpu.cpu_alloc == 64
    assert gpu.real_mem_mb == 512 * 1024 and gpu.alloc_mem_mb == 256 * 1024
    assert gpu.alloc_mem_pct == 0.5
    assert gpu.gres == "gpu:4" and gpu.gres_used == "gpu:2"

    free = nodes["cpu-node-02"]
    assert free.is_drained is False
    assert free.cpu_tot == 48 and free.cpu_alloc == 0
    assert free.alloc_mem_mb == 0 and free.alloc_mem_pct == 0.0

    dead = nodes["dead-node-03"]
    assert dead.is_drained is True  # down,offline → unusable capacity


def test_parse_pbsnodes_pbspro_falls_back_to_pcpus_for_core_count():
    text = "n1\n     state = free\n     pcpus = 64\n"
    (node,) = parse_pbsnodes(text, family="pbspro")
    assert node.cpu_tot == 64


def test_parse_pbsnodes_torque_populates_from_status_line():
    nodes = {n.name: n for n in parse_pbsnodes(_TORQUE_A, family="torque")}
    assert set(nodes) == {"node-t1", "node-t2", "node-t3"}

    n1 = nodes["node-t1"]
    assert n1.state == "job-exclusive" and n1.is_drained is False
    assert n1.cpu_tot == 16  # status.ncpus confirms np
    # physmem 512 GiB total, availmem 256 GiB free → 256 GiB used, 50%.
    assert n1.real_mem_mb == 512 * 1024
    assert n1.alloc_mem_mb == 256 * 1024 and n1.alloc_mem_pct == 0.5
    assert n1.cpu_load == 8.0 and n1.cpu_load_frac == 0.5
    assert n1.gres == "gpu:2"

    n2 = nodes["node-t2"]
    assert n2.cpu_tot == 8 and n2.is_drained is False
    assert n2.alloc_mem_mb == 0 and n2.alloc_mem_pct == 0.0  # fully free
    assert n2.cpu_load == 0.05

    n3 = nodes["node-t3"]
    assert n3.is_drained is True
    assert n3.cpu_tot == 8 and n3.real_mem_mb is None  # no status line


def test_parse_pbsnodes_empty_returns_no_nodes():
    assert parse_pbsnodes("", family="pbspro") == []
    assert parse_pbsnodes("\n  \n", family="torque") == []


def _runner(responses):
    """Minimal stand-in for ``_CommandRunner`` keyed by command prefix."""

    class _R:
        calls: list[str] = []

        def run(self, cmd):
            self.calls.append(cmd)
            for needle, resp in responses.items():
                if cmd.startswith(needle):
                    return resp
            return 0, "", ""

    return _R()


def _pbs_combined(
    pbsnodes_out: str, pbsnodes_rc: int, qstat_out: str = "", queues_out: str = ""
) -> str:
    """Marker-framed stdout the merged pbsnodes + qstat -an1 + qstat -Qf probe emits."""
    return (
        f"__HPC_PBSNODES__\n{pbsnodes_out}\n__HPC_PBSNODES_RC__={pbsnodes_rc}\n"
        f"__HPC_QSTAT__\n{qstat_out}\n__HPC_QSTAT_RC__=0\n"
        f"__HPC_QUEUES__\n{queues_out}\n__HPC_QUEUES_RC__=0\n"
    )


def test_pbs_inspect_pbspro_happy_path_populates_nodes():
    # pbsnodes + qstat now ride ONE merged ssh round-trip (PBS co-tenant work).
    runner = _runner({"echo __HPC_PBSNODES__": (0, _pbs_combined(_PBSPRO_AV, 0), "")})
    snap = get_backend_class("pbspro").inspect_cluster("c", {}, runner=runner)
    d = snap.to_dict()
    assert d["scheduler_kind"] == "pbspro"
    assert {n["name"] for n in d["nodes"]} == {
        "gpu-node-01",
        "cpu-node-02",
        "dead-node-03",
    }
    # Real data present → the minimal-fallback note is dropped.
    assert d["errors"] == []
    # exactly one round-trip carried both pbsnodes and qstat
    assert len(runner.calls) == 1
    assert "pbsnodes -av" in runner.calls[0] and "qstat -an1" in runner.calls[0]


def test_pbs_inspect_torque_uses_plain_pbsnodes_a():
    runner = _runner({"echo __HPC_PBSNODES__": (0, _pbs_combined(_TORQUE_A, 0), "")})
    snap = get_backend_class("torque").inspect_cluster("c", {}, runner=runner)
    assert len(runner.calls) == 1
    assert "pbsnodes -a" in runner.calls[0] and "pbsnodes -av" not in runner.calls[0]
    assert len(snap.nodes) == 3
    assert snap.errors == []


def test_pbs_inspect_falls_back_to_minimal_on_command_failure():
    # pbsnodes exits non-zero (captured inline as the section's $?), even though
    # the merged shell itself ran → still routes to the safe minimal snapshot.
    runner = _runner({"echo __HPC_PBSNODES__": (0, _pbs_combined("", 1), "")})
    snap = get_backend_class("pbspro").inspect_cluster("c", {}, runner=runner)
    assert snap.nodes == []
    assert any(e["code"] == "pbs_inspect_minimal" for e in snap.errors)


def test_pbs_inspect_falls_back_to_minimal_on_unparseable_output():
    runner = _runner(
        {"echo __HPC_PBSNODES__": (0, _pbs_combined("garbage with no stanzas", 0), "")}
    )
    snap = get_backend_class("pbspro").inspect_cluster("c", {}, runner=runner)
    assert snap.nodes == []
    assert any(e["code"] == "pbs_inspect_minimal" for e in snap.errors)


def test_pbs_inspect_pbspro_output_conforms_to_schema():
    # The populated node dict must satisfy inspect-cluster.output.json — in
    # particular the alloc_mem_pct [0, 1] bound that validate_output enforces
    # at CLI emit. (errors=[] on the happy path, so the snapshot's dict-shaped
    # errors — lifted to partial_errors by the CLI wrapper — don't apply here.)
    from hpc_agent._kernel.contract.schema import _output_schema_for, validate

    # Include a populated queue so the normalized parallel_environments entries
    # are validated against the pinned _ParallelEnvironment def (additionalProperties
    # = False) — catches a parser emitting an unexpected key.
    queues = "Queue: workq\n    queue_type = Execution\n    resources_max.nodect = 8\n"
    runner = _runner({"echo __HPC_PBSNODES__": (0, _pbs_combined(_PBSPRO_AV, 0, "", queues), "")})
    snap = get_backend_class("pbspro").inspect_cluster("c", {}, runner=runner)
    assert snap.errors == []
    assert snap.parallel_environments and snap.parallel_environments[0]["name"] == "workq"
    schema = _output_schema_for("inspect-cluster")
    validate(snap.to_dict(), schema)  # raises on any field/constraint mismatch


def test_pbs_inspect_attaches_co_tenants_from_qstat():
    # PBS co-tenant parity with SLURM/SGE: qstat -an1 jobs are bucketed per node.
    qstat = (
        "pbs-server:\n"
        "                                                            Req'd  Req'd   Elap\n"
        "Job ID          Username Queue    Jobname  SessID NDS TSK Memory Time S Time\n"
        "--------------- -------- -------- -------- ------ --- --- ------ ---- - ----\n"
        "101.pbs alice workq train 1234 1 4 8gb 24:00 R 01:23 gpu-node-01/0*4\n"
        "102.pbs bob   workq sim   1235 2 8 16gb 24:00 R 00:30 cpu-node-02/0*4+gpu-node-01/0*2\n"
        "103.pbs carol workq queued -- 1 1 1gb 01:00 Q -- --\n"
    )
    runner = _runner({"echo __HPC_PBSNODES__": (0, _pbs_combined(_PBSPRO_AV, 0, qstat), "")})
    snap = get_backend_class("pbspro").inspect_cluster("c", {}, runner=runner)
    nodes = {n.name: n for n in snap.nodes}
    by_user = {t["user"]: t for t in nodes["gpu-node-01"].co_tenants}
    assert set(by_user) == {"alice", "bob"}  # both placed on gpu-node-01
    assert by_user["alice"]["cpus"] == 4 and by_user["alice"]["state"] == "R"
    assert by_user["bob"]["cpus"] == 2  # bob's gpu-node-01 share, not his 4 on cpu-node-02
    # carol's job is queued (no exec_host) → attributed to no node.
    assert all(t["user"] != "carol" for n in snap.nodes for t in n.co_tenants)


def test_parse_qstat_co_tenants_unit():
    from hpc_agent.infra.inspect.pbs import parse_qstat_co_tenants

    out = parse_qstat_co_tenants(
        "101.pbs alice workq j 1234 1 4 8gb 24:00 R 01:23 node01/0*4+node02/0*2\n"
    )
    assert set(out) == {"node01", "node02"}
    assert out["node01"][0]["cpus"] == 4 and out["node01"][0]["user"] == "alice"
    assert out["node02"][0]["cpus"] == 2
    # queued job (no exec_host) and header/separator lines are ignored.
    assert parse_qstat_co_tenants("102.pbs bob q j 0 1 1 1gb 1:00 Q -- --") == {}
    assert parse_qstat_co_tenants("Job ID Username\n--------------- --------\npbs-server:") == {}


def test_pbs_inspect_enumerates_queues_as_parallel_environments():
    # #293: PBS execution queues surface in parallel_environments; Route queues
    # (which forward rather than run) are skipped.
    queues = (
        "Queue: workq\n"
        "    queue_type = Execution\n"
        "    resources_max.nodect = 16\n"
        "    resources_max.ncpus = 512\n"
        "Queue: serial\n"
        "    queue_type = Execution\n"
        "    resources_max.nodect = 1\n"
        "Queue: routeq\n"
        "    queue_type = Route\n"
    )
    runner = _runner({"echo __HPC_PBSNODES__": (0, _pbs_combined(_PBSPRO_AV, 0, "", queues), "")})
    snap = get_backend_class("pbspro").inspect_cluster("c", {}, runner=runner)
    pes = {pe["name"]: pe for pe in snap.parallel_environments}
    assert set(pes) == {"workq", "serial"}  # Route queue skipped
    assert all(pe["source"] == "queue" for pe in pes.values())
    assert pes["workq"]["kind"] == "mpi" and pes["workq"]["max_nodes"] == 16
    assert pes["workq"]["raw"]["slots"] == 512
    assert pes["serial"]["kind"] == "smp" and pes["serial"]["max_nodes"] == 1


def test_parse_qstat_queues_unit():
    from hpc_agent.infra.inspect.pbs import parse_qstat_queues

    text = (
        "Queue: big\n"
        "    queue_type = Execution\n"
        "    resources_max.nodect = 4\n"
        "    resources_max.ncpus = 128\n"
    )
    assert parse_qstat_queues(text) == [
        {
            "name": "big",
            "source": "queue",
            "kind": "mpi",
            "max_nodes": 4,
            "raw": {"slots": 128},
        }
    ]
    assert parse_qstat_queues("Queue: r\n    queue_type = Route\n") == []
    assert parse_qstat_queues("") == []


def test_parse_qstat_queues_torque_reads_nodes_nodespec():
    # #293 family-gate: TORQUE states the cap as resources_max.nodes (a nodespec
    # like 4:ppn=8), not pbspro's resources_max.nodect — take the leading count.
    from hpc_agent.infra.inspect.pbs import parse_qstat_queues

    text = "Queue: batch\n    queue_type = Execution\n    resources_max.nodes = 4:ppn=8\n"
    (q,) = parse_qstat_queues(text, family="torque")
    assert q == {
        "name": "batch",
        "source": "queue",
        "kind": "mpi",
        "max_nodes": 4,
        "raw": {"slots": None},
    }
    # single-node nodespec → smp
    serial = "Queue: serial\n    queue_type = Execution\n    resources_max.nodes = 1\n"
    assert parse_qstat_queues(serial, family="torque")[0]["kind"] == "smp"


def test_parse_qstat_co_tenants_torque_range_exec_host():
    # #293 family robustness: TORQUE exec_host range form node/0-3 → 4 cores.
    from hpc_agent.infra.inspect.pbs import parse_qstat_co_tenants

    out = parse_qstat_co_tenants("1.srv alice batch j 0 1 4 8gb 1:00 R 0:30 node01/0-3\n")
    assert out["node01"][0]["cpus"] == 4


def test_parse_pbsnodes_pbspro_clamps_overcommitted_alloc_mem():
    # assigned.mem (256 GiB) > available.mem (192 GiB): independently-reported
    # PBS values can over-commit; alloc_mem_pct must clamp to 1.0 rather than
    # exceed the schema's [0, 1] bound.
    text = (
        "n1\n"
        "     state = job-busy\n"
        "     resources_available.ncpus = 8\n"
        "     resources_available.mem = 201326592kb\n"
        "     resources_assigned.mem = 268435456kb\n"
    )
    (node,) = parse_pbsnodes(text, family="pbspro")
    assert node.alloc_mem_pct == 1.0


def test_parse_pbsnodes_pbspro_no_gpu_node_has_empty_gres():
    # PBS Pro reports resources_available.ngpus = 0 on CPU nodes; that must
    # not advertise a phantom "gpu:0" GRES (matches SLURM's empty-gres shape).
    text = (
        "n1\n"
        "     state = free\n"
        "     resources_available.ncpus = 8\n"
        "     resources_available.ngpus = 0\n"
    )
    (node,) = parse_pbsnodes(text, family="pbspro")
    assert node.gres == "" and node.gres_used == ""
