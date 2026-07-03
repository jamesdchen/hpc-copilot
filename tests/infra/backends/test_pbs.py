"""PBS family (pbspro / torque) engine behaviour.

Curated from the PBS Pro/OpenPBS + TORQUE man pages, pbs-drmaa state
mapping, and Nextflow's PbsExecutor/PbsProExecutor. The two variants are
distinct families because they diverge structurally (array flag, index
env var, resource grammar, finished-state token, history query).
"""

from __future__ import annotations

import pytest

from hpc_agent.infra.backends import get_backend, get_backend_class


def _noop_ssh(_cmd):
    from types import SimpleNamespace

    return SimpleNamespace(stdout="", stderr="", returncode=0)


def _backend(family, **over):
    kw = dict(script="cpu.pbs", ssh_run=_noop_ssh, remote_repo="/r", pass_env_keys=("K",))
    kw.update(over)
    return get_backend(family, **kw)


# --- class metadata --------------------------------------------------------


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_metadata(family):
    cls = get_backend_class(family)
    assert cls.scheduler_name == family
    assert cls.template_ext == ".pbs"
    assert cls.supports_test_only_eta is False
    # job-id regex captures the numeric sequence from <seq>.<server> / <seq>[].<server>
    assert cls.JOB_ID_REGEX.search("12345.pbsserver").group(1) == "12345"
    assert cls.JOB_ID_REGEX.search("12346[].hpcnode0").group(1) == "12346"


# --- submit command shape (the canonical fork split) -----------------------


def test_pbspro_submit_uses_J_and_joins_streams():
    b = _backend("pbspro")
    assert b._build_command("1-10", "job", {"K": "V"}) == [
        "qsub",
        "-J",
        "1-10",
        "-N",
        "job",
        "-o",
        "/r/logs",
        "-j",
        "oe",
        "-v",
        "K=V",
        "cpu.pbs",
    ]


def test_torque_submit_uses_t():
    b = _backend("torque")
    cmd = b._build_command("1-10", "job", {"K": "V"})
    assert cmd[:3] == ["qsub", "-t", "1-10"]
    assert "-J" not in cmd
    assert cmd[-1] == "cpu.pbs"


def test_pbs_v_comma_guard():
    from hpc_agent import errors

    b = _backend("pbspro", pass_env_keys=("MODULES",))
    with pytest.raises(errors.SpecInvalid, match="','"):
        b._build_command("1-1", "job", {"MODULES": "python/3.11,gcc/11"})


def test_pbs_dependency_flag():
    for fam in ("pbspro", "torque"):
        b = _backend(fam)
        assert b._build_dependency_flag(["12.s", "13.s"]) == ["-W", "depend=afterany:12.s:13.s"]
        assert b._build_dependency_flag([]) == []


# --- resource flags (the second fork split) --------------------------------


def _res(**kw):
    from hpc_agent._wire.workflows.submit_flow import SubmitResources

    return SubmitResources(**kw)


def test_pbspro_resource_select_syntax():
    b = _backend("pbspro")
    assert b.resource_flags(_res(cpus=8, mem_mb=4096, walltime_sec=7200)) == [
        "-l",
        "select=1:ncpus=8:mem=4096mb",
        "-l",
        "walltime=02:00:00",
    ]
    assert b.resource_flags(_res()) == []  # opt-in


def test_torque_resource_nodes_ppn_syntax():
    b = _backend("torque")
    assert b.resource_flags(_res(cpus=8, mem_mb=4096, walltime_sec=7200)) == [
        "-l",
        "nodes=1:ppn=8,mem=4096mb,walltime=02:00:00",
    ]
    assert b.resource_flags(_res()) == []


# --- state classification (live qstat tokens) ------------------------------

_PBS_CLASSIFY = [
    ("Q", "alive"),
    ("R", "alive"),
    ("E", "alive"),
    ("B", "alive"),
    ("T", "alive"),
    ("W", "alive"),
    ("M", "alive"),
    ("H", "held"),
    ("S", "held"),
    ("U", "held"),
]


@pytest.mark.parametrize(("state", "bucket"), _PBS_CLASSIFY)
@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_classify(family, state, bucket):
    assert get_backend_class(family).classify_scheduler_state(state) == bucket


# --- live-state command shape (qstat -t <ids>, NOT qstat -u) ---------------


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_live_cmds_use_explicit_ids_not_wide_u_format(family):
    # ``qstat -u`` would trigger PBS's wide alternate listing (state column
    # shifts off index 4); passing explicit ids keeps the brief format and
    # ``-t`` expands array subjobs.
    cls = get_backend_class(family)
    for cmd in (
        cls.build_alive_check_cmd(["12345", "12346"]),
        cls.build_scheduler_state_cmd(["12345", "12346"]),
    ):
        assert cmd.startswith("qstat -t ")
        assert "-u" not in cmd
        assert "12345" in cmd and "12346" in cmd
    # empty id list short-circuits (no stray ``qstat -t`` with no args)
    assert cls.build_alive_check_cmd([]) == "true"
    assert cls.build_scheduler_state_cmd([]) == "true"


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_build_cancel_cmd_uses_qdel(family):
    # PBS (like SGE) cancels via ``qdel <id> <id> ...``; empty ids short-circuit.
    cls = get_backend_class(family)
    assert cls.build_cancel_cmd(["12345", "12346"]) == "qdel 12345 12346"
    assert cls.build_cancel_cmd([]) == "true"


# --- qstat -t parsing (brief format; id is <seq>.<server>[<idx>]) ----------
# Matches PBS's *brief* listing (the format emitted when ids are passed),
# where the single-letter state sits at column index 4.

_QSTAT = (
    "Job id            Name   User  Time Use S Queue\n"
    "----------------  -----  ----  -------- - -----\n"
    "12345.pbsserver   job    a     01:00:00 R workq\n"
    "12347.pbsserver   prep   a     00:00:00 H workq\n"
    "12346[].pbsserver arr    a     10:00:00 B workq\n"
)


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_parse_alive_strips_server_and_brackets(family):
    cls = get_backend_class(family)
    alive = cls.parse_alive_output(_QSTAT, ["12345", "12346", "12347", "99999"])
    assert alive == {"12345", "12347", "12346"}


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_parse_states(family):
    cls = get_backend_class(family)
    states = cls.parse_scheduler_states(_QSTAT, ["12345", "12346", "12347"])
    assert states == {"12345": "R", "12347": "H", "12346": "B"}
    assert cls.classify_scheduler_state(states["12347"]) == "held"


# --- log paths reuse the SGE .o<id>.<idx> layout ---------------------------


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_log_paths(family):
    cls = get_backend_class(family)
    assert cls.stderr_log_path("/repo", "job", "555", 0) == "/repo/logs/job.o555.1"


@pytest.mark.parametrize(
    ("family", "index_var"),
    [("pbspro", "PBS_ARRAY_INDEX"), ("torque", "PBS_ARRAYID")],
)
def test_pbs_script_pins_log_filename_to_stderr_log_path(family, index_var):
    # #217: the script must redirect each task's output to the exact name
    # stderr_log_path expects (<job_name>.o<seq>.<array-index>), rather than
    # rely on PBS's variant-dependent default array-log naming. The bare-seq
    # extraction must use a leading-digit run (works for pbspro ``12345[3]``
    # and torque ``12345-3``), and the array index must be the family's own var.
    body = get_backend_class(family).render_script(kind="cpu")
    assert 'PBS_SEQ="${PBS_JOBID%%[!0-9]*}"' in body
    assert f'exec >"logs/${{PBS_JOBNAME}}.o${{PBS_SEQ}}.${{{index_var}}}" 2>&1' in body
    # the redirect mirrors stderr_log_path's <job_name>.o<job_id>.<idx> shape
    assert get_backend_class(family).stderr_log_path("/r", "j", "9", 0) == "/r/logs/j.o9.1"


# --- history (qstat -xf -> Exit_status) + minimal inspect snapshot ---------


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_query_jobs_dispatches_without_raising(family):
    from unittest.mock import patch

    # query_pbs shells out to qstat; with no real cluster it returns an empty
    # task map + a diagnostic error rather than raising.
    with patch(
        "hpc_agent.infra.backends.query.subprocess.run",
        side_effect=FileNotFoundError("qstat"),
    ):
        out = get_backend_class(family).query_jobs(["12345"])
    assert out["tasks"] == {}
    assert any(e["code"] == "qstat_unavailable" for e in out["errors"])


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_inspect_returns_minimal_snapshot(family):
    snap = get_backend_class(family).inspect_cluster("c", {})
    d = snap.to_dict()
    assert d["scheduler_kind"] == family
    assert d["nodes"] == []
    assert any(e["code"] == "pbs_inspect_minimal" for e in d["errors"])
