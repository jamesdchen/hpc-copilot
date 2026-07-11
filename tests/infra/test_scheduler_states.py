"""Backend scheduler-state parsing/classification (#157).

The post-submit ``verify-submitted`` verb needs more than the alive-check's
"still in the queue?" — it needs each job's *state* so it can flag an SGE
``Eqw`` (error) or a held job that a plain alive-check reports as merely
present. These cover the pure, scheduler-shape-only backend helpers.
"""

from __future__ import annotations

from hpc_agent.infra.backends import get_backend_class

SGE = get_backend_class("sge")
SLURM = get_backend_class("slurm")


def test_sge_build_scheduler_state_cmd() -> None:
    assert SGE.build_scheduler_state_cmd([]) == "true"
    cmd = SGE.build_scheduler_state_cmd(["123"])
    # Sentinel-ack (positive-evidence rule): the query ends by echoing an
    # affirmative token with the scheduler command's rc, replacing ``|| true``.
    assert "qstat -u" in cmd and "|| true" not in cmd
    assert "__HPC_SCHED_ACK__=$?" in cmd


def test_sge_parse_scheduler_states_picks_state_column() -> None:
    out = (
        "job-ID  prior   name    user   state submit/start at      queue\n"
        "-----------------------------------------------------------------\n"
        "12345 0.50500 myjob   alice  Eqw   05/28/2026 12:00:00  all.q\n"
        "12346 0.50500 myjob   alice  r     05/28/2026 12:00:00  all.q\n"
        "12347 0.50500 myjob   alice  qw    05/28/2026 12:00:00\n"
    )
    states = SGE.parse_scheduler_states(out, ["12345", "12346", "12347", "99999"])
    # 99999 is absent from the output → omitted (caller treats as gone).
    assert states == {"12345": "Eqw", "12346": "r", "12347": "qw"}


def test_sge_classify_scheduler_state() -> None:
    assert SGE.classify_scheduler_state("Eqw") == "error"
    assert SGE.classify_scheduler_state("Er") == "error"
    assert SGE.classify_scheduler_state("hqw") == "held"
    assert SGE.classify_scheduler_state("r") == "alive"
    assert SGE.classify_scheduler_state("qw") == "alive"
    assert SGE.classify_scheduler_state("t") == "alive"


def test_slurm_build_scheduler_state_cmd() -> None:
    assert SLURM.build_scheduler_state_cmd([]) == "true"
    cmd = SLURM.build_scheduler_state_cmd(["12345", "12346"])
    assert "squeue" in cmd and "%T" in cmd and "|| true" not in cmd
    assert "__HPC_SCHED_ACK__=$?" in cmd


def test_scheduler_query_ran_positive_evidence() -> None:
    """The sentinel-ack transport verdict: presence of the ack proves the query
    RAN; absence is UNKNOWN, never 'no jobs'. SGE/PBS additionally require rc 0
    (qstat exits 0 on an empty queue); SLURM accepts any rc (squeue exits
    non-zero once queried ids have left the queue)."""
    # Ack present, rc 0: ran, and the ack line is stripped from the body (the
    # surrounding line structure — incl. the row's own trailing newline — is
    # preserved, since parse_scheduler_states/parse_alive_output splitline it).
    clean, ok = SGE.scheduler_query_ran("12345 r\n__HPC_SCHED_ACK__=0\n")
    assert ok is True and "__HPC_SCHED_ACK__" not in clean and clean == "12345 r\n"
    # SGE with a non-zero ack = qstat itself failed → UNKNOWN.
    _, ok = SGE.scheduler_query_ran("__HPC_SCHED_ACK__=1\n")
    assert ok is False
    # No ack at all (silent/truncated read) = UNKNOWN for every family.
    _, ok = SGE.scheduler_query_ran("")
    assert ok is False
    _, ok = SLURM.scheduler_query_ran("")
    assert ok is False
    # SLURM: ack present but non-zero rc (all jobs left the queue) still ran.
    clean, ok = SLURM.scheduler_query_ran("__HPC_SCHED_ACK__=1\n")
    assert ok is True and clean == ""


def test_slurm_parse_scheduler_states() -> None:
    out = "12345 RUNNING\n12346 PENDING\n12347 FAILED\n"
    states = SLURM.parse_scheduler_states(out, ["12345", "12346", "12347", "88"])
    assert states == {"12345": "RUNNING", "12346": "PENDING", "12347": "FAILED"}


def test_slurm_parse_scheduler_states_array_suffix() -> None:
    # squeue may print array task ids like 12345_3; the base id is matched.
    out = "12345_0 RUNNING\n12345_1 PENDING\n"
    states = SLURM.parse_scheduler_states(out, ["12345"])
    assert states == {"12345": "PENDING"}  # last line wins; both map to base id


def test_slurm_classify_scheduler_state() -> None:
    assert SLURM.classify_scheduler_state("FAILED") == "error"
    assert SLURM.classify_scheduler_state("NODE_FAIL") == "error"
    assert SLURM.classify_scheduler_state("OUT_OF_MEMORY") == "error"
    assert SLURM.classify_scheduler_state("RUNNING") == "alive"
    assert SLURM.classify_scheduler_state("PENDING") == "alive"
